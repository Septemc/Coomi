"""权限系统 — 控制工具执行权限"""
from __future__ import annotations

from enum import Enum
from typing import Any

from .bash_safety import BashSafetyChecker


class PermissionLevel(Enum):
    """权限级别"""
    AUTO = "auto"           # 自动执行，无需确认
    ASK = "ask"             # 需要用户确认
    DENY = "deny"           # 禁止执行


class PermissionMode(Enum):
    """工具权限模式"""
    ASK_APPROVAL = "ask_approval"
    APPROVE_FOR_ME = "approve_for_me"
    FULL_ACCESS = "full_access"


PERMISSION_MODE_LABELS = {
    PermissionMode.ASK_APPROVAL: "Ask for approval",
    PermissionMode.APPROVE_FOR_ME: "Approve for me",
    PermissionMode.FULL_ACCESS: "Full access",
}


PERMISSION_MODE_DESCRIPTIONS = {
    PermissionMode.ASK_APPROVAL: "Always ask before tools read, write, run commands, or use the internet",
    PermissionMode.APPROVE_FOR_ME: "Only ask for actions detected as potentially unsafe",
    PermissionMode.FULL_ACCESS: "Unrestricted access to tools",
}


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
        "AskUserQuestion": PermissionLevel.AUTO,

        # 写操作 / shell - 默认需要确认
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
        self._mode: PermissionMode = PermissionMode.ASK_APPROVAL
        self._bash_safety = BashSafetyChecker()

    @property
    def mode(self) -> PermissionMode:
        return self._mode

    def set_mode(self, mode: PermissionMode) -> None:
        self._mode = mode

    def cycle_mode(self) -> PermissionMode:
        modes = [
            PermissionMode.ASK_APPROVAL,
            PermissionMode.APPROVE_FOR_ME,
            PermissionMode.FULL_ACCESS,
        ]
        idx = modes.index(self._mode)
        self._mode = modes[(idx + 1) % len(modes)]
        return self._mode

    def get_mode_label(self) -> str:
        return PERMISSION_MODE_LABELS[self._mode]

    def get_mode_description(self) -> str:
        return PERMISSION_MODE_DESCRIPTIONS[self._mode]

    @property
    def is_full_access(self) -> bool:
        """Full access is an unconditional runtime approval policy."""
        return self._mode == PermissionMode.FULL_ACCESS

    def check_execution_permission(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        source: str = "native",
        mutates_state: bool = False,
    ) -> PermissionLevel:
        """Return the final permission after mode and call-source policy are applied."""
        if self.is_full_access:
            return PermissionLevel.AUTO
        level = self.check_permission(tool_name, arguments)
        if source == "text_fallback" and mutates_state and level == PermissionLevel.AUTO:
            return PermissionLevel.ASK
        return level

    def check_permission(self, tool_name: str, arguments: dict[str, Any]) -> PermissionLevel:
        """检查工具执行权限"""
        if self._mode == PermissionMode.FULL_ACCESS:
            return PermissionLevel.AUTO

        # 检查会话级批准
        if tool_name in self._session_approvals:
            return PermissionLevel.AUTO

        if self._mode == PermissionMode.ASK_APPROVAL:
            if tool_name == "AskUserQuestion":
                return PermissionLevel.AUTO
            return PermissionLevel.ASK

        if self._mode == PermissionMode.APPROVE_FOR_ME:
            return self._check_approve_for_me(tool_name, arguments)

        # 检查规则
        return self._rules.get(tool_name, PermissionLevel.ASK)

    def _check_approve_for_me(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> PermissionLevel:
        if tool_name in {
            "Read",
            "Glob",
            "Grep",
            "WebFetch",
            "WebSearch",
            "Write",
            "Edit",
            "Config",
            "TodoWrite",
            "EnterPlanMode",
            "ExitPlanMode",
            "AskUserQuestion",
        }:
            return PermissionLevel.AUTO

        if tool_name in {"Bash", "PowerShell"}:
            command = str(arguments.get("command", ""))
            result = self._bash_safety.check_command(command)
            return PermissionLevel.AUTO if result.risk_level == "low" else PermissionLevel.ASK

        if tool_name == "Agent":
            return PermissionLevel.ASK

        return self._rules.get(tool_name, PermissionLevel.ASK)

    def approve_tool(self, tool_name: str) -> None:
        """批准工具执行（会话级）"""
        self._session_approvals.add(tool_name)

    def set_rule(self, tool_name: str, level: PermissionLevel) -> None:
        """设置权限规则"""
        self._rules[tool_name] = level
