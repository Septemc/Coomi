"""通用 OpenAI-compatible Provider — 配置驱动，无需写新类"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

from openai import AsyncOpenAI

from ...types import LLMResponse, ToolCall
from .config import ProviderConfig
from .provider import LLMProvider


class GenericOpenAIProvider(LLMProvider):
    """通用 OpenAI-compatible Provider

    适用于任何兼容 OpenAI API 格式的服务（GLM, Grok, Gemini via OpenAI endpoint 等）。
    纯配置驱动，不需要为每个服务单独写 Provider 类。

    子类可覆盖 _build_params() 添加厂商特定参数（如 thinking mode）。
    """

    def __init__(self, config: ProviderConfig):
        self.config = config
        base_url = config.base_url or None
        if base_url:
            self.client = AsyncOpenAI(api_key=config.api_key, base_url=base_url)
        else:
            self.client = AsyncOpenAI(api_key=config.api_key)
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
        stream: bool = False,
        tool_choice: str = "auto",
    ) -> dict[str, Any]:
        """构建请求参数 — 子类覆盖以添加厂商特定字段"""
        params: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if stream:
            params["stream"] = True
            params["stream_options"] = {"include_usage": True}
        if tools:
            params["tools"] = tools
        # 禁用 thinking mode，避免 DeepSeek-compatible API 的
        # reasoning_content 回传要求导致 400 错误
        params["extra_body"] = {"thinking": {"type": "disabled"}}
        return params

    def _parse_response(self, response) -> LLMResponse:
        """解析非流式响应 — 子类可覆盖"""
        choice = response.choices[0]
        content = choice.message.content
        reasoning_content = getattr(choice.message, "reasoning_content", None)
        tool_calls = None
        if choice.message.tool_calls:
            tool_calls = [
                ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=json.loads(tc.function.arguments),
                )
                for tc in choice.message.tool_calls
            ]

        usage = None
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }

        return LLMResponse(content=content, tool_calls=tool_calls, usage=usage, reasoning_content=reasoning_content)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs,
    ) -> LLMResponse:
        params = self._build_params(messages, tools, stream=False)
        response = await self.client.chat.completions.create(**params)
        return self._parse_response(response)

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        **kwargs,
    ) -> AsyncIterator[str]:
        params = self._build_params(messages, stream=True)
        response = await self.client.chat.completions.create(**params)
        async for chunk in response:
            if chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    async def chat_stream_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs,
    ) -> AsyncIterator[dict[str, Any]]:
        params = self._build_params(messages, tools, stream=True)

        response = await self.client.chat.completions.create(**params)

        tool_calls_accum: dict[int, dict[str, Any]] = {}
        tool_names_seen: set[int] = set()
        usage_yielded = False

        async for chunk in response:
            if chunk.usage:
                usage_yielded = True
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

            if getattr(delta, "reasoning_content", None):
                yield {"type": "reasoning_content", "content": delta.reasoning_content}

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
                            "id": tc.id or None,
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

        for idx in sorted(tool_calls_accum.keys()):
            tc = tool_calls_accum[idx]
            try:
                tc["arguments"] = json.loads(tc["arguments"])
            except json.JSONDecodeError:
                tc["arguments"] = {}
            yield {"type": "tool_call", "data": tc}
