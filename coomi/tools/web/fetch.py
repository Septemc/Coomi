"""WebFetch 工具 - 获取 URL 内容"""
from __future__ import annotations

from typing import Any

from ..base import BaseTool, ToolAccess, ToolConcurrency, ToolResult


class WebFetchTool(BaseTool):
    """获取 URL 内容"""

    name = "WebFetch"
    description = "Fetches content from a specified URL and processes it."
    access = ToolAccess.READ_ONLY
    concurrency = ToolConcurrency.PARALLEL
    requires_confirmation = False

    def get_parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "format": "uri",
                    "description": "The URL to fetch content from",
                },
                "prompt": {
                    "type": "string",
                    "description": "The prompt to run on the fetched content",
                },
            },
            "required": ["url", "prompt"],
        }

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        url = arguments["url"]
        prompt = arguments["prompt"]

        try:
            import httpx

            response = httpx.get(url, follow_redirects=True, timeout=30)
            response.raise_for_status()

            content = response.text

            # 简单返回内容，实际应用中可以用 LLM 处理
            return ToolResult(
                success=True,
                output=f"Content from {url}:\n\n{content[:5000]}",
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))
