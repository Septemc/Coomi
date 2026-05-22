"""增量 Markdown 流式渲染器 - 使用 Rich Live 实现瀑布流效果"""
from __future__ import annotations

import threading
import time

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text


class StreamRenderer:
    """使用 Rich Live 的增量 Markdown 渲染器

    实现类似 Claude Code 的瀑布流输出效果：
    - 流式接收内容块
    - 增量渲染 Markdown
    - 防抖动更新（避免过度重绘）
    """

    def __init__(self, console: Console, render_interval: float = 0.05):
        """
        Args:
            console: Rich Console 实例
            render_interval: 渲染间隔（秒），默认 50ms
        """
        self.console = console
        self._buffer: str = ""
        self._render_interval = render_interval
        self._last_render_time: float = 0
        self._live: Live | None = None
        self._started: bool = False
        self._lock = threading.Lock()

    def start(self) -> None:
        """启动 Live 显示"""
        self._buffer = ""
        self._live = Live(
            console=self.console,
            refresh_per_second=20,
            transient=False,  # 停止后保留输出
        )
        self._live.start()
        self._started = True

    def write(self, chunk: str) -> None:
        """写入内容块

        Args:
            chunk: 文本内容块
        """
        with self._lock:
            self._buffer += chunk
            now = time.time()
            if now - self._last_render_time >= self._render_interval:
                self._last_render_time = now
                self._update_display()

    def _update_display(self) -> None:
        """更新 Live 显示"""
        if self._live and self._started and self._buffer:
            try:
                self._live.update(Markdown(self._buffer))
            except Exception:
                # Markdown 解析失败时 fallback 到纯文本
                self._live.update(Text(self._buffer))

    def finish(self) -> str:
        """完成渲染，返回最终内容

        Returns:
            str: 完整的缓冲区内容
        """
        with self._lock:
            # 最终渲染
            if self._live and self._started:
                try:
                    self._live.update(Markdown(self._buffer))
                except Exception:
                    self._live.update(Text(self._buffer))
                self._live.stop()
            self._started = False
            return self._buffer

    @property
    def is_started(self) -> bool:
        """检查渲染器是否已启动"""
        return self._started

    @property
    def content(self) -> str:
        """获取当前缓冲区内容"""
        return self._buffer
