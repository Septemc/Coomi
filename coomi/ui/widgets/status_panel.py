"""StatusPanel — 底部状态栏 widget

持有 StatusLine 数据对象引用，从 StatusLine 读取模型/context/token 数据。
自身管理 spinner、tool_name、mode 等 UI 状态。
"""
from __future__ import annotations

from textual.widget import Widget

from ..status_line import StatusLine, format_token_count

SPINNER_CHARS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class StatusPanel(Widget):
    """底部状态栏 — 模型名称 + ctx% + token 用量 + spinner + tool 状态."""

    DEFAULT_CSS = """
    StatusPanel {
        height: 2;
        padding: 0 1;
    }
    """

    def __init__(self, status_line: StatusLine, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sl = status_line
        self._mode: str = "idle"       # "idle" | "executing" | "compressing" | "plan" | "question" | "loop"
        self._spinner_char: str = ""
        self._tool_name: str | None = None
        self._compress_info: str = ""
        self._exit_pending: bool = False
        self._loop_step: int = 0
        self._loop_total: int = 0

    # -- public mutation API ------------------------------------------------

    def set_executing(self, tool_name: str | None = None) -> None:
        self._mode = "executing"
        self._tool_name = tool_name
        self.refresh()

    def set_compressing(self, before: int, after: int) -> None:
        self._mode = "compressing"
        self._compress_info = f"{before} -> {after} messages"
        self.refresh()

    def set_idle(self) -> None:
        self._mode = "idle"
        self._tool_name = None
        self._compress_info = ""
        self._spinner_char = ""
        self._exit_pending = False
        self._loop_step = 0
        self._loop_total = 0
        self.refresh()

    def set_plan_mode(self, active: bool) -> None:
        """设置 Plan Mode 状态"""
        if active:
            self._mode = "plan"
        else:
            self._mode = "idle"
        self.refresh()

    def set_question_mode(self) -> None:
        """设置问询模式状态"""
        self._mode = "question"
        self.refresh()

    def set_loop_progress(self, current_step: int, total_steps: int) -> None:
        """设置 Loop 模式进度"""
        self._mode = "loop"
        self._loop_step = current_step
        self._loop_total = total_steps
        self.refresh()

    def set_spinner(self, char: str) -> None:
        self._spinner_char = char
        self.refresh()

    def set_exit_pending(self) -> None:
        self._exit_pending = True
        self.refresh()

    def reset_exit_pending(self) -> None:
        self._exit_pending = False
        self.refresh()

    # -- render -------------------------------------------------------------

    def render(self):
        from rich.table import Table

        table = Table.grid(padding=(0, 0))
        table.add_column(ratio=1)

        sl = self._sl
        total = sl.get_context_window_size()
        estimated = sl.estimated_prompt_tokens
        pct = (estimated / total * 100) if total > 0 else 0
        ctx_color = "green" if pct < 50 else ("yellow" if pct < 80 else "red")

        model = sl.model_display or "Coomi"
        permission = sl.permission_label
        cum = format_token_count(sl.cumulative_usage.total_tokens)

        top = (
            f"[bold cyan]{model}[/bold cyan] | "
            f"[yellow]🛡 {permission}[/yellow] | "
            f"[{ctx_color}]ctx: {pct:.1f}% ({format_token_count(estimated)} / {format_token_count(total)})[/{ctx_color}] | "
            f"[dim]cum: {cum} tokens[/dim]"
        )

        if self._exit_pending and self._mode == "idle":
            bottom = "[bold yellow]Press Esc again to exit[/bold yellow]"
        elif self._mode == "question":
            bottom = (
                "[bold yellow]◎ 等待用户回答...[/bold yellow] "
                "[dim]| ←→ 切换问题  ↑↓ 选选项  Enter 确认  Esc 取消[/dim]"
            )
        elif self._mode == "plan":
            bottom = (
                "[bold yellow]⚡ Plan Mode[/bold yellow] "
                "[dim]| Esc to exit plan[/dim]"
            )
        elif self._mode == "loop":
            bottom = (
                f"[bold green]🔁 Loop: Step {self._loop_step}/{self._loop_total}[/bold green] "
                "[dim]| Esc to cancel[/dim]"
            )
        elif self._mode == "compressing":
            bottom = f"[bold yellow]{self._spinner_char} Compress: {self._compress_info}[/bold yellow]"
        elif self._mode == "executing":
            if self._tool_name:
                bottom = f"[bold yellow]{self._spinner_char} Executing {self._tool_name}...[/bold yellow] [dim]| Esc to cancel[/dim]"
            else:
                bottom = f"[bold yellow]{self._spinner_char} Thinking...[/bold yellow] [dim]| Esc to cancel[/dim]"
        else:
            bottom = "[dim]Ready[/dim]"

        table.add_row(top)
        table.add_row(bottom)
        return table
