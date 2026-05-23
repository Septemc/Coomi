"""记忆召回 - 使用小模型选择相关记忆"""
from __future__ import annotations

import json
from typing import Any

from ..llm.factory import create_fast_provider
from ..llm.provider import LLMProvider
from .manager import MemoryManager
from .types import Memory, MemoryType


# 召回配置
RECALL_LIMIT = 5


class MemoryRecall:
    """记忆召回器

    使用 deepseek-v4-flash 作为选择器，从记忆库中筛选相关记忆。
    """

    def __init__(self, llm: LLMProvider, memory_manager: MemoryManager):
        """
        Args:
            llm: LLM Provider 实例（用于召回）
            memory_manager: 记忆管理器
        """
        self.llm = llm
        self.memory_manager = memory_manager

    def recall(self, context: str, limit: int = RECALL_LIMIT) -> list[Memory]:
        """召回相关记忆

        Args:
            context: 当前上下文（用户输入或对话历史）
            limit: 返回数量

        Returns:
            list[Memory]: 相关记忆列表
        """
        # 获取所有记忆
        all_memories = self.memory_manager.list_memories()

        if not all_memories:
            return []

        if len(all_memories) <= limit:
            return all_memories

        # 构建记忆清单
        memory_index = self._build_memory_index(all_memories)

        # 使用小模型选择
        selected_indices = self._select_with_llm(context, memory_index, limit)

        # 返回选中的记忆
        return [all_memories[i] for i in selected_indices if i < len(all_memories)]

    def _build_memory_index(self, memories: list[Memory]) -> str:
        """构建记忆清单文本"""
        lines = []
        for i, memory in enumerate(memories):
            stale_marker = " [stale]" if memory.is_stale else ""
            lines.append(f"{i}. [{memory.memory_type.value}] {memory.name}: {memory.description}{stale_marker}")
        return "\n".join(lines)

    def _select_with_llm(self, context: str, memory_index: str, limit: int) -> list[int]:
        """使用 LLM 选择相关记忆

        Args:
            context: 当前上下文
            memory_index: 记忆清单
            limit: 返回数量

        Returns:
            list[int]: 选中的记忆索引
        """
        prompt = f"""你是一个记忆选择器。根据当前上下文，从记忆清单中选择最相关的 {limit} 条记忆。

当前上下文:
{context}

记忆清单:
{memory_index}

请返回一个 JSON 数组，包含选中的记忆索引（从 0 开始）。
只返回 JSON 数组，不要其他文字。

示例: [0, 3, 5, 7, 9]"""

        try:
            # 尝试使用 fast_model，否则用当前模型
            fast_provider = create_fast_provider(self.llm)
            llm = fast_provider if fast_provider else self.llm

            response = llm.chat(
                messages=[{"role": "user", "content": prompt}],
                tools=None,
            )

            # 解析 JSON
            content = response.content or "[]"
            import re
            match = re.search(r'\[[\d,\s]*\]', content)
            if match:
                indices = json.loads(match.group())
                return [i for i in indices if isinstance(i, int)][:limit]

        except Exception:
            pass

        return list(range(min(limit, len(memory_index.split('\n')))))

    def recall_with_filter(
        self,
        context: str,
        memory_type: MemoryType | None = None,
        exclude_stale: bool = True,
        limit: int = RECALL_LIMIT,
    ) -> list[Memory]:
        """带过滤的召回

        Args:
            context: 当前上下文
            memory_type: 记忆类型过滤
            exclude_stale: 是否排除陈旧记忆
            limit: 返回数量

        Returns:
            list[Memory]: 相关记忆列表
        """
        all_memories = self.memory_manager.list_memories(memory_type=memory_type)

        if exclude_stale:
            all_memories = [m for m in all_memories if not m.is_stale]

        if not all_memories:
            return []

        if len(all_memories) <= limit:
            return all_memories

        # 构建记忆清单
        memory_index = self._build_memory_index(all_memories)

        # 使用小模型选择
        selected_indices = self._select_with_llm(context, memory_index, limit)

        # 返回选中的记忆
        return [all_memories[i] for i in selected_indices if i < len(all_memories)]
