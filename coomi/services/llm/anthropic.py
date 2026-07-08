"""Anthropic Provider 实现"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from ...types import LLMResponse, ToolCall
from .config import ProviderConfig
from .provider import LLMProvider


class AnthropicProvider(LLMProvider):
    """Anthropic LLM Provider (Claude)"""

    MAX_TOKENS = 8192

    def __init__(self, config: ProviderConfig):
        self.config = config
        timeout = httpx.Timeout(300.0, connect=30.0)  # 300s 总超时，30s 连接超时
        kwargs = {"api_key": config.api_key, "timeout": timeout}
        if config.base_url:
            kwargs["base_url"] = config.base_url
        try:
            import anthropic
        except ImportError:
            raise ImportError(
                "Anthropic provider 需要安装 anthropic 包：\n"
                "  pip install coomi-agent[anthropic]"
            )
        self.client = anthropic.AsyncAnthropic(**kwargs)
        self.model = config.model

    def switch_model(self, model_name: str) -> str:
        self.model = model_name
        return self.model

    def get_model_display_name(self) -> str:
        return self.config.display

    def _convert_messages(
        self, messages: list[dict[str, Any]]
    ) -> tuple[str, list[dict[str, Any]]]:
        system = ""
        converted = []
        i = 0

        while i < len(messages):
            msg = messages[i]
            if msg["role"] == "system":
                system = msg.get("content", "")
                i += 1
            elif msg["role"] == "tool":
                content_blocks = []
                while i < len(messages) and messages[i]["role"] == "tool":
                    tool_msg = messages[i]
                    tool_use_id = tool_msg.get("tool_call_id", "")
                    if tool_use_id:
                        content_blocks.append({
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": tool_msg.get("content", "") or "(Tool completed with no output)",
                        })
                    i += 1
                if content_blocks:
                    converted.append({"role": "user", "content": content_blocks})
                continue
            elif msg["role"] == "assistant" and msg.get("tool_calls"):
                content = []
                if msg.get("content"):
                    content.append({"type": "text", "text": msg["content"]})
                for tc in msg["tool_calls"]:
                    if "function" in tc:
                        name = tc["function"].get("name", "")
                        args = tc["function"].get("arguments", "{}")
                    else:
                        name = tc.get("name", "")
                        args = tc.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    content.append({
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": name,
                        "input": args,
                    })
                converted.append({"role": "assistant", "content": content})
                i += 1
            else:
                converted.append(msg)
                i += 1

        return system, converted

    def _convert_tools(
        self, tools: list[dict[str, Any]] | None
    ) -> list[dict[str, Any]] | None:
        if not tools:
            return None
        converted = []
        for tool in tools:
            func = tool.get("function", {})
            converted.append({
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", {}),
            })
        return converted

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs,
    ) -> LLMResponse:
        system, converted_messages = self._convert_messages(messages)
        converted_tools = self._convert_tools(tools)

        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.MAX_TOKENS,
            "messages": converted_messages,
        }
        if system:
            params["system"] = system
        if converted_tools:
            params["tools"] = converted_tools

        response = await self.client.messages.create(**params)

        content = ""
        tool_calls = None
        for block in response.content:
            if block.type == "text":
                content += block.text
            elif block.type == "tool_use":
                if tool_calls is None:
                    tool_calls = []
                tool_calls.append(ToolCall(
                    id=block.id,
                    name=block.name,
                    arguments=block.input,
                ))

        usage = None
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.input_tokens,
                "completion_tokens": response.usage.output_tokens,
                "total_tokens": response.usage.input_tokens + response.usage.output_tokens,
            }

        return LLMResponse(content=content or None, tool_calls=tool_calls, usage=usage)

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        **kwargs,
    ) -> AsyncIterator[str]:
        system, converted_messages = self._convert_messages(messages)

        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.MAX_TOKENS,
            "messages": converted_messages,
        }
        if system:
            params["system"] = system

        async with self.client.messages.stream(**params) as stream:
            async for text in stream.text_stream:
                yield text

    async def chat_stream_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs,
    ) -> AsyncIterator[dict[str, Any]]:
        system, converted_messages = self._convert_messages(messages)
        converted_tools = self._convert_tools(tools)

        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.MAX_TOKENS,
            "messages": converted_messages,
        }
        if system:
            params["system"] = system
        if converted_tools:
            params["tools"] = converted_tools

        tool_input_accum: dict[int, dict[str, Any]] = {}

        async with self.client.messages.stream(**params) as stream:
            async for event in stream:
                if event.type == "content_block_start":
                    if event.content_block.type == "tool_use":
                        initial_input = getattr(event.content_block, "input", None)
                        tool_input_accum[event.index] = {
                            "id": event.content_block.id,
                            "name": event.content_block.name,
                            "json_fragments": [],
                            "input": initial_input,
                        }
                        yield {
                            "type": "tool_call_start",
                            "tool_name": event.content_block.name,
                            "index": event.index,
                        }
                elif event.type == "content_block_delta":
                    if event.delta.type == "text_delta":
                        yield {"type": "content", "content": event.delta.text}
                    elif event.delta.type == "input_json_delta":
                        idx = event.index
                        if idx in tool_input_accum:
                            tool_input_accum[idx]["json_fragments"].append(
                                event.delta.partial_json
                            )

            final_msg = await stream.get_final_message()

        _merge_final_tool_inputs(tool_input_accum, final_msg)

        if final_msg.usage:
            yield {
                "type": "usage",
                "data": {
                    "prompt_tokens": final_msg.usage.input_tokens,
                    "completion_tokens": final_msg.usage.output_tokens,
                    "total_tokens": final_msg.usage.input_tokens + final_msg.usage.output_tokens,
                },
            }

        for idx in sorted(tool_input_accum.keys()):
            acc = tool_input_accum[idx]
            raw_arguments = "".join(acc["json_fragments"])
            arguments, raw_arguments, parse_error = _parse_anthropic_tool_input(
                acc.get("input"),
                raw_arguments,
            )
            yield {
                "type": "tool_call",
                "data": {
                    "id": acc["id"],
                    "name": acc["name"],
                    "arguments": arguments,
                    "raw_arguments": raw_arguments,
                    "parse_error": parse_error,
                },
            }


def _merge_final_tool_inputs(
    tool_input_accum: dict[int, dict[str, Any]],
    final_msg: Any,
) -> None:
    """Fill missing tool inputs from the final Anthropic message."""
    for idx, block in enumerate(getattr(final_msg, "content", []) or []):
        if getattr(block, "type", None) != "tool_use":
            continue
        acc = tool_input_accum.setdefault(
            idx,
            {
                "id": getattr(block, "id", f"toolu_{idx}"),
                "name": getattr(block, "name", ""),
                "json_fragments": [],
                "input": None,
            },
        )
        if not acc.get("id"):
            acc["id"] = getattr(block, "id", f"toolu_{idx}")
        if not acc.get("name"):
            acc["name"] = getattr(block, "name", "")
        if acc.get("input") in (None, {}, ""):
            acc["input"] = getattr(block, "input", None)


def _parse_anthropic_tool_input(
    direct_input: Any,
    raw_json: str,
) -> tuple[dict[str, Any], str, str | None]:
    if isinstance(direct_input, dict) and direct_input:
        return direct_input, json.dumps(direct_input, ensure_ascii=False), None
    if isinstance(direct_input, str) and direct_input.strip():
        raw_json = direct_input.strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            return {}, raw_json, str(exc)
        if isinstance(parsed, dict):
            return parsed, raw_json, None
        return {}, raw_json, "Anthropic tool input must be a JSON object."
    if isinstance(direct_input, dict):
        return direct_input, json.dumps(direct_input, ensure_ascii=False), None
    return {}, raw_json, None
