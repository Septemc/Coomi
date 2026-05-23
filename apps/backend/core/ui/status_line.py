"""状态栏 - 显示模型、上下文窗口、Token 使用情况"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from rich.console import Console

from ..types import TokenUsage

# 默认上下文窗口大小
DEFAULT_CONTEXT_WINDOW = 256_000

# 状态文件路径
def _get_state_path() -> Path:
    return Path.cwd() / ".coomi" / "state.json"


def _load_persisted_window() -> int | None:
    """从 .coomi/state.json 加载上次保存的窗口大小"""
    try:
        state_path = _get_state_path()
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            size = state.get("context_window_size")
            if isinstance(size, int) and size >= 1_000:
                return size
    except Exception:
        pass
    return None


def _save_persisted_window(size: int) -> None:
    """保存窗口大小到 .coomi/state.json"""
    try:
        state_path = _get_state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state = {}
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding="utf-8"))
        state["context_window_size"] = size
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass


def format_token_count(tokens: int) -> str:
    """格式化 token 数量为人类可读格式"""
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    elif tokens >= 1_000:
        return f"{tokens / 1_000:.1f}K"
    else:
        return str(tokens)


class StatusLine:
    """持久化状态栏，显示模型和 token 使用情况

    两行显示：
    - 第一行：模型 | ctx 百分比（当前估算 prompt / 窗口）
    - 第二行：累计 token 消耗

    上下文窗口加载优先级：/context 命令 > state.json > .env > 默认 256K
    """

    def __init__(self, console: Console):
        self.console = console
        self._model_name: str = ""
        self._model_display: str = ""
        self._cumulative_usage: TokenUsage = TokenUsage()  # 会话累计
        self._estimated_prompt_tokens: int = 0  # 当前 prompt 估算（每次 render 前更新）
        self._last_render_time: float = 0
        self._debounce_ms: float = 300
        self._window_from_command: int | None = None  # /context 命令设置的值

        # 上下文窗口：优先级 /context > state.json > .env > 默认
        if self._window_from_command:
            self._context_window_size = self._window_from_command
        else:
            persisted = _load_persisted_window()
            if persisted:
                self._context_window_size = persisted
            else:
                env_size = os.getenv("CONTEXT_WINDOW_SIZE")
                if env_size:
                    try:
                        self._context_window_size = int(env_size)
                    except ValueError:
                        self._context_window_size = DEFAULT_CONTEXT_WINDOW
                else:
                    self._context_window_size = DEFAULT_CONTEXT_WINDOW

    def set_model(self, model_name: str, display_name: str) -> None:
        self._model_name = model_name
        self._model_display = display_name

    def set_context_window_size(self, size: int) -> None:
        self._window_from_command = size
        self._context_window_size = size
        _save_persisted_window(size)

    def get_context_window_size(self) -> int:
        return self._context_window_size

    def update_usage(self, usage: dict[str, int]) -> None:
        """从单次 API 响应更新使用量（不更新累计值）"""
        self._estimated_prompt_tokens = usage.get("prompt_tokens", 0)
        self._schedule_render()

    def update_session_usage(self, token_usage: TokenUsage, estimated_prompt_tokens: int = 0) -> None:
        """更新会话累计使用量和当前 prompt 估算

        Args:
            token_usage: 会话累计 TokenUsage
            estimated_prompt_tokens: 当前即将发送的 prompt 估算大小
        """
        self._cumulative_usage = token_usage
        self._estimated_prompt_tokens = estimated_prompt_tokens

    def _schedule_render(self) -> None:
        now = time.time() * 1000
        if now - self._last_render_time >= self._debounce_ms:
            self._last_render_time = now
            self._render()

    def _render(self) -> None:
        """渲染状态栏（两行）"""
        total = self._context_window_size
        estimated = self._estimated_prompt_tokens
        pct = (estimated / total * 100) if total > 0 else 0

        # 颜色：绿 <50% / 黄 50-80% / 红 80-90%
        if pct < 50:
            ctx_style = "green"
        elif pct < 80:
            ctx_style = "yellow"
        else:
            ctx_style = "red"

        est_str = format_token_count(estimated)
        total_str = format_token_count(total)

        # 第一行：模型 + ctx 百分比（当前估算 / 窗口）
        line1_parts = []
        if self._model_display:
            line1_parts.append(f"[bold cyan]{self._model_display}[/bold cyan]")
        line1_parts.append(
            f"[{ctx_style}]ctx: {pct:.1f}% ({est_str} / {total_str})[/{ctx_style}]"
        )
        self.console.print(f"\n{' | '.join(line1_parts)}")

        # 第二行：累计消耗
        cumulative = self._cumulative_usage.total_tokens
        cum_str = format_token_count(cumulative)
        self.console.print(
            f"[dim]累计: {cum_str} tokens "
            f"({self._cumulative_usage.input_tokens:,} in / {self._cumulative_usage.output_tokens:,} out)[/dim]"
        )

    def render_final(self) -> None:
        self._render()

    def get_status_text(self) -> str:
        """获取状态文本（用于测试）"""
        total = self._context_window_size
        estimated = self._estimated_prompt_tokens
        pct = (estimated / total * 100) if total > 0 else 0
        est_str = format_token_count(estimated)
        total_str = format_token_count(total)
        cumulative = self._cumulative_usage.total_tokens
        cum_str = format_token_count(cumulative)
        return (
            f"{self._model_display} | ctx: {pct:.1f}% ({est_str} / {total_str}) | "
            f"累计: {cum_str} tokens ({self._cumulative_usage.input_tokens:,} in / {self._cumulative_usage.output_tokens:,} out)"
        )
