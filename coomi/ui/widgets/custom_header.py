"""Custom top navigation bar."""
from __future__ import annotations

import os

from rich.text import Text
from textual import events
from textual.widget import Widget


class CustomHeader(Widget):
    """Top bar with title, current path, and a right-aligned settings entry."""

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

    TITLE = " Coomi Agent"
    SETTING = "Setting "
    TITLE_PATH_GAP = 2
    PATH_SETTING_GAP = 3

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._path_start = 0
        self._path_end = 0
        self._setting_start = 0
        self._display_path = ""
        self._is_selecting_path = False
        self._selection_start: int | None = None
        self._selection_end: int | None = None

    def render(self) -> Text:
        width = max(0, self.size.width)
        title = self.TITLE
        setting = self.SETTING

        if width <= len(setting) + 2:
            title = ""
        elif width < len(title) + len(setting) + 1:
            title = title[:max(0, width - len(setting) - 1)]

        path_budget = max(
            0,
            width
            - len(title)
            - self.TITLE_PATH_GAP
            - self.PATH_SETTING_GAP
            - len(setting),
        )
        full_path = self._current_path()
        display_path = _middle_ellipsis(full_path, path_budget)

        self._path_start = len(title) + self.TITLE_PATH_GAP
        self._path_end = self._path_start + len(display_path)
        self._setting_start = max(0, width - len(setting))
        self._display_path = display_path

        between_path_and_setting = max(1, self._setting_start - self._path_end)

        text = Text()
        text.append(title, style="bold cyan")
        text.append(" " * self.TITLE_PATH_GAP)
        self._append_path(text, display_path)
        text.append(" " * between_path_and_setting)
        text.append(setting, style="bold cyan")
        return text

    def _append_path(self, text: Text, display_path: str) -> None:
        if not display_path:
            return
        if not self.has_selection():
            text.append(display_path, style="white")
            return

        start, end = sorted((self._selection_start or 0, self._selection_end or 0))
        start = max(0, min(start, len(display_path)))
        end = max(0, min(end, len(display_path)))
        text.append(display_path[:start], style="white")
        text.append(display_path[start:end], style="white on #264f78")
        text.append(display_path[end:], style="white")

    def _current_path(self) -> str:
        cwd = getattr(self.app, "_cwd", "") or os.getcwd()
        return os.path.abspath(cwd)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if self._path_start <= event.x < self._path_end:
            self._is_selecting_path = True
            rel = self._path_rel(event.x)
            self._selection_start = rel
            self._selection_end = rel
            self.capture_mouse()
            event.stop()
            self.refresh()
        elif event.x < self._setting_start:
            self.clear_selection()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self._is_selecting_path:
            return
        self._selection_end = self._path_rel(event.x)
        event.stop()
        self.refresh()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self._is_selecting_path:
            self._selection_end = self._path_rel(event.x)
            self._is_selecting_path = False
            self.release_mouse()
            event.stop()
            self.refresh()

    def on_click(self, event: events.Click) -> None:
        if event.x >= self._setting_start:
            event.stop()
            self.app.action_open_settings()

    def _path_rel(self, x: int) -> int:
        return max(0, min(x - self._path_start, len(self._display_path)))

    def get_selected_text(self) -> str | None:
        if not self.has_selection():
            return None
        start, end = sorted((self._selection_start or 0, self._selection_end or 0))
        selected = self._display_path[start:end]
        if selected == self._display_path and "..." in self._display_path:
            return self._current_path()
        return selected or None

    def clear_selection(self) -> None:
        self._is_selecting_path = False
        self._selection_start = None
        self._selection_end = None
        self.refresh()

    def has_selection(self) -> bool:
        return (
            self._selection_start is not None
            and self._selection_end is not None
            and self._selection_start != self._selection_end
        )


def _middle_ellipsis(value: str, max_width: int) -> str:
    if max_width <= 0:
        return ""
    if len(value) <= max_width:
        return value
    if max_width <= 3:
        return "." * max_width
    marker = "..."
    keep = max_width - len(marker)
    left = max(1, keep // 2)
    right = max(1, keep - left)
    return value[:left] + marker + value[-right:]
