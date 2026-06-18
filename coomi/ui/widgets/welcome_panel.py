"""Welcome panel for the initial empty transcript state."""
from __future__ import annotations

from rich.align import Align
from rich.console import Group
from rich.panel import Panel
from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual.widget import Widget


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
    "....abca....",
    "...dehgj....",
    ".keGdnmmlj..",
    ".oeidqpriis.",
    "ktiGddruGdlj",
    "vewxyddyxwej",
    "klzABddBAzlj",
    ".kCweGGewCk.",
    "..D.D..D.D..",
    "..E.F..E.E..",
    ".F..F..E..E.",
    "...F....E...",
    "...F....E...",
)


class WelcomePanel(Widget):
    """Initial guide panel with a terminal-rendered pixel mascot."""

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

    def set_context(self, model_display: str, tool_count: int) -> None:
        self._model_display = model_display
        self._tool_count = tool_count
        self.refresh()

    def render(self):
        width = max(42, self.size.width or 80)
        height = max(14, self.size.height or 24)
        if width >= 64:
            return self._render_speech_layout(width, height)
        return self._render_stacked_layout(width, height)

    def _render_speech_layout(self, width: int, height: int) -> Group:
        bubble_width = min(58, max(34, width - 32))
        group_width = 24 + 2 + bubble_width
        left_pad = max(2, (width - group_width) // 2)
        mascot_offset = 2 if height >= 24 else 1
        group_height = max(
            len(MASCOT_ROWS_12X13) + mascot_offset,
            self._bubble_line_count(bubble_width),
        )
        spacer_lines = max(1, (height - group_height) // 2)

        row = Table.grid(expand=True)
        row.add_column(width=left_pad)
        row.add_column(width=24)
        row.add_column(width=2)
        row.add_column(width=bubble_width)
        row.add_column(ratio=1)
        row.add_row(
            "",
            Group(Text("\n" * mascot_offset), Align.left(render_pixel_mascot())),
            "",
            self._render_bubble(bubble_width),
            "",
        )
        return Group(Text("\n" * spacer_lines), row)

    def _render_stacked_layout(self, width: int, height: int) -> Group:
        bubble_width = max(34, width - 6)
        bubble_lines = self._bubble_line_count(bubble_width)
        group_height = bubble_lines + 1 + len(MASCOT_ROWS_12X13)
        spacer_lines = max(0, height - group_height - 1)

        bubble_row = Table.grid(expand=True)
        bubble_row.add_column(ratio=1)
        bubble_row.add_column(width=bubble_width)
        bubble_row.add_column(ratio=1)
        bubble_row.add_row("", self._render_bubble(bubble_width), "")

        mascot_row = Table.grid(expand=True)
        mascot_row.add_column(width=2)
        mascot_row.add_column(width=24)
        mascot_row.add_column(ratio=1)
        mascot_row.add_row("", Align.left(render_pixel_mascot()), "")
        return Group(Text("\n" * spacer_lines), bubble_row, Text("\n"), mascot_row)

    def _bubble_line_count(self, width: int) -> int:
        # Border + vertical padding + content lines.
        return 8 if width < 50 else 10

    def _render_bubble(self, width: int) -> Panel:
        model = self._model_display or "model pending"
        tools = f"{self._tool_count} tools" if self._tool_count else "tools loading"
        guide = Text()
        guide.append("准备就绪\n", style="bold cyan")
        guide.append(f"{model} · {tools}\n\n", style="dim")
        if width < 50:
            guide.append("Enter 发送，Shift+Enter 换行。\n")
            guide.append("/model 模型，/context 上下文。\n")
            guide.append("Shift+Tab 权限，Ctrl+P 命令。\n")
            guide.append("双击 Esc 退出。", style="dim")
        else:
            guide.append("Enter 发送消息，Shift+Enter 换行。\n")
            guide.append("/model 切换模型，/context 调整上下文窗口。\n")
            guide.append("Shift+Tab 切换工具权限模式。\n")
            guide.append("Ctrl+P 打开命令面板，Ctrl+C 复制选中文本。\n")
            guide.append("双击 Esc 退出应用。", style="dim")
        return Panel(
            guide,
            width=width,
            padding=(1, 2),
            border_style="#00a8df",
            title="操作指南",
            title_align="left",
        )


def render_pixel_mascot() -> Text:
    """Render the 12 x 13 mascot blocks as stable terminal cells."""
    text = Text()
    for row_index, row in enumerate(MASCOT_ROWS_12X13):
        for token in row:
            color = MASCOT_PALETTE.get(token)
            if color is None:
                text.append("  ")
            else:
                text.append("  ", style=Style(bgcolor=color))
        if row_index < len(MASCOT_ROWS_12X13) - 1:
            text.append("\n")
    return text
