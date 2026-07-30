"""状态栏 - 显示模型、上下文窗口、Token 使用情况

状态栏是纯数据持有层，不执行任何终端渲染。
所有渲染由 StatusPanel widget 通过 Textual 统一完成。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

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
    """纯数据状态栏 — 持有模型名称、上下文窗口大小、Token 使用情况。

    不执行任何终端渲染。渲染由 StatusPanel widget 通过 Textual 统一完成。

    上下文窗口加载优先级：/context 命令 > state.json > .env > 默认 256K
    """

    def __init__(self):
        self._model_name: str = ""
        self._model_display: str = ""
        self._permission_label: str = "Ask for approval"
        self._cumulative_usage: TokenUsage = TokenUsage()  # 会话累计
        self.estimated_prompt_tokens: int = 0  # 当前 prompt 估算（公开属性，供 StatusPanel 读取）
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

    @property
    def model_display(self) -> str:
        return self._model_display

    def set_permission_label(self, label: str) -> None:
        self._permission_label = label

    @property
    def permission_label(self) -> str:
        return self._permission_label

    @property
    def cumulative_usage(self) -> TokenUsage:
        return self._cumulative_usage

    def set_context_window_size(self, size: int) -> None:
        self._window_from_command = size
        self._context_window_size = size
        _save_persisted_window(size)

    def get_context_window_size(self) -> int:
        return self._context_window_size

    def update_usage(self, usage: dict[str, int]) -> None:
        """从单次 API 响应更新当前 prompt 估算（纯内存更新，不渲染）"""
        self.estimated_prompt_tokens = usage.get("prompt_tokens", 0)

    def update_session_usage(self, token_usage: TokenUsage, estimated_prompt_tokens: int = 0) -> None:
        """更新会话累计使用量和当前 prompt 估算

        Args:
            token_usage: 会话累计 TokenUsage
            estimated_prompt_tokens: 当前即将发送的 prompt 估算大小
        """
        self._cumulative_usage = token_usage
        self.estimated_prompt_tokens = estimated_prompt_tokens
