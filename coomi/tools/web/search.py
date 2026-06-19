"""WebSearch 工具 - 搜索网页"""
from __future__ import annotations

from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

import httpx

from ..base import BaseTool, ToolAccess, ToolConcurrency, ToolResult


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""


def _normalize_text(value: str) -> str:
    return " ".join(unescape(value).split())


def _attrs_to_dict(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
    return {name.lower(): value or "" for name, value in attrs}


def _class_contains(class_attr: str, *names: str) -> bool:
    classes = {item.strip().lower() for item in class_attr.split() if item.strip()}
    return any(name.lower() in classes for name in names)


def _normalize_url(href: str, base_url: str) -> str:
    href = unescape(href.strip())
    if not href:
        return ""

    absolute = urljoin(base_url, href)
    parsed = urlparse(absolute)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        uddg = parse_qs(parsed.query).get("uddg")
        if uddg:
            return unquote(uddg[0])
    return absolute


def _hostname(url: str) -> str:
    return urlparse(url).hostname.lower().removeprefix("www.") if urlparse(url).hostname else ""


def _domain_matches(hostname: str, domain: str) -> bool:
    domain = domain.lower().strip().removeprefix("www.")
    if not domain:
        return False
    return hostname == domain or hostname.endswith(f".{domain}")


def _filter_results(
    results: list[SearchResult],
    allowed_domains: list[str],
    blocked_domains: list[str],
) -> list[SearchResult]:
    filtered: list[SearchResult] = []
    seen: set[str] = set()

    for result in results:
        host = _hostname(result.url)
        if not host:
            continue
        if allowed_domains and not any(_domain_matches(host, domain) for domain in allowed_domains):
            continue
        if blocked_domains and any(_domain_matches(host, domain) for domain in blocked_domains):
            continue
        if result.url in seen:
            continue
        seen.add(result.url)
        filtered.append(result)
    return filtered


def _contains_cjk(value: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in value)


def _looks_like_news_query(value: str) -> bool:
    lowered = value.lower()
    news_terms = ("news", "latest", "today", "breaking", "新闻", "最新", "今日", "今天")
    return any(term in lowered for term in news_terms)


class DuckDuckGoParser(HTMLParser):
    """Parse DuckDuckGo lite/html result pages."""

    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.results: list[SearchResult] = []
        self._link_href: str | None = None
        self._link_parts: list[str] = []
        self._snippet_parts: list[str] | None = None
        self._snippet_tag = ""
        self._snippet_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = _attrs_to_dict(attrs)
        class_attr = attr.get("class", "")
        if tag == "a" and _class_contains(class_attr, "result__a", "result-link"):
            self._link_href = _normalize_url(attr.get("href", ""), self.base_url)
            self._link_parts = []
            return

        if _class_contains(class_attr, "result__snippet", "result-snippet"):
            self._snippet_parts = []
            self._snippet_tag = tag
            self._snippet_depth = 0
            return

        if self._snippet_parts is not None:
            self._snippet_depth += 1

    def handle_data(self, data: str) -> None:
        if self._link_href is not None:
            self._link_parts.append(data)
        if self._snippet_parts is not None:
            self._snippet_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._link_href is not None:
            title = _normalize_text("".join(self._link_parts))
            if title and self._link_href:
                self.results.append(SearchResult(title=title, url=self._link_href))
            self._link_href = None
            self._link_parts = []
            return

        if self._snippet_parts is not None:
            if self._snippet_depth > 0:
                self._snippet_depth -= 1
                return
            if tag == self._snippet_tag:
                snippet = _normalize_text("".join(self._snippet_parts))
                for index in range(len(self.results) - 1, -1, -1):
                    if not self.results[index].snippet:
                        result = self.results[index]
                        self.results[index] = SearchResult(result.title, result.url, snippet)
                        break
                self._snippet_parts = None
                self._snippet_tag = ""


class BingParser(HTMLParser):
    """Parse Bing HTML result pages."""

    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.results: list[SearchResult] = []
        self._inside_result = False
        self._inside_h2 = False
        self._link_href: str | None = None
        self._link_parts: list[str] = []
        self._snippet_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = _attrs_to_dict(attrs)
        if tag == "li" and _class_contains(attr.get("class", ""), "b_algo"):
            self._inside_result = True
            return

        if not self._inside_result:
            return

        if tag == "h2":
            self._inside_h2 = True
            return

        if (
            tag == "a"
            and self._inside_h2
            and self._link_href is None
            and attr.get("href", "").startswith("http")
        ):
            self._link_href = _normalize_url(attr.get("href", ""), self.base_url)
            self._link_parts = []
            return

        if tag == "p" and self.results and self._snippet_parts is None:
            self._snippet_parts = []

    def handle_data(self, data: str) -> None:
        if self._link_href is not None:
            self._link_parts.append(data)
        if self._snippet_parts is not None:
            self._snippet_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._link_href is not None:
            title = _normalize_text("".join(self._link_parts))
            if title and self._link_href:
                self.results.append(SearchResult(title=title, url=self._link_href))
            self._link_href = None
            self._link_parts = []
            return

        if tag == "p" and self._snippet_parts is not None:
            snippet = _normalize_text("".join(self._snippet_parts))
            if snippet and self.results:
                result = self.results[-1]
                if not result.snippet:
                    self.results[-1] = SearchResult(result.title, result.url, snippet)
            self._snippet_parts = None
            return

        if tag == "h2" and self._inside_h2:
            self._inside_h2 = False
            return

        if tag == "li" and self._inside_result:
            self._inside_result = False
            self._inside_h2 = False
            self._snippet_parts = None


class SogouParser(HTMLParser):
    """Parse Sogou result pages for Chinese queries."""

    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.results: list[SearchResult] = []
        self._inside_title = False
        self._link_href: str | None = None
        self._link_parts: list[str] = []
        self._snippet_parts: list[str] | None = None
        self._snippet_tag = ""
        self._snippet_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = _attrs_to_dict(attrs)
        class_attr = attr.get("class", "")
        if tag == "h3" and (_class_contains(class_attr, "vr-title") or not self._inside_title):
            self._inside_title = True
            return

        if tag == "a" and self._inside_title and self._link_href is None:
            self._link_href = _normalize_url(attr.get("href", ""), self.base_url)
            self._link_parts = []
            return

        if _class_contains(class_attr, "fz-mid", "str-info", "w-desc"):
            self._snippet_parts = []
            self._snippet_tag = tag
            self._snippet_depth = 0
            return

        if self._snippet_parts is not None:
            self._snippet_depth += 1

    def handle_data(self, data: str) -> None:
        if self._link_href is not None:
            self._link_parts.append(data)
        if self._snippet_parts is not None:
            self._snippet_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._link_href is not None:
            title = _normalize_text("".join(self._link_parts))
            if title and self._link_href:
                self.results.append(SearchResult(title=title, url=self._link_href))
            self._link_href = None
            self._link_parts = []
            return

        if tag == "h3" and self._inside_title:
            self._inside_title = False
            return

        if self._snippet_parts is not None:
            if self._snippet_depth > 0:
                self._snippet_depth -= 1
                return
            if tag == self._snippet_tag:
                snippet = _normalize_text("".join(self._snippet_parts))
                if snippet and self.results:
                    result = self.results[-1]
                    if not result.snippet:
                        self.results[-1] = SearchResult(result.title, result.url, snippet)
                self._snippet_parts = None
                self._snippet_tag = ""


def _parse_duckduckgo(html: str, base_url: str) -> list[SearchResult]:
    parser = DuckDuckGoParser(base_url)
    parser.feed(html)
    return parser.results


def _parse_bing(html: str, base_url: str) -> list[SearchResult]:
    parser = BingParser(base_url)
    parser.feed(html)
    return parser.results


def _parse_sogou(html: str, base_url: str) -> list[SearchResult]:
    parser = SogouParser(base_url)
    parser.feed(html)
    return parser.results


class WebSearchTool(BaseTool):
    """搜索网页"""

    name = "WebSearch"
    description = (
        "Search the live web for current, recent, or location-specific public information, "
        "including weather forecasts, news, prices, releases, and facts that may have changed."
    )
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
        query = str(arguments["query"]).strip()
        if not query:
            return ToolResult(success=False, output="", error="Search query cannot be empty")

        allowed_domains = [str(item) for item in arguments.get("allowed_domains") or []]
        blocked_domains = [str(item) for item in arguments.get("blocked_domains") or []]
        encoded_query = quote_plus(query)
        is_cjk_query = _contains_cjk(query)
        prefer_sogou = is_cjk_query or _looks_like_news_query(query)
        headers = dict(_HEADERS)
        if not is_cjk_query:
            headers["Accept-Language"] = "en-US,en;q=0.9,zh;q=0.8"
        duckduckgo_providers = [
            (
                "DuckDuckGo Lite",
                f"https://lite.duckduckgo.com/lite/?q={encoded_query}",
                _parse_duckduckgo,
            ),
            (
                "DuckDuckGo HTML",
                f"https://html.duckduckgo.com/html/?q={encoded_query}",
                _parse_duckduckgo,
            ),
        ]
        bing_url = (
            f"https://www.bing.com/search?q={encoded_query}&cc=cn&setlang=zh-Hans&mkt=zh-CN"
            if is_cjk_query
            else f"https://www.bing.com/search?q={encoded_query}&cc=us&setlang=en-US&mkt=en-US"
        )
        bing_provider = (
            "Bing",
            bing_url,
            _parse_bing,
        )
        sogou_provider = (
            "Sogou",
            f"https://www.sogou.com/web?query={encoded_query}",
            _parse_sogou,
        )
        providers = (
            [*duckduckgo_providers, sogou_provider, bing_provider]
            if prefer_sogou
            else [*duckduckgo_providers, bing_provider, sogou_provider]
        )

        errors: list[str] = []
        for provider_name, url, parser in providers:
            try:
                response = httpx.get(url, headers=headers, follow_redirects=True, timeout=30)
                response.raise_for_status()
                results = parser(response.text, url)
                results = _filter_results(results, allowed_domains, blocked_domains)
            except Exception as exc:
                errors.append(f"{provider_name}: {exc}")
                continue

            if results:
                return ToolResult(success=True, output=self._format_results(query, provider_name, results))
            errors.append(f"{provider_name}: no parseable results")

        return ToolResult(
            success=False,
            output="",
            error=f"Search failed for '{query}'. " + "; ".join(errors),
        )

    def _format_results(
        self,
        query: str,
        provider_name: str,
        results: list[SearchResult],
    ) -> str:
        lines = [f"Search results for '{query}' from {provider_name}:\n"]
        for index, result in enumerate(results[:8], 1):
            lines.append(f"{index}. {result.title}")
            lines.append(f"   {result.url}")
            if result.snippet:
                lines.append(f"   {result.snippet}")
            lines.append("")
        return "\n".join(lines)[:3000]
