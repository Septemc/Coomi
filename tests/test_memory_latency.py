from __future__ import annotations

import pytest

from coomi.engine.session import build_system_prompt
from coomi.services.memory.recall import MemoryRecall
from coomi.services.memory.types import Memory, MemoryType
from coomi.types import LLMResponse


def _memory(index: int, description: str = "general note", content: str = "") -> Memory:
    return Memory(
        name=f"memory-{index}",
        description=description,
        memory_type=MemoryType.PROJECT,
        content=content or description,
    )


class FakeMemoryManager:
    def __init__(self, memories: list[Memory]):
        self._memories = memories

    def list_memories(self, memory_type=None):
        memories = self._memories
        if memory_type is not None:
            memories = [memory for memory in memories if memory.memory_type == memory_type]
        return memories

    def get_selected_memory_content(self, memories: list[Memory]) -> str:
        return "\n".join(memory.content for memory in memories)

    def get_index_content(self) -> str:
        return "memory index should stay out of active turns"


class FailingRecallLLM:
    def __init__(self):
        self.calls = 0

    async def chat(self, messages, tools=None, **kwargs):
        self.calls += 1
        raise AssertionError("memory recall should not call the LLM for low-signal input")


class FakeRecallLLM:
    def __init__(self, content: str = "[]"):
        self.calls = 0
        self.content = content

    async def chat(self, messages, tools=None, **kwargs):
        self.calls += 1
        return LLMResponse(content=self.content)


@pytest.mark.asyncio
async def test_memory_recall_skips_llm_for_greeting():
    manager = FakeMemoryManager([_memory(i) for i in range(10)])
    llm = FailingRecallLLM()

    selected = await MemoryRecall(llm, manager).recall("你好")

    assert selected == []
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_memory_recall_uses_local_relevance_before_llm():
    manager = FakeMemoryManager(
        [
            _memory(0, "frontend color preference", "Use quiet UI colors."),
            _memory(1, "web search debugging", "WebSearch uses Sogou first for CJK."),
            _memory(2, "testing preference", "Run pytest after engine changes."),
        ]
    )
    llm = FakeRecallLLM("[0]")

    selected = await MemoryRecall(llm, manager).recall("继续修复 WebSearch 搜索问题")

    assert [memory.name for memory in selected] == ["memory-1"]
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_active_turn_without_memory_match_does_not_inject_memory_index():
    manager = FakeMemoryManager([_memory(i) for i in range(2)])
    llm = FakeRecallLLM()
    recall = MemoryRecall(llm, manager)

    prompt = await build_system_prompt(
        memory_manager=manager,
        memory_recall=recall,
        current_context="你好",
        cwd=".",
        model_display="fake",
    )

    assert "## Memory Index" not in prompt
    assert "memory index should stay out of active turns" not in prompt
    assert llm.calls == 0
