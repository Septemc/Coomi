"""Bash 安全检查器 — 语法级别的命令安全分析"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


@dataclass
class SafetyCheckResult:
    """安全检查结果"""
    safe: bool
    risk_level: Literal["low", "medium", "high", "critical"]
    warnings: list[str]
    details: str = ""


class BashSafetyChecker:
    """Bash 命令安全检查器"""

    # 危险命令模式
    DANGEROUS_PATTERNS = [
        # 破坏性操作
        (r'\brm\s+(-[rf]+\s+)?/', "删除根目录文件"),
        (r'\brm\s+.*\*', "通配符删除"),
        (r'\bmkfs\b', "格式化文件系统"),
        (r'\bdd\s+.*of=', "dd 写入操作"),

        # 权限提升
        (r'\bsudo\b', "sudo 权限提升"),
        (r'\bchmod\s+777', "危险权限设置"),
        (r'\bchown\b', "修改文件所有者"),

        # 网络操作
        (r'\bcurl\b.*\|\s*(ba)?sh', "远程脚本执行"),
        (r'\bwget\b.*\|\s*(ba)?sh', "远程脚本执行"),
        (r'\bnc\s+-l', "netcat 监听"),

        # 命令注入模式
        (r'[;&|`$]', "命令链/注入字符"),
        (r'\$\(', "命令替换"),
        (r'`[^`]+`', "反引号命令替换"),
    ]

    # 路径逃逸模式
    PATH_ESCAPE_PATTERNS = [
        (r'\.\./\.\./', "多层路径逃逸"),
        (r'/etc/(passwd|shadow|hosts)', "系统敏感文件"),
        (r'~/.ssh/', "SSH 密钥目录"),
        (r'\$HOME', "HOME 变量引用"),
    ]

    def check_command(self, command: str) -> SafetyCheckResult:
        """检查命令安全性"""
        warnings = []
        risk_level: Literal["low", "medium", "high", "critical"] = "low"

        # 检查危险命令模式
        for pattern, desc in self.DANGEROUS_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                warnings.append(f"检测到 {desc}")
                risk_level = self._escalate_risk(risk_level, "medium")

        # 检查路径逃逸
        for pattern, desc in self.PATH_ESCAPE_PATTERNS:
            if re.search(pattern, command):
                warnings.append(f"检测到 {desc}")
                risk_level = self._escalate_risk(risk_level, "high")

        # 判断是否安全
        safe = risk_level in ("low", "medium")

        return SafetyCheckResult(
            safe=safe,
            risk_level=risk_level,
            warnings=warnings,
            details=f"命令: {command[:100]}..."
        )

    def _escalate_risk(self, current: str, new: str) -> str:
        """提升风险等级"""
        levels = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        return new if levels[new] > levels[current] else current
