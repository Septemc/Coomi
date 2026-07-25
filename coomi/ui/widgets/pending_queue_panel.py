"""PendingQueuePanel — 待执行队列面板

执行过程中用户按 Enter 追加的普通文本进入待执行队列，本面板负责展示。
通过 Ctrl+G 进入"队列选择模式"后：
  - ↑/↓ 切换选中的消息
  - ←/→ 切换动作（插队 / 置顶 / 编辑 / 删除）
  - Enter 对选中消息执行选中动作
  - Esc 退出队列模式

队列项文本做截断显示（前缀 + 省略号），后面留白给可选功能。
"""
from __future__ import annotations

from rich.console import Group
from rich.text import Text
from textual.widget import Widget

# 动作定义：顺序即 ←→ 循环顺序，索引与 _queue_action_index 对应
QUEUE_ACTIONS = ("插队", "置顶", "编辑", "删除")


class PendingQueuePanel(Widget):
    """待执行队列面板 — 位于 StatusPanel 与 PromptTextArea 之间。"""

    can_focus = True

    DEFAULT_CSS = """
    PendingQueuePanel {
        height: auto;
        max-height: 8;
        padding: 0 1;
        background: #161b22;
        border-top: solid #30363d;
        display: none;
    }

    PendingQueuePanel:focus {
        border-top: solid #3fb950;
    }
    """

    # 单条消息截断显示的最大字符数
    ITEM_MAX_CHARS = 48

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._items: list[str] = []
        self._selecting: bool = False       # 是否处于队列选择模式
        self._msg_index: int = 0            # 当前选中的消息
        self._action_index: int = 0         # 当前选中的动作

    # -- public API ---------------------------------------------------------

    def update_state(
        self,
        items: list[str],
        selecting: bool,
        msg_index: int,
        action_index: int,
    ) -> None:
        """由 App 统一驱动刷新。传入队列快照与选择状态。"""
        self._items = list(items)
        self._selecting = selecting
        self._msg_index = msg_index
        self._action_index = action_index
        # 队列为空则整体隐藏
        self.display = bool(self._items)
        self.refresh()

    # -- rendering ----------------------------------------------------------

    def _truncate(self, text: str) -> str:
        """单行截断显示：去换行 + 前缀 + 省略号。"""
        flat = " ".join(text.split())
        if len(flat) > self.ITEM_MAX_CHARS:
            return flat[: self.ITEM_MAX_CHARS] + "…"
        return flat

    def _render_action_bar(self) -> Text:
        """渲染动作条：插队 | 置顶 | 编辑 | 删除，高亮当前动作。"""
        bar = Text()
        bar.append("  动作: ", style="dim")
        for i, action in enumerate(QUEUE_ACTIONS):
            if i > 0:
                bar.append(" ", style="dim")
            if i == self._action_index:
                bar.append(f" {action} ", style="bold black on #3fb950")
            else:
                bar.append(f" {action} ", style="dim")
        bar.append("   ←→ 切换动作 · Enter 确认 · Esc 退出", style="dim")
        return bar

    def render(self):
        if not self._items:
            return Text("")

        lines: list[Text] = []
        total = len(self._items)
        header = Text()
        if self._selecting:
            header.append("待执行队列 ", style="bold #d4a72c")
            header.append(f"({total}) — 队列选择模式", style="dim")
        else:
            header.append("待执行队列 ", style="bold #d4a72c")
            header.append(f"({total}) — Ctrl+G 整理", style="dim")
        lines.append(header)

        for i, item in enumerate(self._items):
            row = Text()
            selected = self._selecting and i == self._msg_index
            if selected:
                row.append(" ▸ ", style="bold #3fb950")
            else:
                row.append(f" {i + 1}. ", style="dim")
            row.append(
                self._truncate(item),
                style="bold white" if selected else "white",
            )
            lines.append(row)

        if self._selecting:
            lines.append(self._render_action_bar())

        return Group(*lines)
