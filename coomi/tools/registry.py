"""工具注册表"""
from __future__ import annotations

import asyncio
import re
from typing import Any

from .base import BaseTool, ToolResult
from ..types import ToolCall


class ToolRegistry:
    """工具注册表 - 管理所有可用工具"""

    def __init__(self):
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """注册工具"""
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool | None:
        """获取工具"""
        canonical = self.canonical_name(name)
        if canonical:
            return self._tools.get(canonical)
        return None

    def canonical_name(self, name: str) -> str | None:
        """Return the registered tool name for exact, case, or snake_case aliases."""
        if name in self._tools:
            return name
        normalized = _normalize_tool_name(name)
        for tool_name in self._tools:
            if _normalize_tool_name(tool_name) == normalized:
                return tool_name
        return None

    def list_tools(self) -> list[BaseTool]:
        """列出所有工具"""
        return list(self._tools.values())

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        """获取所有工具的OpenAI格式定义"""
        return [tool.to_openai_tool() for tool in self._tools.values()]

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        """执行工具调用（异步，在线程池中运行同步工具）"""
        tool = self.get(tool_call.name)
        if tool is None:
            return ToolResult(
                success=False,
                output="",
                error=f"Tool '{tool_call.name}' not found",
            )

        try:
            result = await asyncio.to_thread(tool.run, tool_call.arguments)
            return result
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Tool execution failed: {str(e)}",
            )

    def execute_sync(self, tool_call: ToolCall) -> ToolResult:
        """执行工具调用（同步）"""
        tool = self.get(tool_call.name)
        if tool is None:
            return ToolResult(
                success=False,
                output="",
                error=f"Tool '{tool_call.name}' not found",
            )

        try:
            result = tool.run(tool_call.arguments)
            return result
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Tool execution failed: {str(e)}",
            )


def create_default_registry() -> ToolRegistry:
    """创建默认的工具注册表，注册所有核心工具"""
    from .file_ops import ReadTool, WriteTool, EditTool
    from .search import GlobTool, GrepTool
    from .shell import BashTool, PowerShellTool
    from .web import WebFetchTool, WebSearchTool
    from .task import TodoWriteTool
    from .agent import AgentTool
    from .user import AskUserQuestionTool
    from .workspace import EnterPlanModeTool, ExitPlanModeTool
    from .config import ConfigTool

    registry = ToolRegistry()

    # 注册核心工具
    registry.register(ReadTool())
    registry.register(WriteTool())
    registry.register(EditTool())
    registry.register(GlobTool())
    registry.register(GrepTool())
    registry.register(BashTool())
    registry.register(PowerShellTool())
    registry.register(WebFetchTool())
    registry.register(WebSearchTool())
    registry.register(TodoWriteTool())
    registry.register(AgentTool())
    registry.register(AskUserQuestionTool())
    registry.register(EnterPlanModeTool())
    registry.register(ExitPlanModeTool())
    registry.register(ConfigTool())

    return registry


def _normalize_tool_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").casefold())
