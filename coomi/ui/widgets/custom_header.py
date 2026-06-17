"""Custom top navigation bar."""
from __future__ import annotations

from rich.text import Text
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

    def render(self) -> Text:
        width = max(0, self.size.width)
        title = " Coomi Agent"
        setting = "Setting "
        if width <= len(setting) + 2:
            title = ""
        elif width < len(title) + len(setting) + 1:
            title = title[:max(0, width - len(setting) - 1)]
        spacer = " " * max(1, width - len(title) - len(setting))

        text = Text()
        text.append(title, style="bold")
        text.append(spacer)
        text.append(setting, style="bold cyan")
        return text

    def on_click(self, event: events.Click) -> None:
        if event.x >= max(0, self.size.width - 10):
            event.stop()
            self.app.action_open_settings()
