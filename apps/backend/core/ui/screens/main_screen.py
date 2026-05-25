"""MainScreen — 主交互屏幕

布局:
  Screen
    ├── Header (dock: top, height: 1)    — 模型名 + 快捷键提示
    ├── RichLog (#message-log)           — 消息历史 + 流式输出 (flex-grow)
    ├── StreamingPreview (#stream-preview) — 实时流式预览 (height: auto)
    ├── StatusPanel (#status-panel)      — 状态栏 (height: 2)
    └── PromptTextArea (#prompt-input)   — 用户输入 (dock: bottom)
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Header, RichLog

from ..widgets.prompt_text_area import PromptTextArea
from ..widgets.status_panel import StatusPanel
from ..widgets.streaming_preview import StreamingPreview


class MainScreen(Screen):
    """主交互屏幕"""

    CSS = """
    #prompt-input {
        dock: bottom;
        height: 5;
        background: #1c2333;
        border-top: solid #30363d;
    }

    #prompt-input:focus {
        background: #1c2333;
    }
    """

    BINDINGS = [
        ("escape", "cancel_or_exit", "Cancel / Exit"),
        ("ctrl+r", "toggle_reasoning", "Toggle reasoning"),
    ]

    def __init__(self, status_line=None, **kwargs):
        super().__init__(**kwargs)
        self._status_line = status_line

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield RichLog(id="message-log", markup=True, wrap=True, highlight=True)
        yield StreamingPreview(id="stream-preview")
        yield StatusPanel(self._status_line, id="status-panel")
        yield PromptTextArea(
            id="prompt-input",
            placeholder='输入消息（Enter 发送，Shift+Enter换行，输入"/"查看指令）',
        )

    @property
    def message_log(self) -> RichLog:
        return self.query_one("#message-log", RichLog)

    @property
    def stream_preview(self) -> StreamingPreview:
        return self.query_one("#stream-preview", StreamingPreview)

    @property
    def status_panel(self) -> StatusPanel:
        return self.query_one("#status-panel", StatusPanel)

    @property
    def prompt_input(self) -> PromptTextArea:
        return self.query_one("#prompt-input", PromptTextArea)
