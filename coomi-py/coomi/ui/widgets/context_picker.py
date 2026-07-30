"""ContextPicker — 交互式上下文窗口选择器

Widget + render() 即时渲染模式。
↑↓ 选择预设，Enter 确认。
"""
from __future__ import annotations

from rich.table import Table
from rich.text import Text
from textual.widget import Widget


PRESETS: list[tuple[str, int]] = [
    ("128K", 128_000),
    ("256K", 256_000),
    ("512K", 512_000),
    ("1M", 1_000_000),
]


def _format_size(size: int) -> str:
    if size >= 1_000_000:
        return f"{size / 1_000_000:.0f}M"
    return f"{size // 1_000}K"


class ContextPicker(Widget):
    """交互式上下文窗口选择器"""

    def __init__(self, current_size: int = 256_000, **kwargs):
        super().__init__(**kwargs)
        self._selected: int = 0
        # 定位最接近当前大小的预设
        for i, (_, size) in enumerate(PRESETS):
            if size == current_size:
                self._selected = i
                break
        else:
            # 找最接近的
            closest = min(range(len(PRESETS)), key=lambda i: abs(PRESETS[i][1] - current_size))
            self._selected = closest

    def render(self) -> Table:
        table = Table.grid(padding=(0, 1))
        table.add_column(ratio=1)

        # ── 标题 ──
        table.add_row(Text.from_markup("[bold #58a6ff]选择上下文窗口[/bold #58a6ff]"))
        table.add_row()

        # ── 预设列表 ──
        for i, (label, size) in enumerate(PRESETS):
            is_sel = (i == self._selected)
            size_str = _format_size(size)
            if is_sel:
                table.add_row(Text.from_markup(
                    f"[bold #0d1117 on #58a6ff] ▸ {size_str} [/bold #0d1117 on #58a6ff]  "
                    f"[dim]{size:,} tokens[/dim]"
                ))
            else:
                table.add_row(Text.from_markup(
                    f"  [#8b949e]○ {size_str}[/#8b949e]  [dim]{size:,} tokens[/dim]"
                ))

        # ── 操作提示 ──
        table.add_row()
        table.add_row(Text.from_markup(
            "  [dim]↑↓ 选择   Enter 确认   Esc 取消[/dim]"
        ))

        return table

    # ── 状态操作 ──

    def move_up(self) -> None:
        self._selected = (self._selected - 1) % len(PRESETS)
        self.refresh()

    def move_down(self) -> None:
        self._selected = (self._selected + 1) % len(PRESETS)
        self.refresh()

    def confirm(self) -> int:
        """返回选中的上下文窗口大小（tokens）"""
        return PRESETS[self._selected][1]
