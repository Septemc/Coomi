"""LLM Provider 抽象基类"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

from ...types import LLMResponse


class LLMProvider(ABC):
    """LLM Provider 抽象基类 - 所有厂商实现必须继承"""

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs,
    ) -> LLMResponse:
        """异步调用LLM

        Args:
            messages: 消息列表
            tools: 工具定义列表（OpenAI格式）
            **kwargs: 厂商特定参数

        Returns:
            LLMResponse: 统一的响应格式
        """
        pass

    @abstractmethod
    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        **kwargs,
    ) -> AsyncIterator[str]:
        """流式纯文本输出（不支持工具调用）

        Args:
            messages: 消息列表
            **kwargs: 厂商特定参数

        Yields:
            str: 文本片段
        """
        pass

    @abstractmethod
    async def chat_stream_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs,
    ) -> AsyncIterator[dict[str, Any]]:
        """流式输出 + 工具调用

        Args:
            messages: 消息列表
            tools: 工具定义列表
            **kwargs: 厂商特定参数

        Yields:
            dict: {"type": "content", "content": "..."} 或 {"type": "tool_call", "data": {...}} 或 {"type": "usage", "data": {...}}
        """
        pass

    @abstractmethod
    def switch_model(self, model_name: str) -> str:
        """运行时切换模型（纯内存操作，无需 async）"""
        pass

    @abstractmethod
    def get_model_display_name(self) -> str:
        """获取人类可读的模型显示名称（纯内存操作，无需 async）"""
        pass
