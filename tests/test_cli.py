from __future__ import annotations

import pytest

from coomi.cli import _is_mouse_enabled


def test_mouse_tracking_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("COOMI_MOUSE", raising=False)

    assert _is_mouse_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", " TRUE "])
def test_mouse_tracking_can_be_enabled_explicitly(
    monkeypatch: pytest.MonkeyPatch, value: str
):
    monkeypatch.setenv("COOMI_MOUSE", value)

    assert _is_mouse_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_mouse_tracking_ignores_disabled_values(
    monkeypatch: pytest.MonkeyPatch, value: str
):
    monkeypatch.setenv("COOMI_MOUSE", value)

    assert _is_mouse_enabled() is False
