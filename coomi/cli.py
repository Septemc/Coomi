#!/usr/bin/env python3
"""Coomi Agent CLI 入口"""
from __future__ import annotations

import os
import signal
import sys
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import Iterator


def _ignore_sigint(_signum: int, _frame: FrameType | None) -> None:
    """Keep a terminal Ctrl+C signal from terminating the TUI process."""


@contextmanager
def _guard_ctrl_c() -> Iterator[None]:
    """Convert SIGINT to a no-op only while the TUI owns the terminal.

    Textual normally receives Ctrl+C as a key event. On some terminals, especially
    after the console has been idle or suspended, it may instead arrive as SIGINT.
    Guarding the process signal keeps that fallback path consistent with Coomi's
    Ctrl+C-as-copy behavior.
    """
    try:
        previous_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, _ignore_sigint)
    except (AttributeError, OSError, ValueError):
        yield
        return

    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous_handler)


def _is_mouse_enabled() -> bool:
    """Return False only when terminal mouse tracking is explicitly disabled."""
    value = os.getenv("COOMI_MOUSE", "").strip().lower()
    return value not in {"0", "false", "no", "off"}


def main():
    """主入口函数"""
    # 检查是否首次运行（无配置文件）
    config_dir = Path.home() / ".coomi" / "config"
    config_path = config_dir / "providers.json"

    if not config_path.exists():
        # 首次运行：引导配置
        from .first_run import run_first_time_setup
        if not run_first_time_setup():
            sys.exit(0)

    # 启动 TUI
    from .ui.textual_app import CoomiApp
    app = CoomiApp()
    with _guard_ctrl_c():
        app.run(mouse=_is_mouse_enabled())


if __name__ == "__main__":
    main()
