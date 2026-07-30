"""OpenAI Responses API provider."""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx
from openai import AsyncOpenAI

from ...types import LLMResponse, ToolCall
from .config import ProviderConfig
from .provider import LLMProvider
from .text_tool_calls import strip_text_tool_calls


class OpenAIResponsesProvider(LLMProvider):
    """Provider for GPT models exposed through OpenAI's Responses API."""

    def __init__(self, config: ProviderConfig):
        self.config = config
        timeout = httpx.Timeout(300.0, connect=30.0)
        self.client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url or None,
            timeout=timeout,
        )
        if not hasattr(self.client, "responses"):
            raise RuntimeError(
                "OpenAI Responses requires openai>=1.66.0. "
                "Upgrade with: pip install -U openai"
            )
        self.model = config.model

    def switch_model(self, model_name: str) -> str:
        self.model = model_name
        return self.model

    def get_model_display_name(self) -> str:
        return self.config.display

    def _build_params(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        stream: bool = False,
    ) -> dict[str, Any]:
        instructions, response_input = _convert_messages(messages)
        params: dict[str, Any] = {
            "model": self.model,
            "input": response_input,
        }
        if instructions:
            params["instructions"] = instructions
        converted_tools = _convert_tools(tools)
        if converted_tools:
            params["tools"] = converted_tools
            params["tool_choice"] = "auto"
        if stream:
            params["stream"] = True
        return params

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs,
    ) -> LLMResponse:
        response = await self.client.responses.create(
            **self._build_params(messages, tools, stream=False)
        )
        return _parse_response(response, text_tool_mode=self.get_text_tool_mode() if tools else "disabled")

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        **kwargs,
    ) -> AsyncIterator[str]:
        async for event in self.chat_stream_with_tools(messages, tools=None, **kwargs):
            if event.get("type") == "content":
                yield event.get("content", "")

    async def chat_stream_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs,
    ) -> AsyncIterator[dict[str, Any]]:
        stream = await self.client.responses.create(
            **self._build_params(messages, tools, stream=True)
        )
        tool_calls: dict[object, dict[str, Any]] = {}
        tool_order: list[object] = []
        tool_starts: set[object] = set()
        usage_yielded = False

        async for event in stream:
            event_type = _get(event, "type", "")

            if event_type == "response.output_text.delta":
                delta = _get(event, "delta", "")
                if delta:
                    yield {"type": "content", "content": delta}
                continue

            if event_type in {
                "response.reasoning_summary_text.delta",
                "response.reasoning_text.delta",
            }:
                delta = _get(event, "delta", "")
                if delta:
                    yield {"type": "reasoning_content", "content": delta}
                continue

            if event_type in {"response.output_item.added", "response.output_item.done"}:
                item = _get(event, "item")
                if _get(item, "type") == "function_call":
                    key = _tool_key(event, item)
                    acc = _ensure_tool(tool_calls, tool_order, key)
                    _merge_tool_item(acc, item, replace_arguments=event_type.endswith(".done"))
                    if key not in tool_starts and acc["name"]:
                        tool_starts.add(key)
                        yield {
                            "type": "tool_call_start",
                            "tool_name": acc["name"],
                            "index": _get(event, "output_index", len(tool_order) - 1),
                        }
                continue

            if event_type == "response.function_call_arguments.delta":
                key = _tool_key(event)
                acc = _ensure_tool(tool_calls, tool_order, key)
                acc["arguments"] += _get(event, "delta", "") or ""
                continue

            if event_type == "response.function_call_arguments.done":
                key = _tool_key(event)
                acc = _ensure_tool(tool_calls, tool_order, key)
                acc["name"] = _get(event, "name", acc["name"]) or acc["name"]
                acc["arguments"] = _get(event, "arguments", acc["arguments"]) or acc["arguments"]
                if key not in tool_starts and acc["name"]:
                    tool_starts.add(key)
                    yield {
                        "type": "tool_call_start",
                        "tool_name": acc["name"],
                        "index": _get(event, "output_index", len(tool_order) - 1),
                    }
                continue

            if event_type == "response.completed":
                response = _get(event, "response")
                _merge_response_tools(tool_calls, tool_order, response)
                usage = _usage_dict(_get(response, "usage"))
                if usage:
                    usage_yielded = True
                    yield {"type": "usage", "data": usage}
                continue

            if event_type in {"response.failed", "response.error", "error"}:
                error = _get(event, "error") or _get(event, "message") or event_type
                raise RuntimeError(f"OpenAI Responses stream failed: {error}")

        if not usage_yielded:
            from ..context.compressor import _estimate_tokens_from_dicts

            estimated = _estimate_tokens_from_dicts(messages)
            yield {
                "type": "usage",
                "data": {
                    "prompt_tokens": estimated,
                    "completion_tokens": 0,
                    "total_tokens": estimated,
                },
            }

        for key in tool_order:
            acc = tool_calls[key]
            raw_arguments = acc["arguments"] or "{}"
            try:
                arguments = json.loads(raw_arguments)
                if not isinstance(arguments, dict):
                    raise TypeError("function arguments must be a JSON object")
                parse_error = None
            except (json.JSONDecodeError, TypeError) as exc:
                arguments = {}
                parse_error = str(exc)
            yield {
                "type": "tool_call",
                "data": {
                    "id": acc["id"] or str(key),
                    "name": acc["name"],
                    "arguments": arguments,
                    "raw_arguments": raw_arguments,
                    "parse_error": parse_error,
                },
            }


def _convert_messages(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Convert Chat Completions history into stateless Responses input items."""
    instructions: list[str] = []
    response_input: list[dict[str, Any]] = []

    for message in messages:
        role = str(message.get("role") or "user")
        content = message.get("content")

        if role == "system":
            if content:
                instructions.append(str(content))
            continue

        if role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            if call_id:
                response_input.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": str(content or "(Tool completed with no output)"),
                    }
                )
            continue

        if content is not None:
            response_input.append({"role": role, "content": str(content)})

        if role != "assistant":
            continue
        for tool_call in message.get("tool_calls") or []:
            function = tool_call.get("function") or {}
            arguments = function.get("arguments", "{}")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)
            call_id = str(tool_call.get("id") or "")
            response_input.append(
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": str(function.get("name") or tool_call.get("name") or ""),
                    "arguments": arguments,
                }
            )

    return "\n\n".join(instructions), response_input


def _convert_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    converted: list[dict[str, Any]] = []
    for tool in tools:
        function = tool.get("function") or {}
        converted.append(
            {
                "type": "function",
                "name": str(function.get("name") or ""),
                "description": str(function.get("description") or ""),
                "parameters": function.get("parameters") or {
                    "type": "object",
                    "properties": {},
                },
                "strict": bool(function.get("strict", False)),
            }
        )
    return converted


def _parse_response(response: Any, text_tool_mode: str = "disabled") -> LLMResponse:
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[ToolCall] = []

    for item in _get(response, "output", []) or []:
        item_type = _get(item, "type")
        if item_type == "message":
            for part in _get(item, "content", []) or []:
                part_type = _get(part, "type")
                if part_type == "output_text":
                    text = _get(part, "text", "")
                    if text:
                        content_parts.append(text)
                elif part_type == "refusal":
                    refusal = _get(part, "refusal", "")
                    if refusal:
                        content_parts.append(refusal)
        elif item_type == "function_call":
            tool_calls.append(_tool_call_from_item(item))
        elif item_type == "reasoning":
            for summary in _get(item, "summary", []) or []:
                text = _get(summary, "text", "")
                if text:
                    reasoning_parts.append(text)

    if not content_parts:
        output_text = _get(response, "output_text", "")
        if output_text:
            content_parts.append(output_text)

    content = "".join(content_parts) or None
    content, text_tool_calls = strip_text_tool_calls(content, mode=text_tool_mode)
    for item in text_tool_calls:
        tool_calls.append(
            ToolCall(
                id=item["id"],
                name=item["name"],
                arguments=item["arguments"],
                raw_arguments=item.get("raw_arguments"),
                parse_error=item.get("parse_error"),
                source=item.get("source", "text_fallback"),
            )
        )

    return LLMResponse(
        content=content,
        tool_calls=tool_calls or None,
        usage=_usage_dict(_get(response, "usage")),
        reasoning_content="\n".join(reasoning_parts) or None,
    )


def _tool_call_from_item(item: Any) -> ToolCall:
    raw_arguments = _get(item, "arguments", "") or "{}"
    try:
        arguments = json.loads(raw_arguments)
        if not isinstance(arguments, dict):
            raise TypeError("function arguments must be a JSON object")
        parse_error = None
    except (json.JSONDecodeError, TypeError) as exc:
        arguments = {}
        parse_error = str(exc)
    return ToolCall(
        id=_get(item, "call_id") or _get(item, "id") or "",
        name=_get(item, "name", "") or "",
        arguments=arguments,
        raw_arguments=raw_arguments,
        parse_error=parse_error,
    )


def _usage_dict(usage: Any) -> dict[str, int] | None:
    if not usage:
        return None
    prompt_tokens = int(_get(usage, "input_tokens", 0) or 0)
    completion_tokens = int(_get(usage, "output_tokens", 0) or 0)
    total_tokens = int(_get(usage, "total_tokens", prompt_tokens + completion_tokens) or 0)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def _tool_key(event: Any, item: Any = None) -> object:
    output_index = _get(event, "output_index")
    if output_index is not None:
        return output_index
    item_id = _get(event, "item_id") or _get(item, "id") or _get(item, "call_id")
    return item_id or 0


def _ensure_tool(
    tools: dict[object, dict[str, Any]],
    order: list[object],
    key: object,
) -> dict[str, Any]:
    if key not in tools:
        tools[key] = {"id": "", "name": "", "arguments": ""}
        order.append(key)
    return tools[key]


def _merge_tool_item(acc: dict[str, Any], item: Any, *, replace_arguments: bool) -> None:
    acc["id"] = _get(item, "call_id") or _get(item, "id") or acc["id"]
    acc["name"] = _get(item, "name", acc["name"]) or acc["name"]
    arguments = _get(item, "arguments", "") or ""
    if arguments:
        if replace_arguments or not acc["arguments"]:
            acc["arguments"] = arguments
        else:
            acc["arguments"] += arguments


def _merge_response_tools(
    tools: dict[object, dict[str, Any]],
    order: list[object],
    response: Any,
) -> None:
    for index, item in enumerate(_get(response, "output", []) or []):
        if _get(item, "type") != "function_call":
            continue
        key = index
        acc = _ensure_tool(tools, order, key)
        _merge_tool_item(acc, item, replace_arguments=True)


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


# Backwards-compatible import name used by older integrations.
OpenAIProvider = OpenAIResponsesProvider
