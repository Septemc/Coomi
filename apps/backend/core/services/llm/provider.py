"""LLM Provider 抽象基类"""
from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Iterator

from ...types import LLMResponse


class ToolCallMode(Enum):
    """工具调用模式"""
    NATIVE = "native"      # 原生工具调用 (OpenAI-compatible)
    JSON = "json"          # JSON 降级方案


class LLMProvider(ABC):
    """LLM Provider 抽象基类 - 所有厂商实现必须继承"""

    @abstractmethod
    def get_tool_call_mode(self) -> ToolCallMode:
        """返回支持的工具调用模式

        Returns:
            ToolCallMode: 原生工具调用或 JSON 降级方案
        """
        pass

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs,
    ) -> LLMResponse:
        """同步调用LLM

        Args:
            messages: 消息列表
            tools: 工具定义列表（OpenAI格式）
            **kwargs: 厂商特定参数

        Returns:
            LLMResponse: 统一的响应格式
        """
        pass

    @abstractmethod
    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        **kwargs,
    ) -> Iterator[str]:
        """流式纯文本输出（不支持工具调用）

        Args:
            messages: 消息列表
            **kwargs: 厂商特定参数

        Yields:
            str: 文本片段
        """
        pass

    @abstractmethod
    def chat_stream_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs,
    ) -> Iterator[dict[str, Any]]:
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
        """运行时切换模型

        Args:
            model_name: 模型名称或别名

        Returns:
            str: 解析后的模型名称
        """
        pass

    @abstractmethod
    def get_model_display_name(self) -> str:
        """获取人类可读的模型显示名称

        Returns:
            str: 模型显示名称
        """
        pass
