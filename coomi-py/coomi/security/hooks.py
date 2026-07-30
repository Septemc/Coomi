"""Hook 系统 — 工具执行前后的拦截器"""
from __future__ import annotations

from typing import Any, Callable
from dataclasses import dataclass


@dataclass
class HookContext:
    """Hook 上下文"""
    tool_name: str
    arguments: dict[str, Any]
    result: Any = None
    error: str | None = None
    skip: bool = False  # 设置为 True 跳过工具执行


class HookSystem:
    """Hook 管理系统"""

    def __init__(self):
        self._pre_hooks: dict[str, list[Callable]] = {}   # 工具执行前
        self._post_hooks: dict[str, list[Callable]] = {}  # 工具执行后

    def register_pre_hook(self, tool_name: str, hook: Callable[[HookContext], None]) -> None:
        """注册工具执行前的 Hook"""
        if tool_name not in self._pre_hooks:
            self._pre_hooks[tool_name] = []
        self._pre_hooks[tool_name].append(hook)

    def register_post_hook(self, tool_name: str, hook: Callable[[HookContext], None]) -> None:
        """注册工具执行后的 Hook"""
        if tool_name not in self._post_hooks:
            self._post_hooks[tool_name] = []
        self._post_hooks[tool_name].append(hook)

    async def run_pre_hooks(self, tool_name: str, arguments: dict[str, Any]) -> HookContext:
        """执行工具前的 Hooks"""
        ctx = HookContext(tool_name=tool_name, arguments=arguments)
        for hook in self._pre_hooks.get(tool_name, []):
            hook(ctx)
            if ctx.skip:
                break
        return ctx

    async def run_post_hooks(self, tool_name: str, arguments: dict[str, Any], result: Any) -> HookContext:
        """执行工具后的 Hooks"""
        ctx = HookContext(tool_name=tool_name, arguments=arguments, result=result)
        for hook in self._post_hooks.get(tool_name, []):
            hook(ctx)
        return ctx
