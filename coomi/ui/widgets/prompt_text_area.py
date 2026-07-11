"""PromptTextArea — 自定义 TextArea，Enter 发送，Shift/Ctrl+Enter 换行

重写 _on_key 拦截 Enter，在 TextArea 原生处理之前处理：
- Enter → 提交（post Submitted message）
- Shift/Ctrl+Enter → 插入换行

注意：Textual 的 Key 事件用 key 字符串表示修饰键（如 "ctrl+enter"），
不是布尔属性 event.shift / event.ctrl。
"""
from __future__ import annotations

from textual import events
from textual.binding import Binding
from textual.message import Message
from textual.widgets import TextArea


class PromptTextArea(TextArea):
    """自定义 TextArea — Enter 发送, Ctrl+Enter 换行"""

    class Submitted(Message):
        """用户按 Enter 提交"""

        def __init__(self, text: str) -> None:
            self.text = text
            super().__init__()

    BINDINGS = [
        Binding("ctrl+enter", "insert_newline", "Insert Newline", show=False),
        Binding("shift+enter", "insert_newline", "Insert Newline", show=False),
        Binding("ctrl+a", "select_all", "Select All", show=False),
        Binding("ctrl+c", "copy", "Copy", show=False),
        Binding("ctrl+v", "paste", "Paste", show=False),
        Binding("ctrl+x", "cut", "Cut", show=False),
        Binding("ctrl+z", "undo", "Undo", show=False),
        Binding("ctrl+y", "redo", "Redo", show=False),
    ]

    def action_insert_newline(self) -> None:
        """Ctrl+Enter → 插入换行"""
        self._replace_via_keyboard("\n", *self.selection)

    async def _on_key(self, event: events.Key) -> None:
        """重写 _on_key — 在 TextArea 原生处理之前拦截 Enter"""
        if event.key in {"shift+enter", "ctrl+enter"}:
            event.stop()
            event.prevent_default()
            self.action_insert_newline()
        elif event.key == "enter":
            # Enter → 提交（Ctrl+Enter 由 BINDINGS 处理，不会到达这里）
            event.stop()
            event.prevent_default()
            text = self.text.strip()
            if text:
                self.post_message(self.Submitted(text))
        else:
            # 其他键交给 TextArea 原生处理
            await super()._on_key(event)
