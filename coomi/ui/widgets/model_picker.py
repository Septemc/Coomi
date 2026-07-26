"""ModelPicker — 交互式模型选择器

Widget + render() 即时渲染模式。
↑↓ 选择模型，←→ 切换 active / once_active，Enter 确认。
"""
from __future__ import annotations

from rich.table import Table
from rich.text import Text
from textual.widget import Widget

from ...services.llm.config import ProviderConfig, provider_type_label


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
        table.add_row(Text.from_markup(
            f"[bold #58a6ff]选择模型[/bold #58a6ff]  [dim]{len(self._providers)} 个[/dim]"
        ))
        table.add_row()

        # ── 模式提示 ──
        if self._mode == "active":
            active_pill = "[bold #0d1117 on #58a6ff] active [/bold #0d1117 on #58a6ff] [dim]持久[/dim]"
            once_pill = "[#8b949e] once_active [/#8b949e] [dim]仅本次[/dim]"
        else:
            active_pill = "[#8b949e] active [/#8b949e] [dim]持久[/dim]"
            once_pill = "[bold #0d1117 on #58a6ff] once_active [/bold #0d1117 on #58a6ff] [dim]仅本次[/dim]"
        table.add_row(Text.from_markup(f"  {active_pill}   [dim]·[/dim]   {once_pill}"))
        table.add_row()

        # ── 模型列表 ──
        for i, p in enumerate(self._providers):
            is_sel = (i == self._selected)
            fast_info = f"  [dim]fast: {p.fast_model}[/dim]" if p.fast_model else ""
            type_label = provider_type_label(p.type)
            if is_sel:
                table.add_row(Text.from_markup(
                    f"[bold #0d1117 on #58a6ff] ▸ {p.id} [/bold #0d1117 on #58a6ff]  "
                    f"[bold #e6edf3]{p.display}[/bold #e6edf3]  [#39c5cf]{type_label}[/#39c5cf]{fast_info}"
                ))
            else:
                table.add_row(Text.from_markup(
                    f"  [#8b949e]○ {p.id}[/#8b949e]  [#c9d1d9]{p.display}[/#c9d1d9]  "
                    f"[dim]{type_label}[/dim]{fast_info}"
                ))

        # ── 操作提示 ──
        table.add_row()
        table.add_row(Text.from_markup(
            "  [dim]↑↓ 选择   ←→ 切换模式   Enter 确认   Esc 取消[/dim]"
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
