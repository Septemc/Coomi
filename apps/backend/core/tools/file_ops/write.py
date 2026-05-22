"""Write 工具 - 创建或覆盖文件"""
from __future__ import annotations

import os
from typing import Any

from ..base import BaseTool, ToolAccess, ToolConcurrency, ToolResult


class WriteTool(BaseTool):
    """创建或覆盖文件"""

    name = "Write"
    description = "Writes a file to the local filesystem."
    access = ToolAccess.WRITE
    concurrency = ToolConcurrency.BLOCKING
    requires_confirmation = False

    def get_parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "The absolute path to the file to write",
                },
                "content": {
                    "type": "string",
                    "description": "The content to write to the file",
                },
            },
            "required": ["file_path", "content"],
        }

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        file_path = arguments["file_path"]
        content = arguments["content"]

        try:
            # 确保目录存在
            os.makedirs(os.path.dirname(file_path), exist_ok=True)

            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)

            return ToolResult(success=True, output=f"File written to {file_path}")
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))
