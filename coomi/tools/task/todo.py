"""TodoWrite 工具 - 管理会话任务清单"""
from __future__ import annotations

from typing import Any

from ..base import BaseTool, ToolAccess, ToolConcurrency, ToolResult


class TodoWriteTool(BaseTool):
    """管理会话任务清单"""

    name = "TodoWrite"
    description = "Use this tool to create and manage a structured task list for your current coding session."
    access = ToolAccess.WRITE
    concurrency = ToolConcurrency.BLOCKING
    requires_confirmation = False

    def __init__(self):
        self.todos: list[dict[str, Any]] = []

    def get_parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                            },
                            "activeForm": {"type": "string"},
                        },
                        "required": ["content", "status", "activeForm"],
                    },
                },
            },
            "required": ["todos"],
        }

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        todos = arguments["todos"]

        try:
            self.todos = todos

            # 格式化输出
            output_lines = []
            for todo in todos:
                status = todo["status"]
                content = todo["content"]
                if status == "completed":
                    output_lines.append(f"- [x] {content}")
                elif status == "in_progress":
                    output_lines.append(f"- [ ] {content} (in progress)")
                else:
                    output_lines.append(f"- [ ] {content}")

            output = "\n".join(output_lines)
            return ToolResult(success=True, output=output)
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))
