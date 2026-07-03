"""CommandPalette — 模态命令面板 (Ctrl+P)

参考 Claude Code FuzzyPicker 模式:
- 模糊搜索过滤
- ↑↓/Ctrl+N/Ctrl+P 导航
- Enter 选择
- Esc 取消
"""
from __future__ import annotations

from rich.table import Table
from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Input, RichLog, Static


# 命令注册表
COMMANDS = [
    ("/plan", "进入 Plan Mode，发起需求澄清问询"),
    ("/exit_plan", "退出 Plan Mode"),
    ("/model", "切换 LLM 模型"),
    ("/context", "设置上下文窗口大小"),
    ("/permission", "查看/切换权限模式"),
    ("/memory", "记忆管理 (list/add/delete/search)"),
    ("/skill", "Skill 扩展管理"),
    ("/mcp", "MCP Server 管理"),
    ("/compact", "立即压缩上下文"),
    ("/clear", "清空当前会话历史"),
    ("/help", "显示帮助信息"),
]


class CommandPalette(ModalScreen[str]):
    """模态命令面板"""

    CSS = """
    CommandPalette {
        align: center middle;
    }

    #palette-container {
        width: 60;
        height: auto;
        max-height: 20;
        background: #161b22;
        border: solid #30363d;
    }

    #palette-title {
        height: 1;
        padding: 0 1;
        background: #0d1117;
        color: #58a6ff;
    }

    #palette-input {
        height: 1;
        margin: 0 1;
        background: #0d1117;
    }

    #palette-commands {
        height: auto;
        max-height: 15;
        padding: 0 1;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("up", "cursor_up", "Up"),
        ("down", "cursor_down", "Down"),
        ("enter", "select", "Select"),
        ("ctrl+p", "cancel", "Cancel"),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._selected_idx = 0
        self._filtered_commands = list(COMMANDS)

    def compose(self) -> ComposeResult:
        yield Static("[bold cyan]Command Palette[/bold cyan]", id="palette-title")
        yield Input(placeholder="> 搜索命令...", id="palette-input")
        yield RichLog(id="palette-commands", markup=True, wrap=False, highlight=False)

    def on_mount(self) -> None:
        self._render_commands()
        self.query_one("#palette-input", Input).focus()

    def _render_commands(self) -> None:
        log = self.query_one("#palette-commands", RichLog)
        log.clear()

        table = Table.grid(padding=(0, 1))
        for i, (cmd, desc) in enumerate(self._filtered_commands):
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
        log.write(table)

    def on_input_changed(self, event: Input.Changed) -> None:
        query = event.value.strip().lower()
        if query:
            self._filtered_commands = [
                (cmd, desc) for cmd, desc in COMMANDS
                if query in cmd or query in desc.lower()
            ]
        else:
            self._filtered_commands = list(COMMANDS)
        self._selected_idx = 0
        self._render_commands()

    def action_cursor_up(self) -> None:
        if self._filtered_commands:
            self._selected_idx = (self._selected_idx - 1) % len(self._filtered_commands)
            self._render_commands()

    def action_cursor_down(self) -> None:
        if self._filtered_commands:
            self._selected_idx = (self._selected_idx + 1) % len(self._filtered_commands)
            self._render_commands()

    def action_select(self) -> None:
        if self._filtered_commands:
            cmd = self._filtered_commands[self._selected_idx][0]
            self.dismiss(cmd)

    def action_cancel(self) -> None:
        self.dismiss(None)
