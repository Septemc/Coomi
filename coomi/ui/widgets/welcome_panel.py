"""Welcome panel for the initial empty transcript state."""
from __future__ import annotations

from rich.align import Align
from rich.console import Group
from rich.panel import Panel
from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual import events
from textual.widget import Widget

from ...services.session_history import SessionHistoryRecord


# Extracted from assets/mascot/coomi.png by cropping the non-transparent bounds
# and resampling the original image into exactly 12 x 13 color blocks.
MASCOT_PALETTE: dict[str, str] = {
    "a": "#233f82",
    "b": "#223f81",
    "c": "#223e81",
    "d": "#2d50ab",
    "e": "#2c4fa9",
    "f": "#2e50aa",
    "g": "#8dadde",
    "h": "#2b4ea9",
    "i": "#2c4faa",
    "j": "#254584",
    "k": "#26437c",
    "l": "#2c4fa8",
    "m": "#8eaedf",
    "n": "#93b3e2",
    "o": "#25427b",
    "p": "#3255ad",
    "q": "#92b2e2",
    "r": "#2f52ac",
    "s": "#244583",
    "t": "#2c4fa7",
    "u": "#2c4fab",
    "v": "#25437b",
    "w": "#2c50ab",
    "x": "#1b1a1e",
    "y": "#2c4ea7",
    "z": "#c67260",
    "A": "#cd7158",
    "B": "#3150a8",
    "C": "#2b4fa8",
    "D": "#26437d",
    "E": "#2c4ea3",
    "F": "#2c4ea2",
    "G": "#2d50ab",
}

MASCOT_ROWS_12X13: tuple[str, ...] = (
    "....aaaa....",
    "...dddgdd...",
    ".adddgggdd..",
    ".addddgddda.",
    "adddddddddda",
    "addxddddxdda",
    "adzzddddzzda",
    ".adddddddda.",
    "..d.d..d.d..",
    "..d.d..d.d..",
    ".d..d..d..d.",
    "...d....d...",
    "...d....d...",
)

MASCOT_COMPACT_WIDTH = max(len(row) for row in MASCOT_ROWS_12X13)
MASCOT_COMPACT_HEIGHT = (len(MASCOT_ROWS_12X13) + 1) // 2
GUIDE_MIN_WIDTH = 34
GUIDE_MAX_WIDTH = 60
MASCOT_GUIDE_GAP = 2
WELCOME_BOTTOM_MARGIN = 1
MASCOT_GUIDE_BOTTOM_GAP = 1


class WelcomePanel(Widget):
    """Initial guide panel with a terminal-rendered pixel mascot and session list."""

    DEFAULT_CSS = """
    WelcomePanel {
        height: 1fr;
        background: #000000;
        padding: 1 2;
    }
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._model_display = ""
        self._tool_count = 0
        self._sessions: list[SessionHistoryRecord] = []
        self._selected_session = 0
        self._session_scroll = 0
        self._history_x_start = 0
        self._history_item_y_start = 0
        self._history_visible_count = 0

    def set_context(
        self,
        model_display: str,
        tool_count: int,
        sessions: list[SessionHistoryRecord] | None = None,
    ) -> None:
        self._model_display = model_display
        self._tool_count = tool_count
        if sessions is not None:
            self._sessions = sessions
            self._selected_session = min(self._selected_session, max(0, len(sessions) - 1))
            self._session_scroll = min(self._session_scroll, self._selected_session)
        self.refresh()

    def has_sessions(self) -> bool:
        return bool(self._sessions)

    def move_session_selection(self, direction: int) -> None:
        if not self._sessions:
            return
        self._selected_session = (self._selected_session + direction) % len(self._sessions)
        self._keep_selected_visible()
        self.refresh()

    def open_selected_session(self) -> None:
        if not self._sessions:
            return
        record = self._sessions[self._selected_session]
        self.app.open_session_from_history(str(record.path))

    def render(self):
        width = max(42, self.size.width or 80)
        height = max(12, self.size.height or 24)
        if width >= 74:
            return self._render_split_layout(width, height)
        return self._render_stacked_layout(width, height)

    def _render_split_layout(self, width: int, height: int) -> Table:
        history_width = min(42, max(30, width // 3))
        left_width = max(30, width - history_width - 1)
        self._history_x_start = left_width + 1
        self._history_item_y_start = 3

        layout = Table.grid(expand=True)
        layout.add_column(width=left_width)
        layout.add_column(width=1)
        layout.add_column(width=history_width)
        layout.add_row(
            self._render_left_area(left_width, height),
            "",
            self._render_history_panel(history_width, height),
        )
        return layout

    def _render_stacked_layout(self, width: int, height: int) -> Group:
        left_height = max(8, height // 2)
        history_height = max(8, height - left_height - 1)
        self._history_x_start = 0
        self._history_item_y_start = left_height + 4
        return Group(
            self._render_left_area(width, left_height),
            Text("\n"),
            self._render_history_panel(width, history_height),
        )

    def _render_left_area(self, width: int, height: int) -> Group:
        bubble_width = min(
            GUIDE_MAX_WIDTH,
            max(GUIDE_MIN_WIDTH, width - MASCOT_COMPACT_WIDTH - MASCOT_GUIDE_GAP - 3),
        )
        bubble = self._render_bubble(bubble_width)
        bubble_lines = self._bubble_line_count(bubble_width)
        mascot_offset = max(1, bubble_lines - MASCOT_COMPACT_HEIGHT - MASCOT_GUIDE_BOTTOM_GAP)
        cluster_height = max(bubble_lines, mascot_offset + MASCOT_COMPACT_HEIGHT)
        top_pad = max(0, height - cluster_height - WELCOME_BOTTOM_MARGIN)

        row = Table.grid(expand=False)
        row.add_column(width=MASCOT_COMPACT_WIDTH)
        row.add_column(width=MASCOT_GUIDE_GAP)
        row.add_column(width=bubble_width)
        row.add_row(
            Group(Text("\n" * mascot_offset), Align.left(render_pixel_mascot())),
            "",
            bubble,
        )
        return Group(Text("\n" * top_pad), row)

    def _bubble_line_count(self, width: int) -> int:
        return 9 if width < 46 else 13

    def _render_bubble(self, width: int) -> Panel:
        model = self._model_display or "model pending"
        tools = f"{self._tool_count} tools" if self._tool_count else "tools loading"
        guide = Text()
        guide.append("操作指南\n", style="bold cyan")
        guide.append(f"{model} · {tools}\n\n", style="dim")
        if width < 46:
            guide.append("Enter 发送，Shift+Enter 换行。\n")
            guide.append("/model 模型，/context 上下文。\n")
            guide.append("/clear 新会话，↑↓ 选历史。\n")
            guide.append("F2 Setting，Ctrl+P 命令。\n")
            guide.append("Shift+Tab 权限，双 Esc 退出。", style="dim")
        else:
            guide.append("Enter 发送消息，Shift+Enter 换行。\n")
            guide.append("/model 切换模型，/context 调整上下文窗口。\n")
            guide.append("/permission 查看权限，Shift+Tab 快速切换。\n")
            guide.append("/clear 新建会话，/compact 压缩上下文。\n")
            guide.append("右侧 Sessions 可用鼠标/↑↓/Enter 打开历史。\n")
            guide.append("Ctrl+P 打开命令面板，F2 打开 Setting。\n")
            guide.append("Ctrl+C 复制选中文本，双击 Esc 退出应用。", style="dim")
        return Panel(
            guide,
            width=width,
            padding=(0, 1),
            border_style="#00a8df",
        )

    def _render_history_panel(self, width: int, height: int) -> Panel:
        content_height = max(4, height - 2)
        visible_count = max(1, content_height - 4)
        self._keep_selected_visible(visible_count)
        records = self._sessions[self._session_scroll : self._session_scroll + visible_count]
        self._history_visible_count = len(records)

        rows: list[Text] = [Text(" Sessions", style="bold cyan"), Text("")]
        if not records:
            rows.append(Text("  暂无历史会话", style="dim"))
        else:
            for index, record in enumerate(records):
                absolute_index = self._session_scroll + index
                rows.append(self._render_session_row(record, absolute_index, max(10, width - 4)))

        used_rows = len(rows) + 2
        spacer = max(0, content_height - used_rows)
        rows.extend(Text("") for _ in range(spacer))
        rows.append(Text(" 鼠标/上下键进行选择", style="dim"))
        rows.append(Text(" 点击/Enter选中会话", style="dim"))

        return Panel(
            Group(*rows),
            width=width,
            height=height,
            border_style="#30363d",
            padding=(0, 1),
        )

    def _render_session_row(self, record: SessionHistoryRecord, index: int, width: int) -> Text:
        title_budget = max(8, width - 13)
        title = _truncate(record.title, title_budget)
        date = record.updated_at.strftime("%m-%d %H:%M") if record.updated_at else "-- -- --:--"
        line = f" {date}  {title}"
        if index == self._selected_session:
            return Text(line.ljust(width), style="bold white on #264f78")
        return Text(line.ljust(width), style="white")

    def on_click(self, event: events.Click) -> None:
        if event.x < self._history_x_start:
            return
        index = event.y - self._history_item_y_start
        if 0 <= index < self._history_visible_count:
            self._selected_session = self._session_scroll + index
            self.refresh()
            self.open_selected_session()
            event.stop()

    def _keep_selected_visible(self, visible_count: int | None = None) -> None:
        visible_count = visible_count or max(1, self._history_visible_count)
        if self._selected_session < self._session_scroll:
            self._session_scroll = self._selected_session
        elif self._selected_session >= self._session_scroll + visible_count:
            self._session_scroll = self._selected_session - visible_count + 1
        max_scroll = max(0, len(self._sessions) - visible_count)
        self._session_scroll = max(0, min(self._session_scroll, max_scroll))


def render_pixel_mascot() -> Text:
    """Render the mascot as compact half-height terminal pixels."""
    text = Text()
    for row_index in range(0, len(MASCOT_ROWS_12X13), 2):
        top = MASCOT_ROWS_12X13[row_index]
        bottom = MASCOT_ROWS_12X13[row_index + 1] if row_index + 1 < len(MASCOT_ROWS_12X13) else ""
        for column in range(MASCOT_COMPACT_WIDTH):
            top_color = MASCOT_PALETTE.get(top[column]) if column < len(top) else None
            bottom_color = MASCOT_PALETTE.get(bottom[column]) if column < len(bottom) else None
            if top_color and bottom_color:
                text.append("▀", style=Style(color=top_color, bgcolor=bottom_color))
            elif top_color:
                text.append("▀", style=Style(color=top_color))
            elif bottom_color:
                text.append("▄", style=Style(color=bottom_color))
            else:
                text.append(" ")
        if row_index + 2 < len(MASCOT_ROWS_12X13):
            text.append("\n")
    return text


def _truncate(value: str, max_width: int) -> str:
    if len(value) <= max_width:
        return value
    if max_width <= 3:
        return "." * max_width
    return value[: max_width - 3] + "..."
