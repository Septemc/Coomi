from __future__ import annotations

import pytest

from coomi.engine.tool_executor import ToolExecutor
from coomi.security import PermissionMode, PermissionSystem
from coomi.tools.registry import ToolRegistry
from coomi.tools.web.fetch import WebFetchTool
from coomi.types import Session, ToolCall


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


def test_web_fetch_prompt_is_optional_in_schema():
    schema = WebFetchTool().get_parameters_schema()

    assert schema["required"] == ["url"]
    assert "prompt" in schema["properties"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"url": "https://example.com/page"},
        {"url": "https://example.com/page", "prompt": "read"},
    ],
)
async def test_web_fetch_executor_accepts_url_only_and_optional_prompt(
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, str],
):
    def fake_get(*args, **kwargs):
        return FakeResponse(
            "<html><body><p>Hello from executor</p></body></html>",
            headers={"content-type": "text/html; charset=utf-8"},
        )

    monkeypatch.setattr("coomi.tools.web.fetch.httpx.get", fake_get)
    registry = ToolRegistry()
    registry.register(WebFetchTool())
    permissions = PermissionSystem()
    permissions.set_mode(PermissionMode.FULL_ACCESS)
    executor = ToolExecutor(registry, permission_system=permissions)
    session = Session(id="s")

    outcome = await executor.execute(
        session,
        ToolCall(id="call_1", name="WebFetch", arguments=arguments),
    )

    assert not outcome.is_error
    assert "Hello from executor" in outcome.result_text
