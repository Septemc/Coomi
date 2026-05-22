"""工具基类"""
from __future__ import annotations

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

    @abstractmethod
    def get_parameters_schema(self) -> dict[str, Any]:
        """获取参数JSON Schema（OpenAI格式）"""
        pass

    @abstractmethod
    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """执行工具"""
        pass

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
