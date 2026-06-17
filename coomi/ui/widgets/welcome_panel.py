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
    "...defghij..",
    ".keGdimnmlj.",
    ".oeidpqriis.",
    "ktiGddruGdlj",
    "vewxyddyxwej",
    "klzABddBAzlj",
    ".kCweGGewCk.",
    "..D.D..D.D..",
    "..E.F..E.E..",
    ".F..F..E..E.",
    ".F.F....E.E.",
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
        bubble_width = min(70, max(36, width - 8))
        bubble = self._render_bubble(bubble_width)
        mascot = render_pixel_mascot()

        top = Table.grid(expand=True)
        top.add_column(ratio=1)
        top.add_column(width=bubble_width)
        top.add_column(ratio=1)
        top.add_row("", bubble, "")

        bubble_lines = 9 if width >= 58 else 7
        spacer_lines = max(1, height - bubble_lines - len(MASCOT_ROWS_12X13) - 2)
        left_pad = 2 if width < 80 else 4

        bottom = Table.grid(expand=True)
        bottom.add_column(width=left_pad)
        bottom.add_column(width=24)
        bottom.add_column(ratio=1)
        bottom.add_row("", Align.left(mascot), "")

        return Group(top, Text("\n" * spacer_lines), bottom)

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
