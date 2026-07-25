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
        self._special_mode: str | None = None  # persistent "plan" / "loop" badge
        self._spinner_char: str = ""
        self._tool_name: str | None = None
        self._compress_info: str = ""
        self._exit_pending: bool = False
        self._loop_step: int = 0
        self._loop_total: int = 0
        self._queue_mode: bool = False  # 队列整理模式（Ctrl+G）徽章

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
        self._mode = "plan" if self._special_mode == "plan" else "idle"
        self._tool_name = None
        self._compress_info = ""
        self._spinner_char = ""
        self._exit_pending = False
        if self._special_mode != "loop":
            self._loop_step = 0
            self._loop_total = 0
        self.refresh()

    def set_plan_mode(self, active: bool) -> None:
        """设置 Plan Mode 状态"""
        if active:
            self._special_mode = "plan"
            if self._mode == "idle":
                self._mode = "plan"
        else:
            if self._special_mode == "plan":
                self._special_mode = None
            if self._mode == "plan":
                self._mode = "idle"
        self.refresh()

    def set_question_mode(self) -> None:
        """设置问询模式状态"""
        self._mode = "question"
        self.refresh()

    def set_queue_mode(self, active: bool) -> None:
        """设置队列整理模式（Ctrl+G）徽章状态。"""
        self._queue_mode = active
        self.refresh()

    def set_loop_progress(self, current_step: int, total_steps: int) -> None:
        """设置 Loop 模式进度"""
        self._special_mode = "loop"
        self._mode = "loop"
        self._loop_step = current_step
        self._loop_total = total_steps
        self.refresh()

    def set_loop_mode(self, active: bool, total_steps: int = 0) -> None:
        """Show or clear the persistent Loop Mode badge."""
        if active:
            self._special_mode = "loop"
            self._mode = "loop"
            self._loop_step = 0
            self._loop_total = total_steps
        else:
            if self._special_mode == "loop":
                self._special_mode = None
            if self._mode == "loop":
                self._mode = "idle"
            self._loop_step = 0
            self._loop_total = 0
        self.refresh()

    @property
    def special_mode(self) -> str | None:
        return self._special_mode

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

        table = Table.grid(padding=(0, 0), expand=True)
        table.add_column(ratio=1)
        width = max(40, self.size.width or 100)

        sl = self._sl
        total = sl.get_context_window_size()
        estimated = sl.estimated_prompt_tokens
        pct = (estimated / total * 100) if total > 0 else 0
        ctx_color = "green" if pct < 50 else ("yellow" if pct < 80 else "red")

        model = _truncate(sl.model_display or "Coomi", 22 if width >= 90 else 16)
        permission = _permission_label(sl.permission_label, compact=width < 90)
        cum = format_token_count(sl.cumulative_usage.total_tokens)
        ctx_text = f"ctx: {pct:.1f}% ({format_token_count(estimated)} / {format_token_count(total)})"

        if width < 62:
            top = f"[bold cyan]{model}[/bold cyan] | [{ctx_color}]{ctx_text}[/{ctx_color}]"
        elif width < 90:
            top = (
                f"[bold cyan]{model}[/bold cyan] | "
                f"{_permission_markup(permission)} | "
                f"[{ctx_color}]{ctx_text}[/{ctx_color}]"
            )
        else:
            top = (
                f"[bold cyan]{model}[/bold cyan] | "
                f"{_permission_markup(permission)} | "
                f"[{ctx_color}]{ctx_text}[/{ctx_color}] | "
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

        top_row = Table.grid(padding=(0, 0), expand=True)
        top_row.add_column(ratio=1)
        top_row.add_column(justify="right", no_wrap=True)
        top_row.add_row(top, self._mode_badge())
        table.add_row(top_row)
        table.add_row(bottom)
        return table

    def _mode_badge(self) -> str:
        """右上角模式徽章。

        优先级：持久模式（plan/loop）> 临时交互（queue/question/compressing）。
        plan/loop 是"我的会话在什么模式"的锚点，始终优先占据徽章；
        没有持久模式时才显示临时状态，避免遮掉锚点。
        """
        # 持久模式：最高优先级
        if self._special_mode == "plan":
            return _badge("⚡", "PLAN MODE", "black", "#d4a72c")
        if self._special_mode == "loop":
            progress = f" {self._loop_step}/{self._loop_total}" if self._loop_total else ""
            return _badge("🔁", f"LOOP MODE{progress}", "black", "#3fb950")
        # 临时交互：仅在无持久模式时显示
        if self._queue_mode:
            return _badge("≡", "QUEUE MODE", "black", "#58a6ff")
        if self._mode == "question":
            return _badge("◎", "ASK MODE", "black", "#e3b341")
        if self._mode == "compressing":
            return _badge("⇄", "COMPRESSING", "black", "#bc8cff")
        return ""


def _badge(icon: str, label: str, fg: str, bg: str) -> str:
    """统一样式的模式徽章：图标 + 短标签 + 左右留白 + 背景色。"""
    return f"[bold {fg} on {bg}] {icon} {label} [/bold {fg} on {bg}]"


def _truncate(value: str, max_len: int) -> str:
    if len(value) <= max_len:
        return value
    return value[:max(1, max_len - 1)] + "…"


def _permission_label(label: str, compact: bool) -> str:
    if not compact:
        return label
    mapping = {
        "Ask for approval": "Ask",
        "Approve for me": "Auto",
        "Full access": "Full",
    }
    return mapping.get(label, label)


def _permission_markup(label: str) -> str:
    colors = {
        "Ask for approval": "white",
        "Ask": "white",
        "Approve for me": "green",
        "Auto": "green",
        "Full access": "#ff9d00",
        "Full": "#ff9d00",
    }
    color = colors.get(label, "white")
    return f"[white]>>[/white] [{color}]{label}[/{color}]"
