"""重试策略 — 智能退避 + 方案切换"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class RetryAction(str, Enum):
    """重试策略建议"""
    RETRY_SAME = "retry_same"         # 相同方法重试
    RETRY_DIFFERENT = "retry_different"  # 换方法重试
    SKIP = "skip"                     # 跳过此步
    FAILED = "failed"                 # 最终失败


@dataclass
class RetryContext:
    """重试上下文"""
    step_index: int
    attempt: int = 0
    last_error: str = ""
    alternative_methods: list[str] = field(default_factory=list)
    network_disconnects: int = 0
    first_failure_at: datetime = field(default_factory=datetime.now)


class RetryPolicy:
    """重试策略管理器
    
    策略：
    - 第1-3次: 相同方法重试（退避: 5s → 15s → 45s）
    - 第4-5次: 分析错误，LLM 生成替代方案重试
    - 第6次: 写入 ISSUE.md → 跳过此步，继续下一步
    
    网络断开特殊处理：指数退避等待重连
    """
    
    MAX_SAME_METHOD_RETRIES = 3       # 同一方案最多重试次数
    MAX_TOTAL_RETRIES = 6             # 总重试次数上限
    BASE_DELAY = 5                    # 基础退避秒数
    NETWORK_BASE_DELAY = 5            # 网络重连基础退避秒数
    NETWORK_MAX_DELAY = 300           # 网络重连最大延迟（5分钟）
    
    def __init__(self):
        self._contexts: dict[int, RetryContext] = {}
        self._network_failures: int = 0
    
    def get_context(self, step_index: int) -> RetryContext:
        """获取或创建步骤的重试上下文"""
        if step_index not in self._contexts:
            self._contexts[step_index] = RetryContext(step_index=step_index)
        return self._contexts[step_index]
    
    def reset_step(self, step_index: int) -> None:
        """重置某步的重试计数"""
        self._contexts.pop(step_index, None)
    
    def decide_action(self, step_index: int, error: str) -> tuple[RetryAction, float]:
        """根据当前重试次数决定下一步动作
        
        Returns:
            (RetryAction, delay_seconds)
        """
        ctx = self.get_context(step_index)
        ctx.attempt += 1
        ctx.last_error = error
        
        if self._is_network_error(error):
            self._network_failures += 1
            ctx.network_disconnects += 1
            delay = min(self.NETWORK_BASE_DELAY * (2 ** ctx.network_disconnects), self.NETWORK_MAX_DELAY)
            return RetryAction.RETRY_SAME, delay
        
        if ctx.attempt <= self.MAX_SAME_METHOD_RETRIES:
            delay = self.BASE_DELAY * (3 ** (ctx.attempt - 1))
            return RetryAction.RETRY_SAME, delay
        
        if ctx.attempt <= self.MAX_TOTAL_RETRIES:
            delay = self.BASE_DELAY * (3 ** (ctx.attempt - 1))
            return RetryAction.RETRY_DIFFERENT, delay
        
        return RetryAction.SKIP, 0.0
    
    async def wait(self, delay: float) -> None:
        """等待指定秒数（可被中断）"""
        if delay > 0:
            await asyncio.sleep(delay)
    
    def _is_network_error(self, error: str) -> bool:
        """判断是否为网络相关错误"""
        error_lower = error.lower()
        network_keywords = [
            "connection", "connect", "timeout", "network",
            "dns", "resolve", "refused", "reset", "unreachable",
            "eof", "broken pipe", "ssl", "certificate",
            "httpx", "requests",
        ]
        return any(kw in error_lower for kw in network_keywords)
    
    def is_network_issue(self, error: str) -> bool:
        """检查是否为网络问题（供外部使用）"""
        return self._is_network_error(error)
    
    def reset_network_failures(self) -> None:
        """网络恢复后重置网络失败计数"""
        self._network_failures = 0
