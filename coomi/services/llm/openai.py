"""OpenAI Provider 实现"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx
from openai import AsyncOpenAI

from ...types import LLMResponse, ToolCall
from .config import ProviderConfig
from .provider import LLMProvider
from .text_tool_calls import strip_text_tool_calls


class OpenAIProvider(LLMProvider):
    """OpenAI LLM Provider"""

    def __init__(self, config: ProviderConfig):
        self.config = config
        timeout = httpx.Timeout(300.0, connect=30.0)  # 300s 总超时，30s 连接超时
        self.client = AsyncOpenAI(api_key=config.api_key, base_url=config.base_url or None, timeout=timeout)
        self.model = config.model

    def switch_model(self, model_name: str) -> str:
        self.model = model_name
        return self.model

    def get_model_display_name(self) -> str:
        return self.config.display

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs,
    ) -> LLMResponse:
        params: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if tools:
            params["tools"] = tools

        response = await self.client.chat.completions.create(**params)

        choice = response.choices[0]
        content = choice.message.content
        content, text_tool_calls = strip_text_tool_calls(content)
        tool_calls = None
        if choice.message.tool_calls:
            tool_calls = []
            for tc in choice.message.tool_calls:
                raw_arguments = tc.function.arguments or ""
                try:
                    arguments = json.loads(raw_arguments)
                    parse_error = None
                except (json.JSONDecodeError, TypeError) as exc:
                    arguments = {}
                    parse_error = str(exc)
                tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=arguments,
                        raw_arguments=raw_arguments,
                        parse_error=parse_error,
                    )
                )
        if text_tool_calls:
            if tool_calls is None:
                tool_calls = []
            for tc in text_tool_calls:
                tool_calls.append(
                    ToolCall(
                        id=tc["id"],
                        name=tc["name"],
                        arguments=tc["arguments"],
                        raw_arguments=tc.get("raw_arguments"),
                        parse_error=tc.get("parse_error"),
                    )
                )

        usage = None
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }

        return LLMResponse(content=content, tool_calls=tool_calls, usage=usage)

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        **kwargs,
    ) -> AsyncIterator[str]:
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            stream=True,
        )
        async for chunk in response:
            if chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    async def chat_stream_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs,
    ) -> AsyncIterator[dict[str, Any]]:
        params: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            params["tools"] = tools

        response = await self.client.chat.completions.create(**params)

        tool_calls_accum: dict[int, dict[str, Any]] = {}
        tool_names_seen: set[int] = set()
        async for chunk in response:
            if chunk.usage:
                yield {
                    "type": "usage",
                    "data": {
                        "prompt_tokens": chunk.usage.prompt_tokens,
                        "completion_tokens": chunk.usage.completion_tokens,
                        "total_tokens": chunk.usage.total_tokens,
                    },
                }

            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta

            if delta.content:
                yield {"type": "content", "content": delta.content}

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_names_seen and tc.function and tc.function.name:
                        tool_names_seen.add(idx)
                        yield {
                            "type": "tool_call_start",
                            "tool_name": tc.function.name,
                            "index": idx,
                        }
                    if idx not in tool_calls_accum:
                        tool_calls_accum[idx] = {
                            "id": tc.id or "",
                            "name": tc.function.name if tc.function and tc.function.name else "",
                            "arguments": tc.function.arguments if tc.function and tc.function.arguments else "",
                        }
                    else:
                        if tc.id:
                            tool_calls_accum[idx]["id"] = tc.id
                        if tc.function and tc.function.name:
                            tool_calls_accum[idx]["name"] = tc.function.name
                        if tc.function and tc.function.arguments:
                            tool_calls_accum[idx]["arguments"] += tc.function.arguments

        for idx in sorted(tool_calls_accum.keys()):
            tc = tool_calls_accum[idx]
            raw_arguments = tc["arguments"]
            try:
                tc["arguments"] = json.loads(raw_arguments)
                tc["parse_error"] = None
            except (json.JSONDecodeError, TypeError) as exc:
                tc["arguments"] = {}
                tc["raw_arguments"] = raw_arguments
                tc["parse_error"] = str(exc)
            else:
                tc["raw_arguments"] = raw_arguments
            yield {"type": "tool_call", "data": tc}
