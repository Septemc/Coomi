"""ToolCallBanner — 工具调用生命周期数据类

非 Widget，输出 Rich Table renderable 供 RichLog.write() 使用。
状态机: PENDING → RUNNING → DONE
"""
from __future__ import annotations

import time

from rich.table import Table

from ..tool_formatter import format_tool_display


class ToolCallBanner:
    """工具调用生命周期数据，通过 build() 输出 Rich Table。"""

    RESULT_PREVIEW_LENGTH = 300

    def __init__(self, tool_name: str):
        self.tool_name = tool_name
        self._state: str = "pending"
        self._arguments: dict = {}
        self._start_time: float = time.time()
        self._elapsed: float = 0.0
        self._result_text: str = ""
        self._cache_hit: bool = False
        self._is_error: bool = False
        self._expanded: bool = False

    def set_arguments(self, arguments: dict) -> None:
        self._arguments = arguments

    def set_running(self) -> None:
        self._state = "running"
        self._start_time = time.time()

    def set_done(
        self,
        result_preview: str = "",
        cache_hit: bool = False,
        is_error: bool = False,
    ) -> None:
        self._state = "done"
        self._elapsed = time.time() - self._start_time
        self._result_text = result_preview or ""
        self._cache_hit = cache_hit
        self._is_error = is_error

    def build(self) -> Table:
        """构建 Rich Table 用于写入 RichLog。"""
        table = Table.grid(padding=(0, 0))

        icon = self._get_icon()
        display = format_tool_display(self.tool_name, self._arguments)
        status = self._get_status_text()
        table.add_row(f"{icon} [bold]{self.tool_name}[/bold] {display} {status}")

        if self._state == "done" and self._result_text:
            table.add_row(self._get_result_preview())

        return table

    def _get_icon(self) -> str:
        if self._cache_hit:
            return "[bold blue]✓[/bold blue]"
        if self._is_error:
            return "[bold red]×[/bold red]"
        if self._state == "done":
            return "[bold green]✓[/bold green]"
        if self._state == "running":
            return "[bold yellow]⟳[/bold yellow]"
        return "[dim]○[/dim]"

    def _get_status_text(self) -> str:
        if self._state == "pending":
            return "[dim]preparing...[/dim]"
        if self._state == "running":
            elapsed = time.time() - self._start_time
            return f"[bold yellow]Executing... ({elapsed:.1f}s)[/bold yellow]"
        if self._cache_hit:
            return f"[dim]✓ cache ({self._elapsed:.1f}s)[/dim]"
        if self._is_error:
            return f"[bold red]failed ({self._elapsed:.1f}s)[/bold red]"
        return f"[dim]✓ ({self._elapsed:.1f}s)[/dim]"

    def _get_result_preview(self) -> str:
        if not self._result_text:
            return ""
        if self._expanded:
            max_preview = min(1000, len(self._result_text))
            return f"[dim]{self._result_text[:max_preview]}[/dim] [bold][-][/bold]"
        max_len = self.RESULT_PREVIEW_LENGTH
        preview = self._result_text[:max_len]
        if len(self._result_text) > max_len:
            preview += "..."
        return f"[dim]{preview}[/dim] [bold][+][/bold]"
