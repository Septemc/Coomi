"""WebSearch 工具 - 搜索网页"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import httpx

from ..base import BaseTool, ToolAccess, ToolConcurrency, ToolResult

_RESULT_RE = re.compile(
    r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>([^<]*(?:<[^/][^>]*>[^<]*</[^>]*>)?[^<]*)</a>',
    re.IGNORECASE,
)
_SNIPPET_RE = re.compile(
    r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]*>")


def _strip_tags(text: str) -> str:
    return _TAG_RE.sub("", text).strip()


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
            url = f"https://html.duckduckgo.com/html/?q={quote(query)}"
            response = httpx.get(url, follow_redirects=True, timeout=30)
            response.raise_for_status()

            html = response.text

            result_links = _RESULT_RE.findall(html)
            snippets = _SNIPPET_RE.findall(html)

            lines = [f"Search results for '{query}':\n"]
            for i, (href, title) in enumerate(result_links, 1):
                title_text = _strip_tags(title)
                snippet_text = _strip_tags(snippets[i - 1]) if i <= len(snippets) else ""
                lines.append(f"{i}. {title_text}")
                lines.append(f"   {href}")
                if snippet_text:
                    lines.append(f"   {snippet_text}")
                lines.append("")

            output = "\n".join(lines) if len(lines) > 1 else f"No results found for '{query}'"

            return ToolResult(success=True, output=output[:3000])
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))
