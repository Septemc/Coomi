"""StreamingPreview — 流式文本预览控件

职责：
1. 显示 "◎ Thinking..." 状态指示
2. 显示 "⟳ ToolName..." 工具状态
3. 流式 Markdown 文本预览（截取最后 N 字符，50ms 节流）
"""
from __future__ import annotations

from rich.text import Text
from textual.widgets import Static


class StreamingPreview(Static):
    """流式文本预览控件，位于 RichLog 和 StatusPanel 之间。"""

    PREVIEW_MAX_CHARS = 8000
    PREVIEW_MAX_LINES = 120

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._throttle_timer = None
        self._pending_text: str | None = None

    def show_text(self, text: str) -> None:
        """显示流式文本预览（50ms 节流）。"""
        self._pending_text = text
        self._start_throttle()

    def _start_throttle(self) -> None:
        if self._throttle_timer is not None:
            return
        if not self.is_mounted:
            return
        self._throttle_timer = self.set_timer(0.05, self._flush_text)

    def _flush_text(self) -> None:
        self._throttle_timer = None
        if self._pending_text is None:
            return
        text = self._pending_text
        self._pending_text = None
        preview = self._build_preview(text)
        self.update(Text(preview))
        try:
            self.scroll_end(animate=False)
        except Exception:
            pass

    def flush_pending(self) -> None:
        """Immediately render pending text before another preview state replaces it."""
        if self._throttle_timer is not None:
            self._throttle_timer.stop()
            self._throttle_timer = None
        self._flush_text()

    def show_thinking(self) -> None:
        """显示 thinking 状态。"""
        self._cancel_throttle()
        self.update("[bold yellow]◎ Thinking...[/bold yellow]")

    def show_reasoning(self, text: str) -> None:
        """显示实时推理内容。"""
        self._cancel_throttle()
        preview = self._build_preview(text)
        output = Text("◎ Thinking\n", style="bold yellow")
        output.append(preview, style="dim")
        self.update(output)
        try:
            self.scroll_end(animate=False)
        except Exception:
            pass

    def show_tool(self, tool_name: str) -> None:
        """显示工具执行状态。"""
        self._cancel_throttle()
        self.update(f"[bold yellow]⟳ {tool_name}...[/bold yellow]")

    def clear_preview(self) -> None:
        """清空预览区。"""
        self._cancel_throttle()
        self._pending_text = None
        self.update("")

    def _cancel_throttle(self) -> None:
        if self._throttle_timer is not None:
            self._throttle_timer.stop()
            self._throttle_timer = None

    def _build_preview(self, text: str) -> str:
        if len(text) > self.PREVIEW_MAX_CHARS:
            text = "... [earlier streaming output omitted]\n" + text[-self.PREVIEW_MAX_CHARS:]
        lines = text.splitlines()
        if len(lines) > self.PREVIEW_MAX_LINES:
            lines = ["... [earlier streaming lines omitted]", *lines[-self.PREVIEW_MAX_LINES:]]
        return "\n".join(lines) if lines else text
