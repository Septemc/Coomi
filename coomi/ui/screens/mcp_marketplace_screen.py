"""Keyboard-first curated MCP server manager."""
from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any, Optional

from rich.markup import escape
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from ...catalogs import McpCatalogEntry, load_mcp_catalog
from ...services.mcp.client import McpError
from ...services.mcp.manager import McpManager
from ...services.mcp.models import McpServerConfig


class McpInstallConfigScreen(ModalScreen[Optional[dict[str, str]]]):
    """Collect only the values required by one curated MCP entry."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+s", "submit", "Install", priority=True),
    ]

    def __init__(self, entry: McpCatalogEntry, **kwargs):
        super().__init__(**kwargs)
        self._entry = entry
        self._inputs = list(entry.inputs)

    def compose(self) -> ComposeResult:
        with Container(id="mcp-install-container"):
            yield Static(f"  配置 {escape(self._entry.name)}", id="mcp-install-title")
            yield Static(
                "[dim]只填写该 MCP 运行所需参数。秘密值会以密码形式输入，但仍会保存到本地 MCP 配置文件。[/dim]",
                id="mcp-install-help",
            )
            with Vertical(id="mcp-install-form"):
                for index, item in enumerate(self._inputs):
                    required = " *" if item.required else ""
                    yield Static(
                        f"[bold]{escape(item.label)}{required}[/bold]  [dim]{escape(item.description)}[/dim]"
                    )
                    yield Input(
                        placeholder=item.placeholder,
                        password=item.secret,
                        id=f"mcp-install-field-{index}",
                    )
            yield Static("", id="mcp-install-error")
            yield Static("[dim]Enter 提交  Ctrl+S 提交  Esc 取消[/dim]", id="mcp-install-footer")

    def on_mount(self) -> None:
        if self._inputs:
            self.query_one("#mcp-install-field-0", Input).focus()

    @on(Input.Submitted)
    def _on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_submit()

    def action_submit(self) -> None:
        values: dict[str, str] = {}
        missing: list[str] = []
        for index, item in enumerate(self._inputs):
            value = self.query_one(f"#mcp-install-field-{index}", Input).value.strip()
            if item.required and not value:
                missing.append(item.label)
            values[item.key] = value
        if missing:
            self.query_one("#mcp-install-error", Static).update(
                f"[red]缺少必填配置：{escape(', '.join(missing))}[/red]"
            )
            return
        self.dismiss(values)

    def action_cancel(self) -> None:
        self.dismiss(None)


class McpMarketplaceScreen(ModalScreen[None]):
    """Browse, configure, test and remove MCP servers."""

    BINDINGS = [
        Binding("escape", "cancel", "Back", priority=True),
        Binding("up", "move_up", "Up", priority=True),
        Binding("down", "move_down", "Down", priority=True),
        Binding("left", "move_action_left", "Action", priority=True),
        Binding("right", "move_action_right", "Action", priority=True),
        Binding("enter", "confirm", "Install / Test", priority=True),
        Binding("delete", "delete_mcp", "Remove", priority=True),
    ]

    def __init__(
        self,
        manager: McpManager,
        *,
        plan_mode: bool = False,
        on_registry_refresh: Callable[[], Any] | None = None,
        catalog: list[McpCatalogEntry] | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._manager = manager
        self._plan_mode = plan_mode
        self._on_registry_refresh = on_registry_refresh
        self._catalog = catalog if catalog is not None else load_mcp_catalog()
        self._items: list[tuple[str, McpCatalogEntry | None, McpServerConfig | None]] = []
        self._operation: dict[str, str] = {}
        self._messages: dict[str, str] = {}
        self._busy = False
        self._delete_pending = ""
        self._delete_timer = None
        self._action_index = 0
        self._rebuild_items()

    def compose(self) -> ComposeResult:
        with Container(id="mcp-marketplace-container"):
            yield Static("  MCP 精选管理", id="mcp-marketplace-title")
            yield OptionList(id="mcp-marketplace-list", compact=True)
            yield Static(id="mcp-marketplace-detail")
            yield Static(id="mcp-marketplace-footer")

    def on_mount(self) -> None:
        self._refresh_view()
        options = self.query_one("#mcp-marketplace-list", OptionList)
        if options.option_count:
            options.highlighted = 0
        options.focus()

    def _rebuild_items(self) -> None:
        configured = {server.name: server for server in self._manager.list()}
        items: list[tuple[str, McpCatalogEntry | None, McpServerConfig | None]] = []
        catalog_ids: set[str] = set()
        for entry in self._catalog:
            catalog_ids.add(entry.id)
            items.append((entry.id, entry, configured.get(entry.id)))
        for server in self._manager.list():
            if server.name not in catalog_ids:
                items.append((server.name, None, server))
        self._items = items

    def _selected_index(self) -> int:
        try:
            highlighted = self.query_one("#mcp-marketplace-list", OptionList).highlighted
        except Exception:
            return 0
        return highlighted if highlighted is not None else 0

    def _selected_item(self) -> tuple[str, McpCatalogEntry | None, McpServerConfig | None] | None:
        if not self._items:
            return None
        return self._items[self._selected_index() % len(self._items)]

    def _selected_id(self) -> str:
        item = self._selected_item()
        return item[0] if item else ""

    def _refresh_view(self, preserve_id: str = "") -> None:
        try:
            options = self.query_one("#mcp-marketplace-list", OptionList)
        except Exception:
            return
        selected_id = preserve_id or self._selected_id()
        options.set_options(
            [Option(self._render_row(item), id=item[0]) for item in self._items]
        )
        if self._items:
            options.highlighted = next(
                (i for i, item in enumerate(self._items) if item[0] == selected_id),
                0,
            )
        self._refresh_detail()

    def _render_row(
        self, item: tuple[str, McpCatalogEntry | None, McpServerConfig | None]
    ) -> str:
        item_id, entry, server = item
        operation = self._operation.get(item_id)
        if operation:
            state = operation
        elif not server:
            state = "未配置"
        elif entry and server.catalog_signature != entry.signature:
            state = "可刷新配置"
        elif not server.enabled:
            state = "已停用"
        elif server.last_error:
            state = "连接失败"
        elif server.last_checked_at:
            state = f"已连接 {server.tools_count} 工具"
        else:
            state = "已配置"
        source = "精选" if entry else "手动"
        actions = self._actions(server)
        rendered = " | ".join(
            f"[reverse]{escape(action)}[/reverse]" if i == self._action_index else escape(action)
            for i, action in enumerate(actions)
        )
        return f"[bold]{escape(entry.name if entry else item_id)}[/bold]  [{state}]  [dim]{source}[/dim]  {rendered}"

    @staticmethod
    def _actions(server: McpServerConfig | None) -> list[str]:
        if server is None:
            return ["安装"]
        return ["关闭" if server.enabled else "启用", "配置", "测试连接", "检查更新", "卸载"]

    @on(OptionList.OptionHighlighted)
    def _on_option_highlighted(self) -> None:
        self._refresh_detail()

    def _refresh_detail(self) -> None:
        try:
            detail = self.query_one("#mcp-marketplace-detail", Static)
            footer = self.query_one("#mcp-marketplace-footer", Static)
        except Exception:
            return
        item = self._selected_item()
        if not item:
            detail.update("[dim]没有可显示的 MCP。[/dim]")
            return
        item_id, entry, server = item
        description = entry.description if entry else "手动添加的 MCP Server"
        runtime = ", ".join(entry.runtime_requirements) if entry else "按现有配置"
        required = ", ".join(item.label for item in entry.inputs) if entry and entry.inputs else "无"
        source = entry.homepage if entry else "用户配置"
        transport = server.transport if server else (entry.transport if entry else "")
        checked = server.last_checked_at if server and server.last_checked_at else "-"
        tools_count = server.tools_count if server else 0
        trust = (
            "官方/已核验" if entry and entry.official else "社区/已核验" if entry else "手动配置"
        )
        license_name = entry.license if entry else "按原始来源"
        notes = entry.install_notes if entry else "手动配置，请自行核对命令与权限。"
        message = self._messages.get(item_id, "")
        if not message and server and server.last_error:
            message = self._redact_error(server)
        delete_note = (
            "\n[bold yellow]再次按 Delete 或 Enter 确认移除；Esc 取消。[/bold yellow]"
            if self._delete_pending == item_id
            else ""
        )
        detail.update(
            f"[bold cyan]{escape(entry.name if entry else item_id)}[/bold cyan]\n"
            f"{escape(description)}\n\n"
            f"[dim]来源:[/dim] {escape(source)}\n"
            f"[dim]来源级别/许可证:[/dim] {escape(trust)} / {escape(license_name)}\n"
            f"[dim]Transport:[/dim] {escape(transport)}\n"
            f"[dim]运行要求:[/dim] {escape(runtime)}\n"
            f"[dim]安装输入:[/dim] {escape(required)}\n"
            f"[dim]已发现工具:[/dim] {tools_count}\n"
            f"[dim]最近检查:[/dim] {escape(checked)}\n"
            f"[dim]安装提示:[/dim] {escape(notes)}\n"
            f"[dim]状态:[/dim] {escape(message or '按 Enter 配置或测试连接')}"
            f"{delete_note}"
        )
        readonly = "  [yellow]Plan Mode：只读[/yellow]" if self._plan_mode else ""
        footer.update(
            "[dim]↑↓ 选择  Enter 配置/测试/刷新  Delete 移除  Esc 返回[/dim]"
            + readonly
        )

    @staticmethod
    def _redact_error(server: McpServerConfig) -> str:
        message = server.last_error
        for value in server.env.values():
            if value:
                message = message.replace(value, "***")
        for value in server.headers.values():
            if value:
                message = message.replace(value, "***")
        return message

    def action_move_up(self) -> None:
        self._move(-1)

    def action_move_down(self) -> None:
        self._move(1)

    def _move(self, delta: int) -> None:
        if not self._items:
            return
        options = self.query_one("#mcp-marketplace-list", OptionList)
        options.highlighted = (self._selected_index() + delta) % len(self._items)
        options.scroll_to_highlight()
        self._action_index = 0
        self._refresh_view(self._selected_id())

    def action_move_action_left(self) -> None:
        self._move_action(-1)

    def action_move_action_right(self) -> None:
        self._move_action(1)

    def _move_action(self, delta: int) -> None:
        item = self._selected_item()
        if not item:
            return
        self._action_index = (self._action_index + delta) % len(self._actions(item[2]))
        self._refresh_view(item[0])

    async def action_confirm(self) -> None:
        item = self._selected_item()
        if not item or self._busy:
            return
        item_id, entry, server = item
        if self._delete_pending == item_id:
            await self._remove(item_id)
            return
        if self._plan_mode:
            self._messages[item_id] = "Plan Mode 下只能浏览，不能配置、测试刷新或移除。"
            self._refresh_view(item_id)
            return
        action = self._actions(server)[self._action_index]
        if server is not None and action in {"启用", "关闭"}:
            updated = self._manager.enable(item_id, enabled=action == "启用")
            self._messages[item_id] = f"已{'启用' if updated.enabled else '关闭'}。"
            await self._refresh_registry()
            self._rebuild_items()
            self._refresh_view(item_id)
            return
        if server is not None and action == "卸载":
            await self.action_delete_mcp()
            return
        if server is not None and action == "检查更新":
            self._messages[item_id] = (
                "发现精选配置更新，选择“配置”可重新应用。"
                if entry and server.catalog_signature != entry.signature else "已是最新精选配置。"
            )
            self._refresh_view(item_id)
            return
        if server is not None and action == "配置" and entry:
            if entry.inputs:
                self.app.push_screen(McpInstallConfigScreen(entry), lambda values: self._on_config_result(entry, values))
            else:
                await self._install(entry, {})
            return
        if server is None:
            if not entry:
                self._messages[item_id] = "手动 MCP 没有可应用的精选配置。"
                self._refresh_view(item_id)
                return
            if entry.inputs:
                self.app.push_screen(
                    McpInstallConfigScreen(entry),
                    lambda values: self._on_config_result(entry, values),
                )
            else:
                await self._install(entry, {})
            return
        await self._test(item_id)

    def _on_config_result(
        self, entry: McpCatalogEntry, values: Optional[dict[str, str]]
    ) -> None:
        if values is not None:
            asyncio.create_task(self._install(entry, values))

    async def _install(self, entry: McpCatalogEntry, values: dict[str, str]) -> None:
        if self._busy:
            return
        item_id = entry.id
        self._busy = True
        self._operation[item_id] = "配置中"
        self._messages.pop(item_id, None)
        self._refresh_view(item_id)
        try:
            rendered = entry.render(values)
            await asyncio.to_thread(self._manager.add_catalog_config, rendered)
            ok, message = await asyncio.to_thread(self._manager.test, item_id)
            self._action_index = 2
            self._messages[item_id] = message
            if ok:
                await self._refresh_registry()
        except (OSError, ValueError, McpError) as exc:
            self._messages[item_id] = str(exc)
        finally:
            self._operation.pop(item_id, None)
            self._busy = False
            self._rebuild_items()
            self._refresh_view(item_id)

    async def _test(self, item_id: str) -> None:
        self._busy = True
        self._operation[item_id] = "检查中"
        self._messages.pop(item_id, None)
        self._refresh_view(item_id)
        try:
            ok, message = await asyncio.to_thread(self._manager.test, item_id)
            self._messages[item_id] = message
            if ok:
                await self._refresh_registry()
        except (OSError, McpError) as exc:
            self._messages[item_id] = str(exc)
        finally:
            self._operation.pop(item_id, None)
            self._busy = False
            self._rebuild_items()
            self._refresh_view(item_id)

    async def action_delete_mcp(self) -> None:
        item = self._selected_item()
        if not item or self._busy:
            return
        item_id, _entry, server = item
        if not server:
            self._messages[item_id] = "该 MCP 尚未配置。"
            self._refresh_view(item_id)
            return
        if self._plan_mode:
            self._messages[item_id] = "Plan Mode 下不能移除 MCP。"
            self._refresh_view(item_id)
            return
        if self._delete_pending == item_id:
            await self._remove(item_id)
            return
        self._delete_pending = item_id
        if self._delete_timer:
            self._delete_timer.stop()
        self._delete_timer = self.set_timer(3.0, self._reset_delete_pending)
        self._refresh_detail()

    async def _remove(self, item_id: str) -> None:
        self._busy = True
        self._operation[item_id] = "移除中"
        self._refresh_view(item_id)
        try:
            await asyncio.to_thread(self._manager.remove, item_id)
            self._messages.pop(item_id, None)
            await self._refresh_registry()
        except (OSError, McpError) as exc:
            self._messages[item_id] = str(exc)
        finally:
            self._operation.pop(item_id, None)
            self._delete_pending = ""
            self._delete_timer = None
            self._busy = False
            self._rebuild_items()
            self._refresh_view(item_id)

    async def _refresh_registry(self) -> None:
        if not self._on_registry_refresh:
            return
        result = self._on_registry_refresh()
        if inspect.isawaitable(result):
            await result

    def _reset_delete_pending(self) -> None:
        self._delete_pending = ""
        self._delete_timer = None
        self._refresh_detail()

    def action_cancel(self) -> None:
        if self._delete_pending:
            self._reset_delete_pending()
            return
        if not self._busy:
            self.dismiss(None)
