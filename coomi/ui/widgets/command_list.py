"""CommandList — 内联斜杠指令列表

单个 Widget + render() 即时渲染。
用户输入 "/" 时显示在消息流上方，↑↓ 选择 + Enter 执行。
"""
from __future__ import annotations

from rich.table import Table
from rich.text import Text
from textual.widget import Widget

COMMANDS = [
    ("/plan", "进入 Plan Mode"),
    ("/exit_plan", "退出 Plan Mode"),
    ("/loop", "长线任务执行模式"),
    ("/model", "切换 LLM 模型"),
    ("/context", "设置上下文窗口"),
    ("/permission", "查看/切换权限模式"),
    ("/memory", "记忆管理"),
    ("/skill", "Skill 扩展管理"),
    ("/mcp", "MCP Server 管理"),
    ("/compact", "压缩上下文"),
    ("/clear", "清空会话"),
    ("/help", "显示帮助"),
]


class CommandList(Widget):
    """内联斜杠指令列表 — render() 即时渲染"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._filter = ""
        self._selected = 0

    @property
    def filtered(self) -> list[tuple[str, str]]:
        f = self._filter.lower()
        return [(c, d) for c, d in COMMANDS if f in c.lower()]

    def set_filter(self, text: str) -> None:
        self._filter = text
        self._selected = 0
        self.refresh(layout=True)

    def move_up(self) -> None:
        items = self.filtered
        if items:
            self._selected = (self._selected - 1) % len(items)
            self.refresh()

    def move_down(self) -> None:
        items = self.filtered
        if items:
            self._selected = (self._selected + 1) % len(items)
            self.refresh()

    def get_selected_command(self) -> str | None:
        items = self.filtered
        if items and self._selected < len(items):
            return items[self._selected][0]
        return None

    def render(self) -> Table:
        table = Table.grid(padding=(0, 1))
        items = self.filtered
        if not items:
            table.add_row(Text.from_markup("[dim]无匹配指令[/dim]"))
            return table
        for i, (cmd, desc) in enumerate(items):
            if i == self._selected:
                table.add_row(Text.from_markup(
                    f"[bold reverse]{cmd}[/bold reverse]  [dim]{desc}[/dim]"
                ))
            else:
                table.add_row(Text.from_markup(
                    f"[cyan]{cmd}[/cyan]  [dim]{desc}[/dim]"
                ))
        return table
