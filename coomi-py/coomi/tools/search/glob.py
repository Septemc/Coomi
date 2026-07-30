"""Glob 工具 - 按模式查找文件"""
from __future__ import annotations

import os
import glob
from typing import Any

from ..base import BaseTool, ToolAccess, ToolConcurrency, ToolResult


class GlobTool(BaseTool):
    """按模式查找文件"""

    name = "Glob"
    description = "Fast file pattern matching tool that works with any codebase size."
    access = ToolAccess.READ_ONLY
    concurrency = ToolConcurrency.PARALLEL
    requires_confirmation = False

    def get_parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "The glob pattern to match files against",
                },
                "path": {
                    "type": "string",
                    "description": "The directory to search in. If not specified, the current working directory will be used.",
                },
            },
            "required": ["pattern"],
        }

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        pattern = arguments["pattern"]
        path = arguments.get("path", ".")

        try:
            full_pattern = os.path.join(path, pattern)
            matches = glob.glob(full_pattern, recursive=True)

            if not matches:
                return ToolResult(success=True, output="No files found")

            # 按修改时间排序
            matches.sort(key=lambda x: os.path.getmtime(x), reverse=True)

            output = "\n".join(matches)
            return ToolResult(success=True, output=output)
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))
