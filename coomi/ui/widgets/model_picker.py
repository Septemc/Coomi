"""ModelPicker — 交互式模型选择器

Widget + render() 即时渲染模式。
↑↓ 选择模型，←→ 切换 active / once_active，Enter 确认。
"""
from __future__ import annotations

from rich.table import Table
from rich.text import Text
from textual.widget import Widget

from ...services.llm.config import ProviderConfig


class ModelPicker(Widget):
    """交互式模型选择器"""

    # 上次选择的模式记忆（跨实例）
    _last_mode: str = "active"

    def __init__(self, providers: list[ProviderConfig], active_id: str = "", **kwargs):
        super().__init__(**kwargs)
        self._providers = providers
        self._selected: int = 0
        self._mode: str = ModelPicker._last_mode  # "active" or "once_active"

        # 定位当前 active 的 provider
        for i, p in enumerate(providers):
            if p.id == active_id:
                self._selected = i
                break

    def render(self) -> Table:
        table = Table.grid(padding=(0, 1))
        table.add_column(ratio=1)

        # ── 标题 ──
        table.add_row(Text.from_markup("[bold cyan]Select Model[/bold cyan]"))
        table.add_row()

        # ── 模式提示 ──
        mode_hint = (
            "  Mode: [bold reverse] active [/bold reverse] (持久) | "
            "[dim] once_active [/dim] (仅本次)"
            if self._mode == "active"
            else "  Mode: [dim] active [/dim] (持久) | "
            "[bold reverse] once_active [/bold reverse] (仅本次)"
        )
        table.add_row(Text.from_markup(mode_hint))
        table.add_row()

        # ── 模型列表 ──
        for i, p in enumerate(self._providers):
            is_sel = (i == self._selected)
            fast_info = f" (fast: {p.fast_model})" if p.fast_model else ""
            row_text = f"{p.id}: {p.display} ({p.type}){fast_info}"

            if is_sel:
                table.add_row(Text.from_markup(
                    f"[bold reverse] ● {row_text} [/bold reverse]"
                ))
            else:
                table.add_row(Text.from_markup(
                    f"  [cyan]○[/cyan] {row_text}"
                ))

        # ── 操作提示 ──
        table.add_row()
        table.add_row(Text.from_markup(
            "  [dim]↑↓ 选择  ←→ 切换模式  Enter 确认  Esc 取消[/dim]"
        ))

        return table

    # ── 状态操作 ──

    def move_up(self) -> None:
        if self._providers:
            self._selected = (self._selected - 1) % len(self._providers)
            self.refresh()

    def move_down(self) -> None:
        if self._providers:
            self._selected = (self._selected + 1) % len(self._providers)
            self.refresh()

    def toggle_mode_left(self) -> None:
        """← 切换到 active"""
        self._mode = "active"
        ModelPicker._last_mode = "active"
        self.refresh()

    def toggle_mode_right(self) -> None:
        """→ 切换到 once_active"""
        self._mode = "once_active"
        ModelPicker._last_mode = "once_active"
        self.refresh()

    def confirm(self) -> tuple[ProviderConfig, str]:
        """返回 (选中的 provider, 模式字符串)"""
        provider = self._providers[self._selected]
        return provider, self._mode
