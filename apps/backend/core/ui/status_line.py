"""状态栏 - 显示模型、上下文窗口、Token 使用情况"""
from __future__ import annotations

import time

from rich.console import Console
from rich.text import Text

from ..types import TokenUsage

# DeepSeek 上下文窗口大小
CONTEXT_WINDOW_SIZES: dict[str, int] = {
    "deepseek-v4-pro": 128_000,
    "deepseek-v4-flash": 128_000,
}

DEFAULT_CONTEXT_WINDOW = 128_000


class StatusLine:
    """持久化状态栏，显示模型和 token 使用情况

    类似 Claude Code 的状态栏：
    - 显示当前模型名称
    - 显示上下文窗口使用百分比
    - 显示输入/输出 token 数量
    """

    def __init__(self, console: Console):
        """
        Args:
            console: Rich Console 实例
        """
        self.console = console
        self._model_name: str = ""
        self._model_display: str = ""
        self._usage: TokenUsage = TokenUsage()
        self._context_window_size: int = DEFAULT_CONTEXT_WINDOW
        self._last_render_time: float = 0
        self._debounce_ms: float = 300  # 300ms 防抖，类似 Claude Code

    def set_model(self, model_name: str, display_name: str) -> None:
        """设置当前模型

        Args:
            model_name: 模型标识名
            display_name: 人类可读的显示名称
        """
        self._model_name = model_name
        self._model_display = display_name
        self._context_window_size = CONTEXT_WINDOW_SIZES.get(
            model_name.lower(), DEFAULT_CONTEXT_WINDOW
        )

    def update_usage(self, usage: dict[str, int]) -> None:
        """从 API 响应更新使用量

        Args:
            usage: 包含 prompt_tokens, completion_tokens, total_tokens 的字典
        """
        self._usage = TokenUsage(
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
        )
        self._schedule_render()

    def update_session_usage(self, token_usage: TokenUsage) -> None:
        """从累积的会话使用量更新

        Args:
            token_usage: TokenUsage 实例
        """
        self._usage = token_usage

    def _schedule_render(self) -> None:
        """调度渲染（带防抖）"""
        now = time.time() * 1000
        if now - self._last_render_time >= self._debounce_ms:
            self._last_render_time = now
            self._render()

    def _render(self) -> None:
        """渲染状态栏"""
        used = self._usage.total_tokens
        total = self._context_window_size
        pct = (used / total * 100) if total > 0 else 0

        status_parts = []
        if self._model_display:
            status_parts.append(f"[bold cyan]{self._model_display}[/bold cyan]")

        # 上下文使用百分比
        if pct < 50:
            ctx_style = "green"
        elif pct < 80:
            ctx_style = "yellow"
        else:
            ctx_style = "red"

        status_parts.append(
            f"[{ctx_style}]ctx: {pct:.1f}%[/{ctx_style}]"
        )

        # Token 数量
        status_parts.append(
            f"[dim]{self._usage.input_tokens:,} in / {self._usage.output_tokens:,} out[/dim]"
        )

        status_text = " | ".join(status_parts)
        self.console.print(f"\n{status_text}")

    def render_final(self) -> None:
        """强制渲染（响应结束时调用）"""
        self._render()

    def get_status_text(self) -> str:
        """获取状态文本（用于测试）

        Returns:
            str: 状态文本
        """
        used = self._usage.total_tokens
        total = self._context_window_size
        pct = (used / total * 100) if total > 0 else 0
        return f"{self._model_display} | ctx: {pct:.1f}% ({self._usage.input_tokens:,} in / {self._usage.output_tokens:,} out)"
