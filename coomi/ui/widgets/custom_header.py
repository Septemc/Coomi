"""CustomHeader — 自定义顶部导航栏

替代 Textual 内置 Header，左上角齿轮图标点击打开设置面板。
"""
from __future__ import annotations

from rich.table import Table
from rich.text import Text
from textual.widget import Widget


class CustomHeader(Widget):
    """自定义 Header — ⚙ Coomi Agent"""

    DEFAULT_CSS = """
    CustomHeader {
        dock: top;
        width: 100%;
        height: 1;
        background: #0d1117;
        color: #c9d1d9;
    }

    CustomHeader:hover {
        background: #161b22;
    }
    """

    def render(self) -> Table:
        table = Table.grid(padding=(0, 0))
        table.add_column(width=3)
        table.add_column(ratio=1)
        table.add_row(Text.from_markup("[bold] ⚙ [/bold]"), "Coomi Agent")
        return table

    def on_click(self) -> None:
        self.app.action_open_settings()
