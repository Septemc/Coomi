"""CommPanel — 执行期额外交流窗口

位于主输入框下方，仅在 agent 执行过程中显示。分两部分：
  - 上半「执行活动」：阶段级实时状态（复用 ToolStart/ToolRunning/ToolDone），
    显示当前正在跑的工具/命令，以及最近几条已完成步骤。
  - 下半「交流输入」：一个独立小输入框，提交内容进入待执行队列
    （方案甲：与主输入框回车等价，视觉上和活动区在一起）。

设计要点：
  - 执行结束即隐藏（display=False）。
  - 活动区只保留最近 N 条，避免无限增长。
  - 下半输入框用独立 Submitted 消息类型，供 App 区分来源后统一入队。
"""
from __future__ import annotations

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Static, TextArea

from ..tool_formatter import format_tool_display


class CommInput(TextArea):
    """额外交流窗口下半部分的独立输入框。

    行为与主 PromptTextArea 一致：Enter 提交、Shift/Ctrl+Enter/Ctrl+J 换行。
    但提交时 post 独立的 Submitted 消息，供 App 识别来源（进入队列）。
    """

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            self.text = text
            super().__init__()

    BINDINGS = [
        Binding("ctrl+enter", "insert_newline", "Insert Newline", show=False),
        Binding("shift+enter", "insert_newline", "Insert Newline", show=False),
        Binding("ctrl+j", "insert_newline", "Insert Newline", show=False),
        Binding("ctrl+a", "select_all", "Select All", show=False),
        Binding("ctrl+c", "copy", "Copy", show=False),
        Binding("ctrl+v", "paste", "Paste", show=False),
        Binding("ctrl+x", "cut", "Cut", show=False),
    ]

    def action_insert_newline(self) -> None:
        self._replace_via_keyboard("\n", *self.selection)

    async def _on_key(self, event: events.Key) -> None:
        if event.key in {"shift+enter", "ctrl+enter", "ctrl+j"}:
            event.stop()
            event.prevent_default()
            self.action_insert_newline()
        elif event.key == "enter":
            event.stop()
            event.prevent_default()
            text = self.text.strip()
            if text:
                self.post_message(self.Submitted(text))
        else:
            await super()._on_key(event)


class CommPanel(Vertical):
    """执行期额外交流窗口 — 输入框下方，上半活动、下半交流。"""

    # 活动区最多保留的已完成步骤条数
    MAX_ACTIVITY_LINES = 4

    DEFAULT_CSS = """
    CommPanel {
        dock: bottom;
        height: auto;
        max-height: 10;
        padding: 0 1;
        background: #12161f;
        border-top: solid #30363d;
        display: none;
    }

    CommPanel #comm-activity {
        height: auto;
        max-height: 6;
        color: #8b949e;
    }

    CommPanel #comm-input {
        height: 3;
        margin-top: 0;
        background: #1c2333;
        border: solid #30363d;
    }

    CommPanel #comm-input:focus {
        border: solid #00a8df;
    }
    """

    COMM_PLACEHOLDER = "临时交流（Enter 追加到待执行队列 · 执行结束后依次发送）"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 已完成步骤（最近 N 条）与当前进行中的步骤
        self._done_lines: list[str] = []
        self._current: str | None = None

    def compose(self) -> ComposeResult:
        yield Static("", id="comm-activity")
        yield CommInput(id="comm-input")

    # -- 显隐控制 -----------------------------------------------------------

    def show_panel(self) -> None:
        self._done_lines.clear()
        self._current = None
        self.display = True
        self._render_activity()

    def hide_panel(self) -> None:
        self.display = False
        self._done_lines.clear()
        self._current = None

    # -- 活动区更新（由 App 在消费事件时调用）--------------------------------

    def set_current(self, tool_name: str, arguments: dict | None = None) -> None:
        """ToolStart / ToolRunning：设置当前进行中的步骤。"""
        display = format_tool_display(tool_name, arguments or {})
        self._current = f"⟳ {display}"
        self._render_activity()

    def push_done(self, tool_name: str, arguments: dict | None = None, is_error: bool = False) -> None:
        """ToolDone：把当前步骤归档为已完成。"""
        display = format_tool_display(tool_name, arguments or {})
        icon = "×" if is_error else "✓"
        self._done_lines.append(f"{icon} {display}")
        if len(self._done_lines) > self.MAX_ACTIVITY_LINES:
            self._done_lines = self._done_lines[-self.MAX_ACTIVITY_LINES:]
        self._current = None
        self._render_activity()

    def set_status(self, text: str) -> None:
        """无工具阶段（等待模型/准备上下文）时的当前状态。"""
        self._current = f"◎ {text}"
        self._render_activity()

    def _render_activity(self) -> None:
        try:
            activity = self.query_one("#comm-activity", Static)
        except Exception:
            return
        lines: list[str] = []
        for line in self._done_lines:
            lines.append(f"[dim]{line}[/dim]")
        if self._current:
            lines.append(f"[bold yellow]{self._current}[/bold yellow]")
        if not lines:
            lines.append("[dim]准备中…[/dim]")
        activity.update("\n".join(lines))

    # -- 输入框访问 ---------------------------------------------------------

    @property
    def comm_input(self) -> CommInput:
        return self.query_one("#comm-input", CommInput)
