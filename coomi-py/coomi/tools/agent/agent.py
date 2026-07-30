"""Agent 工具 - 创建子 Agent"""
from __future__ import annotations

from typing import Any

from ..base import BaseTool, ToolAccess, ToolConcurrency, ToolResult


class AgentTool(BaseTool):
    """创建子 Agent 执行任务"""

    name = "Agent"
    description = "Launch a new agent to handle complex, multi-step tasks."
    access = ToolAccess.WRITE
    concurrency = ToolConcurrency.PARALLEL
    requires_confirmation = False

    def get_parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "A short description of the task",
                },
                "prompt": {
                    "type": "string",
                    "description": (
                        "Detailed instructions for the subagent. If omitted, Coomi uses "
                        "the description as the prompt."
                    ),
                },
                "model": {
                    "type": "string",
                    "enum": ["sonnet", "opus", "haiku"],
                    "description": "Model to use for the subagent",
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": "Run the subagent in the background",
                },
            },
            "required": ["description"],
        }

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        description = str(arguments.get("description") or "").strip()
        prompt = str(arguments.get("prompt") or description).strip()
        requested = prompt or description or "(empty)"
        return ToolResult(
            success=False,
            output="",
            error=(
                "Agent/Task delegation is recognized, but sub-agent execution is not "
                "implemented yet. Continue by performing the task directly in the "
                "current agent session, or implement the sub-agent runner before "
                f"delegating. Requested task: {requested}"
            ),
        )
