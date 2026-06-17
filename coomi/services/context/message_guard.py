"""API message preparation and tool-call pairing guards."""
from __future__ import annotations

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

        kept_calls: list[ToolCall] = []
        duplicate_ids: list[str] = []
        for tool_call in msg.tool_calls:
            if not tool_call.id:
                duplicate_ids.append("<missing id>")
                continue
            if tool_call.id in seen_tool_call_ids:
                duplicate_ids.append(tool_call.id)
                continue
            seen_tool_call_ids.add(tool_call.id)
            kept_calls.append(tool_call)

        if duplicate_ids and not repair:
            raise MessagePairingError(
                f"Duplicate tool_call ids at message {i}: {', '.join(duplicate_ids)}"
            )

        assistant_msg = replace(msg, tool_calls=kept_calls or None).to_dict(
            include_reasoning=False
        )
        if not _has_api_content(assistant_msg):
            assistant_msg = {"role": "assistant", "content": "[Tool call removed]"}
        result.append(assistant_msg)

        expected_ids = [tool_call.id for tool_call in kept_calls]
        expected_set = set(expected_ids)
        seen_results: set[str] = set()
        tool_result_dicts: list[dict[str, Any]] = []

        j = i + 1
        while j < len(messages) and messages[j].role == "tool":
            tool_msg = messages[j]
            tool_call_id = tool_msg.tool_call_id
            if tool_call_id in expected_set and tool_call_id not in seen_results:
                tool_result_dicts.append(_tool_message_to_dict(tool_msg, tool_call_id))
                seen_results.add(tool_call_id)
            elif not repair:
                raise MessagePairingError(
                    f"Unexpected tool result after message {i}: {tool_call_id or '<missing id>'}"
                )
            j += 1

        missing_ids = [tool_call_id for tool_call_id in expected_ids if tool_call_id not in seen_results]
        if missing_ids and not repair:
            raise MessagePairingError(
                f"Missing tool results after message {i}: {', '.join(missing_ids)}"
            )

        for missing_id in missing_ids:
            tool_result_dicts.append(
                {"role": "tool", "tool_call_id": missing_id, "content": SYNTHETIC_TOOL_RESULT}
            )

        result.extend(tool_result_dicts)
        i = j

    return result


def _tool_message_to_dict(msg: Message, tool_call_id: str) -> dict[str, Any]:
    content = msg.content
    if content is None or content == "":
        content = "(Tool completed with no output)"
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


def _has_api_content(msg: dict[str, Any]) -> bool:
    if msg.get("content") is not None:
        return True
    if msg.get("tool_calls"):
        return True
    if msg.get("role") == "tool" and msg.get("tool_call_id"):
        return True
    return False

