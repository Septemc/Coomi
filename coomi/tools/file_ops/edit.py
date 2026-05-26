"""Edit 工具 - 原地修改文件"""
from __future__ import annotations

from typing import Any

from ..base import BaseTool, ToolAccess, ToolConcurrency, ToolResult


class EditTool(BaseTool):
    """原地修改文件（old_string -> new_string）"""

    name = "Edit"
    description = "Performs exact string replacements in files."
    access = ToolAccess.WRITE
    concurrency = ToolConcurrency.BLOCKING
    requires_confirmation = False

    def get_parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "The absolute path to the file to modify",
                },
                "old_string": {
                    "type": "string",
                    "description": "The text to replace",
                },
                "new_string": {
                    "type": "string",
                    "description": "The text to replace it with",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences (default false)",
                },
            },
            "required": ["file_path", "old_string", "new_string"],
        }

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        file_path = arguments["file_path"]
        old_string = arguments["old_string"]
        new_string = arguments["new_string"]
        replace_all = arguments.get("replace_all", False)

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()

            # 检查 old_string 是否存在
            if old_string not in content:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"old_string not found in {file_path}",
                )

            # 检查是否唯一（如果 replace_all=False）
            if not replace_all and content.count(old_string) > 1:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"old_string appears multiple times in {file_path}. Use replace_all=true or provide more context.",
                )

            # 执行替换
            if replace_all:
                new_content = content.replace(old_string, new_string)
            else:
                new_content = content.replace(old_string, new_string, 1)

            with open(file_path, "w", encoding="utf-8") as f:
                f.write(new_content)

            return ToolResult(success=True, output=f"File {file_path} updated")
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))
