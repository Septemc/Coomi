"""Keyboard-first curated Skill manager."""
from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

from rich.markup import escape
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from ...catalogs import SkillCatalogEntry, load_skill_catalog
from ...services.skills.installer import SkillInstallError
from ...services.skills.manager import SkillManager
from ...services.skills.models import SkillRecord, SkillUpdateStatus


class SkillMarketplaceScreen(ModalScreen[None]):
    """Browse curated and locally installed Skills without leaving the TUI."""

    BINDINGS = [
        Binding("escape", "cancel", "Back", priority=True),
        Binding("up", "move_up", "Up", priority=True),
        Binding("down", "move_down", "Down", priority=True),
        Binding("left", "move_action_left", "Action", priority=True),
        Binding("right", "move_action_right", "Action", priority=True),
        Binding("enter", "confirm", "Install / Check / Update", priority=True),
        Binding("delete", "delete_skill", "Uninstall", priority=True),
    ]

    def __init__(
        self,
        manager: SkillManager,
        *,
        plan_mode: bool = False,
        on_changed: Callable[[], Any] | None = None,
        catalog: list[SkillCatalogEntry] | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._manager = manager
        self._plan_mode = plan_mode
        self._on_changed = on_changed
        self._catalog = catalog if catalog is not None else load_skill_catalog()
        self._items: list[tuple[str, SkillCatalogEntry | None, SkillRecord | None]] = []
        self._updates: dict[str, SkillUpdateStatus] = {}
        self._errors: dict[str, str] = {}
        self._operation: dict[str, str] = {}
        self._busy = False
        self._delete_pending = ""
        self._delete_timer = None
        self._action_index = 0
        self._rebuild_items()

    def compose(self) -> ComposeResult:
        with Container(id="skill-marketplace-container"):
            yield Static("  Skill 精选管理", id="skill-marketplace-title")
            yield OptionList(id="skill-marketplace-list", compact=True)
            yield Static(id="skill-marketplace-detail")
            yield Static(id="skill-marketplace-footer")

    def on_mount(self) -> None:
        self._refresh_view()
        options = self.query_one("#skill-marketplace-list", OptionList)
        if options.option_count:
            options.highlighted = 0
        options.focus()

    def _rebuild_items(self) -> None:
        installed = {record.name: record for record in self._manager.list()}
        items: list[tuple[str, SkillCatalogEntry | None, SkillRecord | None]] = []
        catalog_ids: set[str] = set()
        for entry in self._catalog:
            catalog_ids.add(entry.id)
            items.append((entry.id, entry, installed.get(entry.id)))
        for record in self._manager.list():
            if record.name not in catalog_ids:
                items.append((record.name, None, record))
        self._items = items

    def _refresh_view(self, preserve_id: str = "") -> None:
        try:
            options = self.query_one("#skill-marketplace-list", OptionList)
        except Exception:
            return
        selected_id = preserve_id or self._selected_id()
        options.set_options(
            [Option(self._render_row(item), id=item[0]) for item in self._items]
        )
        if self._items:
            index = next(
                (i for i, item in enumerate(self._items) if item[0] == selected_id),
                0,
            )
            options.highlighted = index
        self._refresh_detail()

    def _render_row(
        self, item: tuple[str, SkillCatalogEntry | None, SkillRecord | None]
    ) -> str:
        item_id, entry, record = item
        operation = self._operation.get(item_id)
        update = self._updates.get(item_id)
        if operation:
            state = operation
        elif not record:
            state = "未安装"
        elif not record.enabled:
            state = "已停用"
        elif update and update.error:
            state = "检查失败"
        elif update and update.update_available:
            state = "可更新"
        elif update:
            state = "已是最新"
        else:
            state = "已安装"
        marker = "精选" if entry else "本地/手动"
        actions = self._actions(record)
        rendered_actions = " | ".join(
            f"[reverse]{escape(action)}[/reverse]" if i == self._action_index else escape(action)
            for i, action in enumerate(actions)
        )
        return f"[bold]{escape(entry.name if entry else item_id)}[/bold]  [{state}]  [dim]{marker}[/dim]  {rendered_actions}"

    @staticmethod
    def _actions(record: SkillRecord | None) -> list[str]:
        if record is None:
            return ["安装"]
        return ["关闭" if record.enabled else "启用", "配置", "检查更新", "卸载"]

    def _selected_index(self) -> int:
        try:
            highlighted = self.query_one("#skill-marketplace-list", OptionList).highlighted
        except Exception:
            return 0
        return highlighted if highlighted is not None else 0

    def _selected_item(self) -> tuple[str, SkillCatalogEntry | None, SkillRecord | None] | None:
        if not self._items:
            return None
        return self._items[self._selected_index() % len(self._items)]

    def _selected_id(self) -> str:
        item = self._selected_item()
        return item[0] if item else ""

    @on(OptionList.OptionHighlighted)
    def _on_option_highlighted(self) -> None:
        self._refresh_detail()

    def _refresh_detail(self) -> None:
        try:
            detail = self.query_one("#skill-marketplace-detail", Static)
            footer = self.query_one("#skill-marketplace-footer", Static)
        except Exception:
            return
        item = self._selected_item()
        if not item:
            detail.update("[dim]没有可显示的 Skill。[/dim]")
            return
        item_id, entry, record = item
        description = entry.description if entry else (record.description if record else "")
        source = entry.source_url if entry else (record.source if record else "")
        requirements = ", ".join(entry.requirements) if entry and entry.requirements else "无额外说明"
        commit = record.commit[:12] if record and record.commit else "-"
        enabled = "是" if record and record.enabled else ("否" if record else "-")
        license_name = entry.license if entry else "按原始来源"
        notes = entry.install_notes if entry else "手动安装的 Skill，请自行核对来源。"
        update = self._updates.get(item_id)
        message = ""
        if update:
            message = update.error or update.message
        if self._errors.get(item_id):
            message = self._errors[item_id]
        delete_note = (
            "\n[bold yellow]再次按 Delete 或 Enter 确认卸载；Esc 取消。[/bold yellow]"
            if self._delete_pending == item_id
            else ""
        )
        detail.update(
            f"[bold cyan]{escape(entry.name if entry else item_id)}[/bold cyan]\n"
            f"{escape(description or '无描述')}\n\n"
            f"[dim]来源:[/dim] {escape(source or '未知')}\n"
            f"[dim]作者/许可证:[/dim] {escape(entry.author if entry else '未知')} / {escape(license_name)}\n"
            f"[dim]要求:[/dim] {escape(requirements)}\n"
            f"[dim]已启用:[/dim] {enabled}\n"
            f"[dim]当前 commit:[/dim] {escape(commit)}\n"
            f"[dim]安装提示:[/dim] {escape(notes)}\n"
            f"[dim]状态:[/dim] {escape(message or '按 Enter 安装或检查更新')}"
            f"{delete_note}"
        )
        readonly = "  [yellow]Plan Mode：只读[/yellow]" if self._plan_mode else ""
        footer.update(
            "[dim]↑↓ 选择  Enter 安装/检查/更新  Delete 卸载  Esc 返回[/dim]"
            + readonly
        )

    def action_move_up(self) -> None:
        self._move(-1)

    def action_move_down(self) -> None:
        self._move(1)

    def _move(self, delta: int) -> None:
        if not self._items:
            return
        options = self.query_one("#skill-marketplace-list", OptionList)
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
        item_id, entry, record = item
        if self._delete_pending == item_id:
            await self._remove(item_id)
            return
        if self._plan_mode:
            self._errors[item_id] = "Plan Mode 下只能浏览，不能安装、更新或卸载。"
            self._refresh_view(item_id)
            return

        action = self._actions(record)[self._action_index]
        if record is not None and action in {"启用", "关闭"}:
            updated = self._manager.enable(item_id, enabled=action == "启用")
            self._errors[item_id] = f"已{'启用' if updated.enabled else '关闭'}。"
            await self._changed()
            self._rebuild_items()
            self._refresh_view(item_id)
            return
        if record is not None and action == "配置":
            self._errors[item_id] = "Skill 无额外参数；配置由 SKILL.md 提供，详情已显示在当前页面。"
            self._refresh_view(item_id)
            return
        if record is not None and action == "卸载":
            await self.action_delete_skill()
            return

        self._busy = True
        self._errors.pop(item_id, None)
        try:
            if record is None:
                if entry is None:
                    return
                self._operation[item_id] = "安装中"
                self._refresh_view(item_id)
                await asyncio.to_thread(
                    self._manager.install,
                    entry.source_url,
                    entry.id,
                    True,
                )
                self._action_index = 2
                self._updates.pop(item_id, None)
                await self._changed()
            else:
                status = self._updates.get(item_id)
                if status and status.update_available:
                    self._operation[item_id] = "更新中"
                    self._refresh_view(item_id)
                    await asyncio.to_thread(self._manager.update, item_id)
                    self._updates.pop(item_id, None)
                    await self._changed()
                else:
                    self._operation[item_id] = "检查中"
                    self._refresh_view(item_id)
                    self._updates[item_id] = await asyncio.to_thread(
                        self._manager.check_update, item_id
                    )
        except (OSError, SkillInstallError, asyncio.TimeoutError) as exc:
            self._errors[item_id] = str(exc)
        finally:
            self._operation.pop(item_id, None)
            self._busy = False
            self._rebuild_items()
            self._refresh_view(item_id)

    async def action_delete_skill(self) -> None:
        item = self._selected_item()
        if not item or self._busy:
            return
        item_id, _entry, record = item
        if not record:
            self._errors[item_id] = "该 Skill 尚未安装。"
            self._refresh_view(item_id)
            return
        if self._plan_mode:
            self._errors[item_id] = "Plan Mode 下不能卸载 Skill。"
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
        self._operation[item_id] = "卸载中"
        self._refresh_view(item_id)
        try:
            await asyncio.to_thread(self._manager.remove, item_id)
            self._updates.pop(item_id, None)
            self._errors.pop(item_id, None)
            await self._changed()
        except (OSError, SkillInstallError) as exc:
            self._errors[item_id] = str(exc)
        finally:
            self._operation.pop(item_id, None)
            self._delete_pending = ""
            self._delete_timer = None
            self._busy = False
            self._rebuild_items()
            self._refresh_view(item_id)

    async def _changed(self) -> None:
        if not self._on_changed:
            return
        result = self._on_changed()
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
