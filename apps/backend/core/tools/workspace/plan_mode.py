"""Plan Mode 工具 - 进入/退出规划模式"""
from __future__ import annotations

from typing import Any

from ..base import BaseTool, ToolAccess, ToolConcurrency, ToolResult


class EnterPlanModeTool(BaseTool):
    """进入规划模式"""

    name = "EnterPlanMode"
    description = "Use this tool proactively when you're about to start a non-trivial implementation task."
    access = ToolAccess.READ_ONLY
    concurrency = ToolConcurrency.BLOCKING
    requires_confirmation = False

    def get_parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(
            success=True,
            output="Plan mode entered. You can now explore the codebase and design an implementation approach.",
        )


class ExitPlanModeTool(BaseTool):
    """退出规划模式"""

    name = "ExitPlanMode"
    description = "Use this tool when you are in plan mode and have finished writing your plan."
    access = ToolAccess.WRITE
    concurrency = ToolConcurrency.BLOCKING
    requires_confirmation = True

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

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(
            success=True,
            output="Plan mode exited. Ready to implement.",
        )
