"""工具基类"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ToolAccess(Enum):
    """工具访问权限"""
    READ_ONLY = "read_only"       # 只读
    WRITE = "write"               # 会修改
    DESTRUCTIVE = "destructive"   # 破坏性，需额外确认


class ToolConcurrency(Enum):
    """工具并发性"""
    BLOCKING = "blocking"         # 串行执行
    PARALLEL = "parallel"         # 可并行


@dataclass
class ToolResult:
    """工具执行结果"""
    success: bool
    output: str
    error: str | None = None


class BaseTool(ABC):
    """工具基类 - 所有工具必须继承"""

    name: str
    description: str
    access: ToolAccess
    concurrency: ToolConcurrency
    requires_confirmation: bool

    @property
    def is_interactive(self) -> bool:
        """交互式工具需要用户输入，走 async 路径而非线程池"""
        return False

    @abstractmethod
    def get_parameters_schema(self) -> dict[str, Any]:
        """获取参数JSON Schema（OpenAI格式）"""
        pass

    @abstractmethod
    def run(self, arguments: dict[str, Any]) -> ToolResult:
        """执行工具（同步方法，由调用方通过 asyncio.to_thread 在线程池中执行）"""
        pass

    async def run_async(self, arguments: dict[str, Any], app_context: Any = None) -> ToolResult:
        """交互式工具的 async 执行入口，默认回退到同步 run()"""
        return await asyncio.to_thread(self.run, arguments)

    def to_openai_tool(self) -> dict[str, Any]:
        """转换为OpenAI工具定义格式"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.get_parameters_schema(),
            },
        }
