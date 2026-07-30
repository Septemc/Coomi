"""Plan Mode 工具 - 进入/退出规划模式

EnterPlanModeTool: 触发 UI 状态变更（StatusPanel、system prompt 注入 Plan 指令）
ExitPlanModeTool: 恢复正常模式
两者都走 run_async 路径以访问 app_context。
"""
from __future__ import annotations

from typing import Any

from ..base import BaseTool, ToolAccess, ToolConcurrency, ToolResult


class EnterPlanModeTool(BaseTool):
    """进入规划模式 — 通过 app_context 触发真实状态变更"""

    name = "EnterPlanMode"
    description = (
        "Use this tool proactively when you're about to start a non-trivial "
        "implementation task. Getting user sign-off on your approach before writing code "
        "prevents wasted effort and ensures alignment. This tool transitions you into "
        "plan mode where you can explore the codebase and design an implementation "
        "approach for user approval."
    )
    access = ToolAccess.READ_ONLY
    concurrency = ToolConcurrency.BLOCKING
    requires_confirmation = False

    @property
    def is_interactive(self) -> bool:
        return True

    def get_parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }

    async def run_async(self, arguments: dict[str, Any], app_context: Any = None) -> ToolResult:
        if app_context:
            await app_context._handle_plan_command()
        return ToolResult(
            success=True,
            output="Plan mode entered. You can now explore the codebase and design an implementation approach. Call ExitPlanMode when ready.",
        )

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(
            success=True,
            output="Plan mode entered. You can now explore the codebase and design an implementation approach. Call ExitPlanMode when ready.",
        )


class ExitPlanModeTool(BaseTool):
    """退出规划模式 — 通过 app_context 恢复正常模式"""

    name = "ExitPlanMode"
    description = (
        "Use this tool when you are in plan mode and have finished writing your plan. "
        "This exits plan mode and restores full read-write access so you can implement "
        "the plan."
    )
    access = ToolAccess.WRITE
    concurrency = ToolConcurrency.BLOCKING
    requires_confirmation = True

    @property
    def is_interactive(self) -> bool:
        return True

    def get_parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "allowedPrompts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool": {
                                "type": "string",
                                "enum": ["Bash"],
                                "description": "The tool this prompt applies to",
                            },
                            "prompt": {
                                "type": "string",
                                "description": "Semantic description of the action",
                            },
                        },
                        "required": ["tool", "prompt"],
                    },
                    "description": "Prompt-based permissions needed to implement the plan",
                },
            },
        }

    async def run_async(self, arguments: dict[str, Any], app_context: Any = None) -> ToolResult:
        if app_context:
            await app_context._handle_exit_plan_command()
        return ToolResult(
            success=True,
            output="Plan mode exited. Ready to implement.",
        )

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(
            success=True,
            output="Plan mode exited. Ready to implement.",
        )
