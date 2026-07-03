"""SettingsScreen — 设置面板模态屏。"""
from __future__ import annotations

from typing import Optional
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.screen import ModalScreen
from textual.widgets import Static

SETTINGS_OPTIONS = [
    ("provider_config", "新增/修改模型API配置", "管理 LLM Provider"),
    ("install_skill", "管理 Skill", "/skill list/install/enable/disable"),
    ("install_mcp", "管理 MCP", "/mcp list/add stdio|http|sse/test/tools"),
]


class SettingsScreen(ModalScreen[Optional[str]]):
    """设置面板"""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("up", "move_up", "Up", priority=True),
        Binding("down", "move_down", "Down", priority=True),
        Binding("enter", "confirm", "Confirm", priority=True),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._selected: int = 0

    def compose(self) -> ComposeResult:
        with Container(id="settings-container"):
            yield Static("  Settings", id="settings-title")
            yield Static(self._render_options(), id="settings-options")

    def _render_options(self) -> str:
        lines = []
        for i, (key, label, desc) in enumerate(SETTINGS_OPTIONS):
            if i == self._selected:
                lines.append(f"[bold reverse] ● {label} [/bold reverse]  [dim]{desc}[/dim]")
            else:
                lines.append(f"  [cyan]○[/cyan] {label}  [dim]{desc}[/dim]")
        lines.append("")
        lines.append("[dim]↑↓ 导航  Enter 选择  Esc 返回[/dim]")
        return "\n".join(lines)

    def _refresh_display(self) -> None:
        try:
            options = self.query_one("#settings-options", Static)
            options.update(self._render_options())
        except Exception:
            pass

    def action_move_up(self) -> None:
        self._selected = (self._selected - 1) % len(SETTINGS_OPTIONS)
        self._refresh_display()

    def action_move_down(self) -> None:
        self._selected = (self._selected + 1) % len(SETTINGS_OPTIONS)
        self._refresh_display()

    def action_confirm(self) -> None:
        key = SETTINGS_OPTIONS[self._selected][0]
        if key == "provider_config":
            self.dismiss("provider_config")
        elif key == "install_skill":
            self.dismiss("install_skill")
        elif key == "install_mcp":
            self.dismiss("install_mcp")

    def action_cancel(self) -> None:
        self.dismiss(None)
