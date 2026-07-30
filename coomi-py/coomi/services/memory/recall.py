"""记忆召回 - 使用小模型选择相关记忆"""
from __future__ import annotations

import asyncio
import json
import os
import re

from ..llm.factory import create_fast_provider
from ..llm.provider import LLMProvider
from .manager import MemoryManager
from .types import Memory, MemoryType


# 召回配置
RECALL_LIMIT = 5
RECALL_LLM_TIMEOUT_SECONDS = float(os.environ.get("COOMI_MEMORY_RECALL_TIMEOUT", "1.5"))

_LOW_SIGNAL_EXACT = {
    "hi",
    "hello",
    "hey",
    "ping",
    "thanks",
    "thankyou",
    "\u4f60\u597d",
    "\u60a8\u597d",
    "\u55e8",
    "\u54c8\u55bd",
    "\u5728\u5417",
    "\u4f60\u662f\u8c01",
    "\u8c22\u8c22",
}

_MEMORY_SIGNAL_HINTS = (
    "project",
    "repo",
    "preference",
    "remember",
    "history",
    "context",
    "code",
    "bug",
    "test",
    "\u9879\u76ee",
    "\u4ee3\u7801",
    "\u4fee",
    "\u6539",
    "\u9519",
    "\u62a5\u9519",
    "\u504f\u597d",
    "\u8bb0\u5fc6",
    "\u5386\u53f2",
    "\u4e0a\u4e0b\u6587",
    "\u5de5\u5177",
)


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

    async def recall(self, context: str, limit: int = RECALL_LIMIT) -> list[Memory]:
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

        if _is_low_signal_context(context):
            return []

        local_matches = _select_locally(context, all_memories, limit)
        if local_matches:
            return local_matches

        if len(all_memories) <= limit or not _has_enough_signal_for_llm(context):
            return []

        # 构建记忆清单
        memory_index = self._build_memory_index(all_memories)

        # 使用小模型选择
        try:
            selected_indices = await asyncio.wait_for(
                self._select_with_llm(context, memory_index, limit),
                timeout=RECALL_LLM_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            selected_indices = []

        # 返回选中的记忆
        return [all_memories[i] for i in selected_indices if i < len(all_memories)]

    def _build_memory_index(self, memories: list[Memory]) -> str:
        """构建记忆清单文本"""
        lines = []
        for i, memory in enumerate(memories):
            stale_marker = " [stale]" if memory.is_stale else ""
            lines.append(f"{i}. [{memory.memory_type.value}] {memory.name}: {memory.description}{stale_marker}")
        return "\n".join(lines)

    async def _select_with_llm(self, context: str, memory_index: str, limit: int) -> list[int]:
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

            response = await llm.chat(
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

        return []

    async def recall_with_filter(
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

        if _is_low_signal_context(context):
            return []

        local_matches = _select_locally(context, all_memories, limit)
        if local_matches:
            return local_matches

        if len(all_memories) <= limit or not _has_enough_signal_for_llm(context):
            return []

        # 构建记忆清单
        memory_index = self._build_memory_index(all_memories)

        # 使用小模型选择
        try:
            selected_indices = await asyncio.wait_for(
                self._select_with_llm(context, memory_index, limit),
                timeout=RECALL_LLM_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            selected_indices = []

        # 返回选中的记忆
        return [all_memories[i] for i in selected_indices if i < len(all_memories)]


def _normalize_context(context: str) -> str:
    return " ".join((context or "").casefold().strip().split())


def _compact_context(context: str) -> str:
    return re.sub(r"\s+", "", _normalize_context(context))


def _is_low_signal_context(context: str) -> bool:
    compact = _compact_context(context)
    if not compact:
        return True
    stripped = compact.strip(".,!?;:~`'\"()[]{}<>/\\|-_=+*#@$%^&")
    if not stripped:
        return True
    if stripped in _LOW_SIGNAL_EXACT:
        return True
    if len(stripped) <= 4 and not any(hint in stripped for hint in _MEMORY_SIGNAL_HINTS):
        return True
    return False


def _query_terms(context: str) -> set[str]:
    text = _normalize_context(context)
    terms: set[str] = set()
    for match in re.finditer(r"[a-z0-9_./:-]{2,}", text):
        terms.add(match.group(0))
    for match in re.finditer(r"[\u4e00-\u9fff]{2,}", text):
        seq = match.group(0)
        terms.add(seq)
        terms.update(seq[i : i + 2] for i in range(len(seq) - 1))
    return {term for term in terms if len(term) >= 2}


def _has_enough_signal_for_llm(context: str) -> bool:
    if _is_low_signal_context(context):
        return False
    compact = _compact_context(context)
    return len(compact) >= 12 or len(_query_terms(context)) >= 2


def _select_locally(context: str, memories: list[Memory], limit: int) -> list[Memory]:
    terms = _query_terms(context)
    if not terms:
        return []

    scored: list[tuple[int, int, Memory]] = []
    for index, memory in enumerate(memories):
        score = _score_memory(memory, terms)
        if score > 0:
            scored.append((score, -index, memory))

    scored.sort(reverse=True, key=lambda item: (item[0], item[1]))
    return [memory for _, _, memory in scored[:limit]]


def _score_memory(memory: Memory, terms: set[str]) -> int:
    fields = (
        (memory.name.casefold(), 5),
        (memory.description.casefold(), 3),
        (memory.content.casefold(), 1),
    )
    score = 0
    for text, weight in fields:
        if not text:
            continue
        for term in terms:
            if term in text:
                score += weight + min(len(term), 8)
    if memory.is_stale and score:
        score = max(1, score - 1)
    return score
