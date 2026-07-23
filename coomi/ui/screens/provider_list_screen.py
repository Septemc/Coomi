"""ProviderListScreen — Provider 列表管理屏

显示所有已配置的 LLM Provider，支持导航、新增、编辑、删除。
"""
from __future__ import annotations

import asyncio
from typing import Optional
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.screen import ModalScreen
from textual.widgets import Static

from ...services.llm.config import ConfigManager, ProviderConfig, provider_type_label


class ProviderListScreen(ModalScreen[Optional[dict]]):
    """Provider 列表屏

    返回: {"action": "edit", "provider": ProviderConfig} 或 {"action": "add"} 或 None
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("up", "move_up", "Up", priority=True),
        Binding("down", "move_down", "Down", priority=True),
        Binding("enter", "confirm", "Confirm", priority=True),
        Binding("delete", "delete_provider", "Delete", priority=True),
    ]

    def __init__(self, config_mgr: ConfigManager, **kwargs):
        super().__init__(**kwargs)
        self._config_mgr = config_mgr
        self._providers: list[ProviderConfig] = config_mgr.list_providers()
        self._selected: int = 0

    def compose(self) -> ComposeResult:
        with Container(id="provider-list-container"):
            yield Static("  Model Providers", id="provider-list-title")
            yield Static(self._render_list(), id="provider-list-items")

    def _render_list(self) -> str:
        lines = []
        active_id = self._config_mgr.data.get("active", "")

        for i, p in enumerate(self._providers):
            is_sel = (i == self._selected)
            is_active = (p.id == active_id)
            active_marker = " [green]✓ active[/green]" if is_active else ""
            fast_info = f" [dim](fast: {p.fast_model})[/dim]" if p.fast_model else ""

            if is_sel:
                lines.append(
                    f"[bold reverse] ● {p.id}: {p.display} ({provider_type_label(p.type)}){fast_info}{active_marker} [/bold reverse]"
                )
            else:
                lines.append(
                    f"  [cyan]○[/cyan] {p.id}: {p.display} ({provider_type_label(p.type)}){fast_info}{active_marker}"
                )

        # 新增选项
        add_sel = (self._selected == len(self._providers))
        if add_sel:
            lines.append("[bold reverse] ● + 新增 Provider [/bold reverse]")
        else:
            lines.append("  [cyan]○[/cyan] + 新增 Provider")

        lines.append("")
        lines.append("[dim]↑↓ 导航  Enter 编辑/新增  Delete 删除  Esc 返回[/dim]")
        return "\n".join(lines)

    def _refresh_display(self) -> None:
        try:
            items = self.query_one("#provider-list-items", Static)
            items.update(self._render_list())
        except Exception:
            pass

    def action_move_up(self) -> None:
        total = len(self._providers) + 1  # +1 for "add"
        self._selected = (self._selected - 1) % total
        self._refresh_display()

    def action_move_down(self) -> None:
        total = len(self._providers) + 1
        self._selected = (self._selected + 1) % total
        self._refresh_display()

    def action_confirm(self) -> None:
        if self._selected == len(self._providers):
            # 新增
            self.dismiss({"action": "add"})
        else:
            # 编辑
            provider = self._providers[self._selected]
            self.dismiss({"action": "edit", "provider": provider})

    def action_delete_provider(self) -> None:
        if self._selected < len(self._providers):
            provider = self._providers[self._selected]
            removed = self._config_mgr.remove_provider(provider.id)
            self._providers = self._config_mgr.list_providers()
            if self._selected >= len(self._providers):
                self._selected = max(0, len(self._providers) - 1)
            self._refresh_display()
            reload_active = getattr(self.app, "_reload_active_provider_from_config", None)
            if removed and reload_active:
                asyncio.create_task(reload_active(show_message=True))

    def action_cancel(self) -> None:
        self.dismiss(None)
