"""记忆提取器 - 对话后自动提取记忆

模仿 Claude Code 的 extractMemories 行为：
1. 分析最近对话
2. 判断是否有值得保存的信息
3. 自动保存到记忆目录
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from ..llm.factory import create_fast_provider
from ..llm.provider import LLMProvider
from ...types import Message
from .manager import MemoryManager
from .types import Memory, MemoryType

# 提取配置
MAX_ANALYZE_MESSAGES = 10  # 分析最近 N 条消息

EXTRACT_PROMPT = """你是 Coomi Agent 的记忆提取流程。分析以下对话，判断是否有值得长期记忆的信息。

值得记忆的信息包括：
- 用户的角色、偏好、工作习惯（type: user）
- 用户对你的行为反馈或纠正（type: feedback）
- 项目的目标、进展、重要决策（type: project）
- 外部系统、资源的指针（type: reference）

不值得记忆的信息：
- 一次性的代码修改细节
- 临时的调试信息
- 已经在代码中可以找到的信息

请返回 JSON，如果有值得记忆的信息：
{"save": true, "type": "user|feedback|project|reference", "name": "简短kebab-case名称", "description": "一句话描述", "content": "记忆内容"}

如果没有值得记忆的信息：
{"save": false}

只返回 JSON，不要其他文字。"""


class MemoryExtractor:
    """记忆提取器

    在对话结束后分析最近的对话，提取值得长期保存的信息。
    """

    def __init__(self, llm: LLMProvider, memory_manager: MemoryManager):
        """
        Args:
            llm: LLM Provider 实例
            memory_manager: 记忆管理器
        """
        self.llm = llm
        self.memory_manager = memory_manager

    async def extract(self, messages: list[Message]) -> Memory | None:
        """从对话中提取记忆

        Args:
            messages: 最近的对话消息

        Returns:
            Memory | None: 提取的记忆，如果没有值得保存的则返回 None
        """
        if not messages:
            return None

        # 只分析最近 N 条消息
        recent = messages[-MAX_ANALYZE_MESSAGES:]
        conversation = self._format_for_analysis(recent)

        # 尝试使用 fast_model（如果配置了），否则用当前模型
        fast_provider = create_fast_provider(self.llm)
        llm = fast_provider if fast_provider else self.llm

        try:
            response = await llm.chat(
                messages=[{"role": "user", "content": f"{EXTRACT_PROMPT}\n\n---\n\n{conversation}"}],
                tools=None,
            )
        except Exception:
            return None

        # 解析 JSON
        content = response.content or ""
        result = self._parse_json(content)

        if not result or not result.get("save"):
            return None

        # 创建记忆
        try:
            memory_type = MemoryType(result.get("type", "user"))
        except ValueError:
            memory_type = MemoryType.USER

        memory = Memory(
            name=result.get("name", f"auto-{datetime.now().strftime('%H%M%S')}"),
            description=result.get("description", ""),
            memory_type=memory_type,
            content=result.get("content", ""),
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )

        # 保存
        self.memory_manager.save_memory(memory)
        return memory

    def _format_for_analysis(self, messages: list[Message]) -> str:
        """格式化消息用于分析"""
        lines = []
        for msg in messages:
            if msg.role == "system":
                continue
            if msg.role == "tool":
                content = (msg.content or "")[:200]
                lines.append(f"[tool] {content}")
            elif msg.role == "assistant" and msg.tool_calls:
                tool_names = [tc.name for tc in msg.tool_calls]
                lines.append(f"[assistant] 调用工具: {', '.join(tool_names)}")
                if msg.content:
                    lines.append(f"  {msg.content[:200]}")
            else:
                content = (msg.content or "")[:500]
                lines.append(f"[{msg.role}] {content}")
        return "\n".join(lines)

    def _parse_json(self, text: str) -> dict[str, Any] | None:
        """从 LLM 输出中解析 JSON"""
        # 尝试直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 尝试提取 JSON 块
        match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass

        return None
