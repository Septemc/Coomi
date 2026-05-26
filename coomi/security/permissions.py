"""权限系统 — 控制工具执行权限"""
from __future__ import annotations

from enum import Enum
from typing import Any


class PermissionLevel(Enum):
    """权限级别"""
    AUTO = "auto"           # 自动执行，无需确认
    ASK = "ask"             # 需要用户确认
    DENY = "deny"           # 禁止执行


class PermissionSystem:
    """权限管理系统"""

    # 默认权限规则
    DEFAULT_RULES = {
        # 读操作 - 自动执行
        "Read": PermissionLevel.AUTO,
        "Glob": PermissionLevel.AUTO,
        "Grep": PermissionLevel.AUTO,
        "WebFetch": PermissionLevel.AUTO,
        "WebSearch": PermissionLevel.AUTO,

        # 写操作 - 需要确认
        "Write": PermissionLevel.ASK,
        "Edit": PermissionLevel.ASK,
        "Bash": PermissionLevel.ASK,
        "PowerShell": PermissionLevel.ASK,

        # 危险操作 - 需要确认
        "Agent": PermissionLevel.ASK,
    }

    def __init__(self):
        self._rules: dict[str, PermissionLevel] = self.DEFAULT_RULES.copy()
        self._session_approvals: set[str] = set()  # 本次会话已批准的操作

    def check_permission(self, tool_name: str, arguments: dict[str, Any]) -> PermissionLevel:
        """检查工具执行权限"""
        # 检查会话级批准
        if tool_name in self._session_approvals:
            return PermissionLevel.AUTO

        # 检查规则
        return self._rules.get(tool_name, PermissionLevel.ASK)

    def approve_tool(self, tool_name: str) -> None:
        """批准工具执行（会话级）"""
        self._session_approvals.add(tool_name)

    def set_rule(self, tool_name: str, level: PermissionLevel) -> None:
        """设置权限规则"""
        self._rules[tool_name] = level
