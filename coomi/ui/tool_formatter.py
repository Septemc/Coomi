"""工具调用详情格式化 - 按工具类型定制显示"""
from __future__ import annotations

from typing import Any


def format_tool_display(name: str, arguments: dict[str, Any] | None = None) -> str:
    """按工具类型格式化显示文本

    Args:
        name: 工具名称
        arguments: 工具参数字典

    Returns:
        格式化后的显示字符串
    """
    args = arguments or {}

    if name == "Read":
        file_path = args.get("file_path", "")
        offset = args.get("offset")
        limit = args.get("limit")
        if file_path:
            if offset is not None and limit is not None:
                return f"Read {file_path} (lines {offset}-{offset + limit - 1})"
            elif offset is not None:
                return f"Read {file_path} (from line {offset})"
            return f"Read {file_path}"
        return "Read"

    elif name in ("Write", "Edit"):
        file_path = args.get("file_path", "")
        if file_path:
            return f"{name} {file_path}"
        return name

    elif name in ("Bash", "PowerShell"):
        command = args.get("command", "")
        if command:
            cmd_short = command[:80] + "..." if len(command) > 80 else command
            return f"{name}: {cmd_short}"
        return name

    elif name == "Glob":
        pattern = args.get("pattern", "")
        if pattern:
            return f"Glob: {pattern}"
        return "Glob"

    elif name == "Grep":
        pattern = args.get("pattern", "")
        glob = args.get("glob", "")
        if pattern:
            if glob:
                return f"Grep: \"{pattern}\" in {glob}"
            return f"Grep: \"{pattern}\""
        return "Grep"

    elif name == "WebFetch":
        url = args.get("url", "")
        if url:
            # 截断长URL，保留前60字符
            url_short = url[:60] + "..." if len(url) > 60 else url
            return f"WebFetch: {url_short}"
        return "WebFetch"

    elif name == "WebSearch":
        query = args.get("query", "")
        if query:
            return f"WebSearch: \"{query}\""
        return "WebSearch"

    elif name == "TodoWrite":
        todos = args.get("todos", [])
        count = len(todos) if isinstance(todos, list) else 0
        return f"TodoWrite: {count} 个任务"

    elif name == "Agent":
        description = args.get("description", "")
        desc_short = description[:60] + "..." if len(description) > 60 else description
        return f"Agent: {desc_short}" if desc_short else "Agent"

    elif name == "AskUserQuestion":
        questions = args.get("questions", [])
        count = len(questions) if isinstance(questions, list) else 0
        return f"AskUserQuestion: {count} 个问题"

    elif name == "CronCreate":
        prompt = args.get("prompt", "")
        desc_short = prompt[:60] + "..." if len(prompt) > 60 else prompt
        return f"CronCreate: {desc_short}" if desc_short else "CronCreate"

    else:
        return name
