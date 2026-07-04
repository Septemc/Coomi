"""通用 OpenAI-compatible Provider — 配置驱动，无需写新类"""
from __future__ import annotations

import json
import re
from typing import Any, AsyncIterator

import httpx
from openai import BadRequestError
from openai import AsyncOpenAI

from ...types import LLMResponse, ToolCall
from .config import ProviderConfig
from .provider import LLMProvider
from .text_tool_calls import strip_text_tool_calls


class GenericOpenAIProvider(LLMProvider):
    """通用 OpenAI-compatible Provider

    适用于任何兼容 OpenAI API 格式的服务（GLM, Grok, Gemini via OpenAI endpoint 等）。
    纯配置驱动，不需要为每个服务单独写 Provider 类。

    子类可覆盖 _build_params() 添加厂商特定参数（如 thinking mode）。
    """

    def __init__(self, config: ProviderConfig):
        self.config = config
        timeout = httpx.Timeout(300.0, connect=30.0)  # 300s 总超时，30s 连接超时
        base_url = config.base_url or None
        if base_url:
            self.client = AsyncOpenAI(api_key=config.api_key, base_url=base_url, timeout=timeout)
        else:
            self.client = AsyncOpenAI(api_key=config.api_key, timeout=timeout)
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
            params["tool_choice"] = tool_choice
        return params

    def _fallback_params(self, params: dict[str, Any]) -> dict[str, Any] | None:
        """Return a less strict request variant for imperfect OpenAI-compatible APIs."""
        if "extra_body" in params:
            fallback = dict(params)
            fallback.pop("extra_body", None)
            return fallback
        if "stream_options" in params:
            fallback = dict(params)
            fallback.pop("stream_options", None)
            return fallback
        if "tool_choice" in params:
            fallback = dict(params)
            fallback.pop("tool_choice", None)
            return fallback
        if "tools" in params:
            fallback = dict(params)
            fallback.pop("tools", None)
            fallback.pop("tool_choice", None)
            return fallback
        return None

    async def _create_completion_with_fallback(
        self,
        params: dict[str, Any],
    ) -> tuple[Any, dict[str, Any]]:
        current = params
        seen: set[str] = set()
        while True:
            try:
                response = await self.client.chat.completions.create(**current)
                return response, current
            except BadRequestError:
                key = json.dumps(sorted(current.keys()))
                if key in seen:
                    raise
                seen.add(key)
                fallback = self._fallback_params(current)
                if fallback is None:
                    raise
                current = fallback

    async def _stream_completion_chunks_with_fallback(
        self,
        params: dict[str, Any],
    ) -> AsyncIterator[Any]:
        current = params
        seen: set[str] = set()
        while True:
            response, used_params = await self._create_completion_with_fallback(current)
            yielded_chunk = False
            try:
                async for chunk in response:
                    yielded_chunk = True
                    yield chunk
                return
            except BadRequestError as exc:
                if yielded_chunk:
                    raise RuntimeError(
                        "Streaming BadRequestError after a partial response; "
                        "not retrying fallback to avoid duplicate output."
                    ) from exc

                key = json.dumps(sorted(used_params.keys()))
                if key in seen:
                    raise
                seen.add(key)
                fallback = self._fallback_params(used_params)
                if fallback is None:
                    raise
                current = fallback

    def _parse_response(self, response, tools_enabled: bool = True) -> LLMResponse:
        """解析非流式响应 — 子类可覆盖"""
        choice = response.choices[0]
        content = choice.message.content
        reasoning_content = getattr(choice.message, "reasoning_content", None)
        content, tag_reasoning = _strip_thinking_tags(content)
        if tag_reasoning:
            reasoning_content = ((reasoning_content or "") + tag_reasoning).strip()
        text_tool_mode = self.get_text_tool_mode() if tools_enabled else "disabled"
        content, text_tool_calls = strip_text_tool_calls(content, mode=text_tool_mode)
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
                        source=tc.get("source", "text_fallback"),
                    )
                )

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
        response, _ = await self._create_completion_with_fallback(params)
        return self._parse_response(response, tools_enabled=bool(tools))

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        **kwargs,
    ) -> AsyncIterator[str]:
        params = self._build_params(messages, stream=True)
        thinking_filter = ThinkingTagFilter()
        async for chunk in self._stream_completion_chunks_with_fallback(params):
            if chunk.choices[0].delta.content:
                reasoning, content = thinking_filter.feed(chunk.choices[0].delta.content)
                if reasoning:
                    # chat_stream's legacy API only yields text, so thinking text is suppressed here.
                    pass
                if content:
                    yield content

    async def chat_stream_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs,
    ) -> AsyncIterator[dict[str, Any]]:
        params = self._build_params(messages, tools, stream=True)

        tool_calls_accum: dict[int, dict[str, Any]] = {}
        tool_names_seen: set[int] = set()
        usage_yielded = False
        thinking_filter = ThinkingTagFilter()

        async for chunk in self._stream_completion_chunks_with_fallback(params):
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
                reasoning, content = thinking_filter.feed(delta.content)
                if reasoning:
                    yield {"type": "reasoning_content", "content": reasoning}
                if content:
                    yield {"type": "content", "content": content}

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


def _strip_thinking_tags(content: str | None) -> tuple[str | None, str | None]:
    if not content or "<think>" not in content.lower():
        return content, None
    reasoning_parts: list[str] = []

    def replace(match: re.Match[str]) -> str:
        reasoning_parts.append(match.group(1).strip())
        return ""

    stripped = re.sub(
        r"<think>(.*?)</think>",
        replace,
        content,
        flags=re.IGNORECASE | re.DOTALL,
    )
    stripped = re.sub(r"</?think>", "", stripped, flags=re.IGNORECASE).strip()
    reasoning = "\n".join(part for part in reasoning_parts if part)
    return stripped or None, reasoning or None


class ThinkingTagFilter:
    """Route <think>...</think> stream fragments away from visible content."""

    def __init__(self):
        self._in_think = False
        self._pending = ""

    def feed(self, chunk: str) -> tuple[str, str]:
        text = self._pending + chunk
        self._pending = ""
        reasoning_parts: list[str] = []
        content_parts: list[str] = []

        while text:
            lower = text.lower()
            if self._in_think:
                end = lower.find("</think>")
                if end == -1:
                    reasoning_parts.append(text)
                    text = ""
                else:
                    reasoning_parts.append(text[:end])
                    text = text[end + len("</think>"):]
                    self._in_think = False
                continue

            start = lower.find("<think>")
            if start == -1:
                keep = _split_possible_tag_prefix(text)
                if keep:
                    content_parts.append(text[:-keep])
                    self._pending = text[-keep:]
                else:
                    content_parts.append(text)
                text = ""
            else:
                content_parts.append(text[:start])
                text = text[start + len("<think>"):]
                self._in_think = True

        return "".join(reasoning_parts), "".join(content_parts)


def _split_possible_tag_prefix(text: str) -> int:
    tag = "<think>"
    lower = text.lower()
    max_len = min(len(tag) - 1, len(text))
    for size in range(max_len, 0, -1):
        if tag.startswith(lower[-size:]):
            return size
    return 0
