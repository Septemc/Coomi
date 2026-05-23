"""Grep 工具 - 正则搜索文件内容"""
from __future__ import annotations

import os
import re
from typing import Any

from ..base import BaseTool, ToolAccess, ToolConcurrency, ToolResult

_EXCLUDE_DIRS = {".git", "node_modules", "__pycache__", "venv", ".venv", ".next", "dist", "build", ".tox", ".eggs"}


class GrepTool(BaseTool):
    """正则搜索文件内容"""

    name = "Grep"
    description = "A powerful search tool built on regex."
    access = ToolAccess.READ_ONLY
    concurrency = ToolConcurrency.PARALLEL
    requires_confirmation = False

    def get_parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "The regular expression pattern to search for",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search in",
                },
                "glob": {
                    "type": "string",
                    "description": "Glob pattern to filter files (e.g. '*.py')",
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["content", "files_with_matches", "count"],
                    "description": "Output mode",
                },
                "-i": {
                    "type": "boolean",
                    "description": "Case insensitive search",
                },
                "-n": {
                    "type": "boolean",
                    "description": "Show line numbers",
                },
                "context": {
                    "type": "number",
                    "description": "Lines to show before and after each match",
                },
                "head_limit": {
                    "type": "number",
                    "description": "Limit output to first N lines/entries",
                },
            },
            "required": ["pattern"],
        }

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        pattern = arguments["pattern"]
        path = arguments.get("path", ".")
        glob_filter = arguments.get("glob")
        output_mode = arguments.get("output_mode", "content")
        case_insensitive = arguments.get("-i", False)
        show_line_numbers = arguments.get("-n", True)
        context = arguments.get("context", 0)
        head_limit = arguments.get("head_limit", 250)

        try:
            flags = re.IGNORECASE if case_insensitive else 0
            regex = re.compile(pattern, flags)

            results = []
            files_with_matches = []
            match_count = 0

            # 遍历文件，排除巨型目录
            for root, dirs, files in os.walk(path):
                dirs[:] = [d for d in dirs if d not in _EXCLUDE_DIRS]
                for file in files:
                    # 应用 glob 过滤
                    if glob_filter:
                        import fnmatch
                        if not fnmatch.fnmatch(file, glob_filter):
                            continue

                    file_path = os.path.join(root, file)

                    # 跳过二进制文件
                    try:
                        with open(file_path, "r", encoding="utf-8") as f:
                            lines = f.readlines()
                    except (UnicodeDecodeError, PermissionError):
                        continue

                    file_matches = []
                    for i, line in enumerate(lines, 1):
                        if regex.search(line):
                            match_count += 1
                            file_matches.append((i, line.rstrip()))

                            if output_mode == "content":
                                if show_line_numbers:
                                    results.append(f"{file_path}:{i}: {line.rstrip()}")
                                else:
                                    results.append(f"{file_path}: {line.rstrip()}")

                    if file_matches:
                        files_with_matches.append(file_path)

                    # 检查限制
                    if head_limit and len(results) >= head_limit:
                        break

                if head_limit and len(results) >= head_limit:
                    break

            # 输出结果
            if output_mode == "files_with_matches":
                output = "\n".join(files_with_matches)
            elif output_mode == "count":
                output = f"{match_count} matches in {len(files_with_matches)} files"
            else:
                output = "\n".join(results[:head_limit])

            if not output:
                output = "No matches found"

            return ToolResult(success=True, output=output)
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))
