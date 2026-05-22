"""WebSearch 工具 - 搜索网页"""
from __future__ import annotations

from typing import Any

from ..base import BaseTool, ToolAccess, ToolConcurrency, ToolResult


class WebSearchTool(BaseTool):
    """搜索网页"""

    name = "WebSearch"
    description = "Search the web for current information."
    access = ToolAccess.READ_ONLY
    concurrency = ToolConcurrency.PARALLEL
    requires_confirmation = False

    def get_parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query",
                },
                "allowed_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Only include results from these domains",
                },
                "blocked_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Exclude results from these domains",
                },
            },
            "required": ["query"],
        }

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        query = arguments["query"]

        try:
            # 使用 DuckDuckGo 搜索（无需 API key）
            import httpx
            from urllib.parse import quote

            url = f"https://html.duckduckgo.com/html/?q={quote(query)}"
            response = httpx.get(url, follow_redirects=True, timeout=30)
            response.raise_for_status()

            # 简单解析结果
            content = response.text

            return ToolResult(
                success=True,
                output=f"Search results for '{query}':\n\n{content[:3000]}",
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))
