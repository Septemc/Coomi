from __future__ import annotations

import pytest

from coomi.tools.web.search import WebSearchTool


class FakeResponse:
    def __init__(self, text: str, status_error: Exception | None = None):
        self.text = text
        self._status_error = status_error

    def raise_for_status(self) -> None:
        if self._status_error is not None:
            raise self._status_error


def test_web_search_parses_duckduckgo_lite(monkeypatch: pytest.MonkeyPatch):
    html = """
    <html><body>
      <a class="result-link" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">
        Example &amp; Result
      </a>
      <td class="result-snippet">A useful snippet</td>
    </body></html>
    """

    def fake_get(*args, **kwargs):
        return FakeResponse(html)

    monkeypatch.setattr("coomi.tools.web.search.httpx.get", fake_get)

    result = WebSearchTool().run({"query": "example"})

    assert result.success
    assert "Example & Result" in result.output
    assert "https://example.com/a" in result.output
    assert "A useful snippet" in result.output


def test_web_search_falls_back_to_bing(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []
    bing_html = """
    <html><body>
      <li class="b_algo">
        <h2><a href="https://news.example.com/story">News Story</a></h2>
        <p>Current news snippet</p>
      </li>
    </body></html>
    """

    def fake_get(url, *args, **kwargs):
        calls.append(url)
        if "bing.com" in url:
            return FakeResponse(bing_html)
        return FakeResponse("<html><body>No results</body></html>")

    monkeypatch.setattr("coomi.tools.web.search.httpx.get", fake_get)

    result = WebSearchTool().run({"query": "general query"})

    assert result.success
    assert len(calls) == 3
    assert "from Bing" in result.output
    assert "News Story" in result.output
    assert "Current news snippet" in result.output


def test_web_search_uses_sogou_before_bing_for_news_queries(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []
    sogou_html = """
    <html><body>
      <h3 class="vr-title">
        <a href="/link?url=abc"><em>Donald Trump</em> latest news</a>
      </h3>
      <div class="fz-mid">Current political news snippet</div>
    </body></html>
    """

    def fake_get(url, *args, **kwargs):
        calls.append(url)
        if "sogou.com" in url:
            return FakeResponse(sogou_html)
        return FakeResponse("<html><body>No results</body></html>")

    monkeypatch.setattr("coomi.tools.web.search.httpx.get", fake_get)

    result = WebSearchTool().run({"query": "Donald Trump latest news"})

    assert result.success
    assert len(calls) == 1
    assert "from Sogou" in result.output
    assert "Donald Trump latest news" in result.output
    assert "Current political news snippet" in result.output


def test_web_search_uses_sogou_before_bing_for_chinese_queries(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []
    sogou_html = """
    <html><body>
      <h3 class="vr-title">
        <a href="https://weather.example.com/chengdu"><em>成都天气预报</em>_明天天气</a>
      </h3>
      <div class="fz-mid">明天 多云 22~30℃</div>
    </body></html>
    """

    def fake_get(url, *args, **kwargs):
        calls.append(url)
        if "sogou.com" in url:
            return FakeResponse(sogou_html)
        return FakeResponse("<html><body>No results</body></html>")

    monkeypatch.setattr("coomi.tools.web.search.httpx.get", fake_get)

    result = WebSearchTool().run({"query": "成都天气预报 明天"})

    assert result.success
    assert len(calls) == 1
    assert "from Sogou" in result.output
    assert "成都天气预报_明天天气" in result.output
    assert "明天 多云 22~30℃" in result.output


def test_web_search_filters_allowed_and_blocked_domains(monkeypatch: pytest.MonkeyPatch):
    html = """
    <html><body>
      <a class="result-link" href="https://allowed.example.com/a">Allowed</a>
      <td class="result-snippet">Keep this</td>
      <a class="result-link" href="https://blocked.example.com/b">Blocked</a>
      <td class="result-snippet">Drop this</td>
      <a class="result-link" href="https://other.example.net/c">Other</a>
      <td class="result-snippet">Drop this too</td>
    </body></html>
    """

    def fake_get(*args, **kwargs):
        return FakeResponse(html)

    monkeypatch.setattr("coomi.tools.web.search.httpx.get", fake_get)

    result = WebSearchTool().run(
        {
            "query": "domain filters",
            "allowed_domains": ["example.com"],
            "blocked_domains": ["blocked.example.com"],
        }
    )

    assert result.success
    assert "Allowed" in result.output
    assert "Blocked" not in result.output
    assert "Other" not in result.output


def test_web_search_reports_provider_failures(monkeypatch: pytest.MonkeyPatch):
    def fake_get(*args, **kwargs):
        return FakeResponse("", RuntimeError("network unavailable"))

    monkeypatch.setattr("coomi.tools.web.search.httpx.get", fake_get)

    result = WebSearchTool().run({"query": "anything"})

    assert not result.success
    assert "DuckDuckGo Lite: network unavailable" in (result.error or "")
    assert "DuckDuckGo HTML: network unavailable" in (result.error or "")
    assert "Bing: network unavailable" in (result.error or "")
