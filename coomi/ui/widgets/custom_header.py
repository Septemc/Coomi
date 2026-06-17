"""Custom top navigation bar."""
from __future__ import annotations

from rich.table import Table
from textual import events
from textual.widget import Widget


class CustomHeader(Widget):
    """Top bar with a static title and a right-aligned settings entry."""

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
        table.add_column(ratio=1)
        table.add_column(width=10, justify="right")
        table.add_row("[bold] Coomi Agent[/bold]", "[bold cyan]Setting[/bold cyan] ")
        return table

    def on_click(self, event: events.Click) -> None:
        if event.x >= max(0, self.size.width - 10):
            event.stop()
            self.app.action_open_settings()
