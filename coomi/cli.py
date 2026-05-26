#!/usr/bin/env python3
"""Coomi Agent CLI 入口"""
from __future__ import annotations

import sys
from pathlib import Path


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
    app.run()


if __name__ == "__main__":
    main()
