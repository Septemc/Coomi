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
from textual.binding import Binding
from textual.screen import Screen

from ..widgets.custom_header import CustomHeader
from ..widgets.prompt_text_area import PromptTextArea
from ..widgets.selectable_rich_log import SelectableRichLog
from ..widgets.status_panel import StatusPanel
from ..widgets.streaming_preview import StreamingPreview
from ..widgets.welcome_panel import WelcomePanel


PROMPT_PLACEHOLDER = '输入消息（Enter发送，Shift+Enter换行，输入“/”查看指令，双Esc退出）'


class MainScreen(Screen):
    """主交互屏幕"""

    CSS = """
    #prompt-input {
        dock: bottom;
        height: 5;
        padding: 0 1;
        background: #1c2333;
        border: solid #30363d;
    }

    #prompt-input:focus {
        background: #1c2333;
        border: solid #00a8df;
    }
    """

    BINDINGS = [
        ("escape", "cancel_or_exit", "Cancel / Exit"),
        ("ctrl+r", "toggle_reasoning", "Toggle reasoning"),
        Binding("ctrl+c", "copy_selected", "Copy selected", priority=True, show=False),
        Binding("shift+tab", "cycle_permission_mode", "Permission mode", priority=True),
    ]

    def __init__(self, status_line=None, **kwargs):
        super().__init__(**kwargs)
        self._status_line = status_line

    def compose(self) -> ComposeResult:
        yield CustomHeader()
        yield WelcomePanel(id="welcome-panel")
        yield SelectableRichLog(id="message-log", markup=True, wrap=True, highlight=True)
        yield StreamingPreview(id="stream-preview")
        yield StatusPanel(self._status_line, id="status-panel")
        yield PromptTextArea(
            id="prompt-input",
            placeholder='输入消息（Enter发送，Shift+Enter换行，输入"/"查看指令，双Esc退出）',
        )

    def on_mount(self) -> None:
        self.prompt_input.placeholder = PROMPT_PLACEHOLDER
        self.message_log.display = False
        self.welcome_panel.display = True
        self.call_after_refresh(self.focus_prompt_input)

    @property
    def welcome_panel(self) -> WelcomePanel:
        return self.query_one("#welcome-panel", WelcomePanel)

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

    def focus_prompt_input(self) -> None:
        """Keep keyboard input anchored to the bottom prompt."""
        try:
            self.prompt_input.focus()
        except Exception:
            pass

    def show_welcome_panel(self) -> None:
        self.message_log.display = False
        self.welcome_panel.display = True
        self.call_after_refresh(self.focus_prompt_input)

    def hide_welcome_panel(self) -> None:
        self.welcome_panel.display = False
        self.message_log.display = True
        self.call_after_refresh(self.focus_prompt_input)

    def update_welcome_panel(self, model_display: str, tool_count: int, sessions=None) -> None:
        self.welcome_panel.set_context(model_display, tool_count, sessions=sessions)

    def action_copy_selected(self) -> None:
        """复制选中的文本"""
        try:
            prompt = self.query_one("#prompt-input", PromptTextArea)
            selected_text = prompt.selected_text
            if selected_text:
                self.app.copy_to_clipboard(selected_text)
                return
        except Exception:
            pass
        try:
            header = self.query_one(CustomHeader)
            selected_text = header.get_selected_text()
            if selected_text:
                self.app.copy_to_clipboard(selected_text)
                return
        except Exception:
            pass
        try:
            log = self.query_one("#message-log", SelectableRichLog)
            selected_text = log.get_selected_text()
            if selected_text:
                self.app.copy_to_clipboard(selected_text)
                return
        except Exception:
            pass

    def action_cycle_permission_mode(self) -> None:
        """切换工具权限模式"""
        self.app.action_cycle_permission_mode()

    def action_cancel_or_exit(self) -> None:
        """ESC 键处理：如果有选择则清除，否则退出"""
        try:
            header = self.query_one(CustomHeader)
            if header.has_selection():
                header.clear_selection()
                return
        except Exception:
            pass
        try:
            log = self.query_one("#message-log", SelectableRichLog)
            if log.has_selection():
                log.clear_selection()
                return
        except Exception:
            pass
        # 如果没有选择，执行退出逻辑
        self.app.action_cancel_or_exit()
