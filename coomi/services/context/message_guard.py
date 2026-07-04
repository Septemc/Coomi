"""API message preparation and tool-call pairing guards."""
from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from ...types import Message, Session, ToolCall


SYNTHETIC_TOOL_RESULT = (
    "Error: Tool result was missing from the local transcript. "
    "The system inserted this placeholder to keep the conversation valid. "
    "Re-evaluate the previous tool call before continuing."
)


class MessagePairingError(ValueError):
    """Raised when message history cannot be prepared without repair."""


def prepare_messages_for_api(session: Session, repair: bool = True) -> list[dict[str, Any]]:
    """Return provider-safe messages for a session.

    This function is intentionally defensive. Local transcripts can become
    malformed after cancellation, compression, resume, or older bugs. Providers
    reject malformed assistant tool_call/tool result sequences, so the final
    outbound payload is normalized here without mutating the transcript.
    """
    prepared: list[dict[str, Any]] = [{"role": "system", "content": session.system_prompt}]
    prepared.extend(_prepare_transcript_messages(session.messages, repair=repair))
    return prepared


def _prepare_transcript_messages(messages: list[Message], repair: bool) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen_tool_call_ids: set[str] = set()
    i = 0

    while i < len(messages):
        msg = messages[i]

        if msg.role == "system":
            i += 1
            continue

        if msg.role == "tool":
            if not repair:
                raise MessagePairingError(
                    f"Orphaned tool result at message {i}: {msg.tool_call_id or '<missing id>'}"
                )
            i += 1
            continue

        if msg.role != "assistant" or not msg.tool_calls:
            msg_dict = msg.to_dict(include_reasoning=False)
            if _has_api_content(msg_dict):
                result.append(msg_dict)
            i += 1
            continue

        kept_native_calls: list[ToolCall] = []
        kept_text_calls: list[ToolCall] = []
        duplicate_ids: list[str] = []
        for tool_call in msg.tool_calls:
            if not tool_call.id:
                duplicate_ids.append("<missing id>")
                continue
            if tool_call.id in seen_tool_call_ids:
                duplicate_ids.append(tool_call.id)
                continue
            seen_tool_call_ids.add(tool_call.id)
            if _is_text_fallback_tool_call(tool_call):
                kept_text_calls.append(tool_call)
            else:
                kept_native_calls.append(tool_call)

        if duplicate_ids and not repair:
            raise MessagePairingError(
                f"Duplicate tool_call ids at message {i}: {', '.join(duplicate_ids)}"
            )

        if kept_text_calls:
            text_call_msg = _text_fallback_calls_to_message(
                msg,
                kept_text_calls,
                include_message_content=not kept_native_calls,
            )
            if _has_api_content(text_call_msg):
                result.append(text_call_msg)

        if kept_native_calls:
            assistant_msg = replace(msg, tool_calls=kept_native_calls).to_dict(
                include_reasoning=False
            )
            if assistant_msg.get("tool_calls") and "content" not in assistant_msg:
                assistant_msg["content"] = ""
            result.append(assistant_msg)
        elif not kept_text_calls:
            assistant_msg = {"role": "assistant", "content": "[Tool call removed]"}
            result.append(assistant_msg)

        native_expected_ids = [tool_call.id for tool_call in kept_native_calls]
        text_expected_ids = [tool_call.id for tool_call in kept_text_calls]
        native_expected_set = set(native_expected_ids)
        text_calls_by_id = {tool_call.id: tool_call for tool_call in kept_text_calls}
        seen_native_results: set[str] = set()
        seen_text_results: set[str] = set()
        native_result_dicts: list[dict[str, Any]] = []
        text_result_dicts: list[dict[str, Any]] = []

        j = i + 1
        while j < len(messages) and messages[j].role == "tool":
            tool_msg = messages[j]
            tool_call_id = tool_msg.tool_call_id
            if tool_call_id in text_calls_by_id and tool_call_id not in seen_text_results:
                text_result_dicts.append(
                    _text_fallback_result_to_message(text_calls_by_id[tool_call_id], tool_msg)
                )
                seen_text_results.add(tool_call_id)
            elif tool_call_id in native_expected_set and tool_call_id not in seen_native_results:
                native_result_dicts.append(_tool_message_to_dict(tool_msg, tool_call_id))
                seen_native_results.add(tool_call_id)
            elif not repair:
                raise MessagePairingError(
                    f"Unexpected tool result after message {i}: {tool_call_id or '<missing id>'}"
                )
            j += 1

        missing_native_ids = [
            tool_call_id
            for tool_call_id in native_expected_ids
            if tool_call_id not in seen_native_results
        ]
        missing_text_ids = [
            tool_call_id
            for tool_call_id in text_expected_ids
            if tool_call_id not in seen_text_results
        ]
        if (missing_native_ids or missing_text_ids) and not repair:
            raise MessagePairingError(
                "Missing tool results after message "
                f"{i}: {', '.join(missing_native_ids + missing_text_ids)}"
            )

        for missing_id in missing_native_ids:
            native_result_dicts.append(
                {"role": "tool", "tool_call_id": missing_id, "content": SYNTHETIC_TOOL_RESULT}
            )

        for missing_id in missing_text_ids:
            text_result_dicts.append(
                _text_fallback_result_to_message(
                    text_calls_by_id[missing_id],
                    Message(
                        role="tool",
                        tool_call_id=missing_id,
                        content=SYNTHETIC_TOOL_RESULT,
                    ),
                )
            )

        result.extend(native_result_dicts)
        result.extend(text_result_dicts)
        i = j

    return result


def _tool_message_to_dict(msg: Message, tool_call_id: str) -> dict[str, Any]:
    content = msg.content
    if content is None or content == "":
        content = "(Tool completed with no output)"
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


def _is_text_fallback_tool_call(tool_call: ToolCall) -> bool:
    return tool_call.source == "text_fallback" or tool_call.id.startswith("text_call_")


def _text_fallback_calls_to_message(
    msg: Message,
    tool_calls: list[ToolCall],
    include_message_content: bool = True,
) -> dict[str, Any]:
    parts: list[str] = []
    if include_message_content and msg.content:
        parts.append(msg.content)
    parts.append("Text fallback tool call(s) parsed from assistant content:")
    for tool_call in tool_calls:
        parts.append(_format_text_fallback_tool_call(tool_call))
    return {"role": "assistant", "content": "\n\n".join(parts)}


def _text_fallback_result_to_message(
    tool_call: ToolCall,
    msg: Message,
) -> dict[str, Any]:
    content = msg.content
    if content is None or content == "":
        content = "(Tool completed with no output)"
    return {
        "role": "user",
        "content": (
            "Text fallback tool result:\n"
            f"Tool call id: {tool_call.id}\n"
            f"Tool: {tool_call.name}\n"
            f"Arguments: {_json_dumps(tool_call.arguments)}\n"
            f"Result:\n{content}"
        ),
    }


def _format_text_fallback_tool_call(tool_call: ToolCall) -> str:
    lines = [
        f"Tool call id: {tool_call.id}",
        f"Tool: {tool_call.name}",
        f"Arguments: {_json_dumps(tool_call.arguments)}",
    ]
    if tool_call.parse_error:
        lines.append(f"Parse error: {tool_call.parse_error}")
    return "\n".join(lines)


def _json_dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _has_api_content(msg: dict[str, Any]) -> bool:
    if msg.get("content") is not None:
        return True
    if msg.get("tool_calls"):
        return True
    if msg.get("role") == "tool" and msg.get("tool_call_id"):
        return True
    return False
