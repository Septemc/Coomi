"""LLM服务层 - 兼容旧接口，内部委托给 ConfigManager"""
from __future__ import annotations

from typing import Any

from .factory import get_llm_provider


class LLMService:
    """LLM 服务 — 兼容旧接口，内部使用 Provider 体系"""

    def __init__(self):
        self._provider = get_llm_provider()

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ):
        return self._provider.chat(messages, tools=tools)

    def chat_stream(self, messages: list[dict[str, Any]]):
        yield from self._provider.chat_stream(messages)

    def chat_stream_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ):
        yield from self._provider.chat_stream_with_tools(messages, tools=tools)
