"""CommPanel — 额外临时交流窗口

一个独立、可随时开关的交流窗口，位于主输入框上方。

设计要点：
  - 默认隐藏（display=False），仅在用户按 Ctrl+T 时打开，再按 Ctrl+T 关闭。
  - 不与 agent 执行绑定，不显示主任务工具进度（主任务进度仍走 StreamingPreview + banner）。
  - 打开后聚焦交流输入框；鼠标点击可在主输入框 / 交流区之间切换焦点。
  - 交流输入框回车提交：
      · 主任务的工具/PowerShell 正在阻塞执行时 —— 立即在独立只读旁路会话上并发执行，
        回复直接显示在本窗口的回复区（仅限本次，不污染主线，不触发主队列）。
      · 其余情况 —— 内容进入「交流队列」，在当前 agent 轮次结束后优先插入执行。
"""
from __future__ import annotations

from rich.markdown import Markdown

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import RichLog, Static, TextArea


class CommInput(TextArea):
    """交流窗口的独立输入框。

    行为与主 PromptTextArea 一致：Enter 提交、Shift/Ctrl+Enter/Ctrl+J 换行。
    但提交时 post 独立的 Submitted 消息，供 App 识别来源。
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
                self.clear()
        else:
            await super()._on_key(event)


class CommPanel(Vertical):
    """额外临时交流窗口 — 主输入框上方，可 Ctrl+T 开关。"""

    DEFAULT_CSS = """
    CommPanel {
        dock: bottom;
        height: auto;
        padding: 0 1 1 1;
        background: #12161f;
        border-top: solid #2d7d9a;
        display: none;
    }

    CommPanel #comm-title {
        height: 1;
        color: #58d0e8;
        text-style: bold;
    }

    CommPanel #comm-output {
        height: auto;
        max-height: 12;
        background: #0d1117;
        border: solid #1f6f8b;
        padding: 0 1;
        display: none;
        scrollbar-size-vertical: 1;
    }

    CommPanel #comm-input {
        height: 3;
        background: #1c2333;
        border: solid #2d7d9a;
    }

    CommPanel #comm-input:focus {
        border: solid #58d0e8;
    }
    """

    COMM_PLACEHOLDER = "输入临时交流内容（Enter 发送 · 工具执行中即时并发只读作答 · 否则排队轮末插入 · 仅限本次）"

    def compose(self) -> ComposeResult:
        yield Static("", id="comm-title")
        yield RichLog(id="comm-output", wrap=True, markup=True, highlight=False)
        yield CommInput(id="comm-input", placeholder=self.COMM_PLACEHOLDER)

    def on_mount(self) -> None:
        self._busy: bool = False
        self._has_output: bool = False
        self._render_title()

    # -- 显隐控制 -----------------------------------------------------------

    def open_panel(self) -> None:
        """打开交流窗口并聚焦输入框。"""
        self.display = True
        self._render_title()
        try:
            self.comm_input.focus()
        except Exception:
            pass

    def close_panel(self) -> None:
        """关闭交流窗口。"""
        self.display = False

    @property
    def is_open(self) -> bool:
        return bool(self.display)

    # -- 标题（含交流队列计数 / 生成中状态）----------------------------------

    def set_queue_count(self, count: int) -> None:
        """更新标题里的交流队列条数。"""
        self._queue_count = count
        self._render_title()

    def set_busy(self, busy: bool) -> None:
        """side 交流生成中标志，反映到标题。"""
        self._busy = busy
        self._render_title()

    def _render_title(self) -> None:
        try:
            title = self.query_one("#comm-title", Static)
        except Exception:
            return
        count = getattr(self, "_queue_count", 0)
        if getattr(self, "_busy", False):
            title.update("💬 临时交流   [#e3b341]● 并发只读作答中…[/#e3b341]   [dim]Ctrl+T 关闭[/dim]")
            return
        pending = f"  [dim]· 队列 {count} 条待插入[/dim]" if count else ""
        title.update(f"💬 临时交流{pending}   [dim]Ctrl+T 关闭[/dim]")

    # -- side 回复显示区 ----------------------------------------------------
    #    以下方法由 engine.side_session.run_side_conversation 调用。

    def _output(self) -> RichLog | None:
        try:
            return self.query_one("#comm-output", RichLog)
        except Exception:
            return None

    def begin_reply(self, prompt_text: str) -> None:
        """一次 side 交流开始：显示回复区并回显用户这条交流内容。"""
        out = self._output()
        if out is None:
            return
        out.display = True
        self._has_output = True
        out.write(f"[bold #58d0e8]你（临时）:[/bold #58d0e8] {prompt_text}")
        out.write("[dim]…只读并发作答中[/dim]")

    def set_thinking(self) -> None:
        """生成中的思考/工具间隙提示（仅更新标题，不写回复区）。"""
        self.set_busy(True)

    def set_tool_status(self, tool_name: str) -> None:
        """side 只读工具调用的轻量状态提示。"""
        out = self._output()
        if out is None:
            return
        out.write(f"[dim]🔍 查阅 {tool_name}…[/dim]")

    def show_reply_streaming(self, reply: str) -> None:
        """生成过程中的流式预览（保持忙碌标题；正文定稿在 append_reply）。"""
        self.set_busy(True)

    def append_reply(self, reply: str, error: str | None = None) -> None:
        """一次 side 交流结束：把最终整块回复写入回复区。"""
        out = self._output()
        if out is None:
            return
        if reply.strip():
            out.write(Markdown(reply))
        if error:
            out.write(f"[red]{error}[/red]")
        out.write("[dim]— 交流结束，主任务继续 —[/dim]")

    def clear_output(self) -> None:
        """清空并隐藏回复区。"""
        out = self._output()
        if out is None:
            return
        out.clear()
        out.display = False
        self._has_output = False

    # -- 输入框访问 ---------------------------------------------------------

    @property
    def comm_input(self) -> CommInput:
        return self.query_one("#comm-input", CommInput)
