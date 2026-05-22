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
                    "description": "The prompt for the subagent",
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
            "required": ["description", "prompt"],
        }

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        description = arguments["description"]
        prompt = arguments["prompt"]

        try:
            # 简化实现：直接返回提示信息
            # 实际应用中需要创建子 Agent 并执行
            return ToolResult(
                success=True,
                output=f"Subagent task '{description}' would be executed with prompt:\n{prompt}",
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))
