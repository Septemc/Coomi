"""WebFetch 工具 - 获取 URL 内容"""
from __future__ import annotations

from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

import httpx

from ..base import BaseTool, ToolAccess, ToolConcurrency, ToolResult


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.7",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
_SOFT_BLOCK_STATUSES = {401, 403, 429}


class _ReadableHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg", "canvas"}:
            self._skip_depth += 1
        elif tag.lower() in {"p", "div", "section", "article", "br", "li", "tr", "h1", "h2", "h3"}:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg", "canvas"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag.lower() in {"p", "div", "section", "article", "li", "tr", "h1", "h2", "h3"}:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = " ".join(unescape(data).split())
        if text:
            self._parts.append(text)
            self._parts.append(" ")

    def text(self) -> str:
        return _compact_text("".join(self._parts))


class WebFetchTool(BaseTool):
    """获取 URL 内容"""

    name = "WebFetch"
    description = "Fetches readable content from a specified URL."
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
                    "description": "Optional fetch intent or processing hint for the fetched content",
                },
            },
            "required": ["url"],
        }

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        url = str(arguments["url"]).strip()
        if not url:
            return ToolResult(success=False, output="", error="URL cannot be empty")

        try:
            headers = dict(_HEADERS)
            origin = _origin(url)
            if origin:
                headers["Referer"] = origin + "/"

            response = httpx.get(url, headers=headers, follow_redirects=True, timeout=30)
            if response.status_code in _SOFT_BLOCK_STATUSES:
                return ToolResult(success=True, output=_blocked_output(url, response.status_code))
            response.raise_for_status()

            content_type = response.headers.get("content-type", "")
            content = _extract_readable_text(response.text, content_type)
            if not content:
                content = "[No readable text extracted from this page.]"

            return ToolResult(
                success=True,
                output=f"Content from {url}:\n\n{content[:5000]}",
            )
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if status_code in _SOFT_BLOCK_STATUSES:
                return ToolResult(success=True, output=_blocked_output(url, status_code))
            return ToolResult(success=False, output="", error=str(exc))
        except Exception as exc:
            return ToolResult(success=False, output="", error=str(exc))


def _origin(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def _blocked_output(url: str, status_code: int) -> str:
    return (
        f"Fetch blocked for {url}: HTTP {status_code}.\n"
        "The site rejected automated fetching. Do not retry the same URL repeatedly. "
        "Use WebSearch snippets, another search result, or an official/API source instead."
    )


def _extract_readable_text(content: str, content_type: str) -> str:
    if "html" not in content_type.lower() and not content.lstrip().startswith("<"):
        return _compact_text(content)
    parser = _ReadableHTMLParser()
    parser.feed(content)
    return parser.text()


def _compact_text(value: str) -> str:
    lines = [" ".join(line.split()) for line in value.splitlines()]
    compacted: list[str] = []
    previous_blank = False
    for line in lines:
        if not line:
            if not previous_blank:
                compacted.append("")
            previous_blank = True
            continue
        compacted.append(line)
        previous_blank = False
    return "\n".join(compacted).strip()
