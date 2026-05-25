"""LLM服务层 - 兼容旧接口，内部委托给 Provider"""
from __future__ import annotations

from typing import Any, AsyncIterator

from .factory import get_llm_provider


class LLMService:
    """LLM 服务 — 兼容旧接口，内部使用 Provider 体系"""

    def __init__(self):
        self._provider = get_llm_provider()

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ):
        return await self._provider.chat(messages, tools=tools)

    async def chat_stream(self, messages: list[dict[str, Any]]) -> AsyncIterator[str]:
        async for chunk in self._provider.chat_stream(messages):
            yield chunk

    async def chat_stream_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        async for chunk in self._provider.chat_stream_with_tools(messages, tools=tools):
            yield chunk
