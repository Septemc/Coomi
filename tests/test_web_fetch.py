from __future__ import annotations

import pytest

from coomi.tools.web.fetch import WebFetchTool


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200, headers: dict[str, str] | None = None):
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"{self.status_code} error")


def test_web_fetch_extracts_readable_html(monkeypatch: pytest.MonkeyPatch):
    html = """
    <html>
      <head><style>.x{}</style><script>alert(1)</script></head>
      <body><h1>Title</h1><p>Hello <strong>world</strong>.</p></body>
    </html>
    """

    def fake_get(*args, **kwargs):
        return FakeResponse(html, headers={"content-type": "text/html; charset=utf-8"})

    monkeypatch.setattr("coomi.tools.web.fetch.httpx.get", fake_get)

    result = WebFetchTool().run({"url": "https://example.com/page", "prompt": "read"})

    assert result.success
    assert "Title" in result.output
    assert "Hello world ." in result.output
    assert "alert" not in result.output
    assert "<html>" not in result.output


def test_web_fetch_soft_blocks_forbidden(monkeypatch: pytest.MonkeyPatch):
    def fake_get(*args, **kwargs):
        return FakeResponse("forbidden", status_code=403)

    monkeypatch.setattr("coomi.tools.web.fetch.httpx.get", fake_get)

    result = WebFetchTool().run({"url": "https://blocked.example.com", "prompt": "read"})

    assert result.success
    assert "HTTP 403" in result.output
    assert "Do not retry the same URL repeatedly" in result.output
