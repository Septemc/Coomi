"""CommandAutocomplete — / 前缀命令自动补全

参考 Claude Code FuzzyPicker 模式:
- / 前缀触发
- 字符匹配过滤
- ↑↓ 导航, Enter 选择, Esc 取消
"""
from __future__ import annotations

from rich.table import Table
from textual.widget import Widget


COMMANDS = [
    ("/plan", "进入 Plan Mode，发起需求澄清问询"),
    ("/exit_plan", "退出 Plan Mode"),
    ("/model", "切换 LLM 模型"),
    ("/context", "设置上下文窗口大小"),
    ("/memory", "记忆管理 (list/add/delete/search)"),
    ("/compact", "立即压缩上下文"),
    ("/clear", "清空当前会话历史"),
    ("/help", "显示帮助信息"),
]


class CommandAutocomplete(Widget):
    """命令自动补全面板 — 当输入以 / 开头时显示

    布局:
      ┌──────────────────────────────────────┐
      │  /plan        进入 Plan Mode...      │
      │  /exit_plan   退出 Plan Mode         │
      │  /compact      立即压缩上下文        │
      │  /clear        清空当前会话历史      │
      │  /model        切换 LLM 模型         │
      │  /help         显示帮助信息          │
      └──────────────────────────────────────┘
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._query = ""
        self._selected_idx = 0
        self._filtered: list[tuple[str, str]] = list(COMMANDS)
        self.display = False

    def render(self):
        if not self._filtered:
            return "[dim]无匹配命令[/dim]"

        table = Table.grid(padding=(0, 2))
        for i, (cmd, desc) in enumerate(self._filtered):
            if i == self._selected_idx:
                table.add_row(
                    f"[bold reverse]{cmd}[/bold reverse]",
                    f"[dim]{desc}[/dim]",
                )
            else:
                table.add_row(
                    f"[cyan]{cmd}[/cyan]",
                    f"[dim]{desc}[/dim]",
                )
        return table

    def update_filter(self, query: str) -> None:
        """更新过滤条件"""
        self._query = query
        if query:
            self._filtered = [
                (cmd, desc) for cmd, desc in COMMANDS
                if cmd.startswith(query)
            ]
        else:
            self._filtered = list(COMMANDS)
        self._selected_idx = 0
        self.refresh()

    def move_up(self) -> None:
        if self._filtered:
            self._selected_idx = (self._selected_idx - 1) % len(self._filtered)
            self.refresh()

    def move_down(self) -> None:
        if self._filtered:
            self._selected_idx = (self._selected_idx + 1) % len(self._filtered)
            self.refresh()

    def get_selected_command(self) -> str | None:
        if self._filtered:
            return self._filtered[self._selected_idx][0]
        return None
