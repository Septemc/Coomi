"""Read 工具 - 读取文件内容"""
from __future__ import annotations

from typing import Any

from ..base import BaseTool, ToolAccess, ToolConcurrency, ToolResult


class ReadTool(BaseTool):
    """读取文件内容"""

    name = "Read"
    description = "Reads a file from the local filesystem. You can access any file directly by using this tool."
    access = ToolAccess.READ_ONLY
    concurrency = ToolConcurrency.PARALLEL
    requires_confirmation = False

    def get_parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "The absolute path to the file to read",
                },
                "offset": {
                    "type": "integer",
                    "description": "The line number to start reading from. Only provide if the file is too large to read at once",
                },
                "limit": {
                    "type": "integer",
                    "description": "The number of lines to read. Only provide if the file is too large to read at once",
                },
            },
            "required": ["file_path"],
        }

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        file_path = arguments["file_path"]
        offset = arguments.get("offset", 0)
        limit = arguments.get("limit", 2000)

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            # 应用 offset 和 limit
            selected_lines = lines[offset:offset + limit]

            # 添加行号
            content = ""
            for i, line in enumerate(selected_lines, start=offset + 1):
                content += f"{i}\t{line}"

            return ToolResult(success=True, output=content)
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))
