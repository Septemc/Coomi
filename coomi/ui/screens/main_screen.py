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

from ..widgets.custom_header import CustomHeader
from ..widgets.prompt_text_area import PromptTextArea
from ..widgets.selectable_rich_log import SelectableRichLog
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
        ("ctrl+c", "copy_selected", "Copy selected"),
    ]

    def __init__(self, status_line=None, **kwargs):
        super().__init__(**kwargs)
        self._status_line = status_line

    def compose(self) -> ComposeResult:
        yield CustomHeader()
        yield SelectableRichLog(id="message-log", markup=True, wrap=True, highlight=True)
        yield StreamingPreview(id="stream-preview")
        yield StatusPanel(self._status_line, id="status-panel")
        yield PromptTextArea(
            id="prompt-input",
            placeholder='输入消息（Enter发送，Shift+Enter换行，输入"/"查看指令，ESC退出）',
        )

    @property
    def message_log(self) -> SelectableRichLog:
        return self.query_one("#message-log", SelectableRichLog)

    @property
    def stream_preview(self) -> StreamingPreview:
        return self.query_one("#stream-preview", StreamingPreview)

    @property
    def status_panel(self) -> StatusPanel:
        return self.query_one("#status-panel", StatusPanel)

    @property
    def prompt_input(self) -> PromptTextArea:
        return self.query_one("#prompt-input", PromptTextArea)

    def action_copy_selected(self) -> None:
        """复制选中的文本"""
        try:
            log = self.query_one("#message-log", SelectableRichLog)
            selected_text = log.get_selected_text()
            if selected_text:
                self.app.copy_to_clipboard(selected_text)
        except Exception:
            pass

    def action_cancel_or_exit(self) -> None:
        """ESC 键处理：如果有选择则清除，否则退出"""
        try:
            log = self.query_one("#message-log", SelectableRichLog)
            if log.has_selection():
                log.clear_selection()
                return
        except Exception:
            pass
        # 如果没有选择，执行退出逻辑
        self.app.action_cancel_or_exit()
