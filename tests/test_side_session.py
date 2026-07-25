from __future__ import annotations

from typing import Any

import pytest

from coomi.engine.side_session import _clone_session, run_side_conversation, SIDE_READONLY_HINT
from coomi.services.llm.provider import LLMProvider
from coomi.tools.base import BaseTool, ToolAccess, ToolResult
from coomi.tools.registry import ToolRegistry
from coomi.types import LLMResponse, Message, Session, ToolCall


class ReadOnlyProbeTool(BaseTool):
    """只读工具 —— side session 应能调用。"""

    name = "Read"
    description = "read probe"
    access = ToolAccess.READ_ONLY

    def get_parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(success=True, output="file contents")


class WriteProbeTool(BaseTool):
    """写工具 —— side session 只读模式应拦截。"""

    name = "Write"
    description = "write probe"
    access = ToolAccess.WRITE

    def get_parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    def run(self, arguments: dict[str, Any]) -> ToolResult:  # pragma: no cover - 不应被执行
        raise AssertionError("Write tool must never run inside a read-only side session")


class TextOnlyProvider(LLMProvider):
    """一次性回一段纯文本，不调工具。"""

    def __init__(self, reply: str = "side reply") -> None:
        self.reply = reply

    async def chat(self, messages, tools=None, **kwargs):
        return LLMResponse(content=self.reply)

    async def chat_stream(self, messages, **kwargs):
        yield self.reply

    async def chat_stream_with_tools(self, messages, tools=None, **kwargs):
        yield {"type": "content", "content": self.reply}

    def switch_model(self, model_name: str) -> str:
        return model_name

    def get_model_display_name(self) -> str:
        return "side-test"


class FakeCommPanel:
    """记录 side_session 回调的假交流窗。"""

    def __init__(self) -> None:
        self.busy_states: list[bool] = []
        self.begun: list[str] = []
        self.streamed: list[str] = []
        self.appended: list[tuple[str, str | None]] = []
        self.tool_status: list[str] = []
        self.thinking = 0

    def set_busy(self, busy: bool) -> None:
        self.busy_states.append(busy)

    def begin_reply(self, prompt_text: str) -> None:
        self.begun.append(prompt_text)

    def show_reply_streaming(self, reply: str) -> None:
        self.streamed.append(reply)

    def append_reply(self, reply: str, error: str | None = None) -> None:
        self.appended.append((reply, error))

    def set_thinking(self) -> None:
        self.thinking += 1

    def set_tool_status(self, tool_name: str) -> None:
        self.tool_status.append(tool_name)


def _make_source_session() -> Session:
    return Session(
        id="main-1",
        system_prompt="You are Coomi Agent.",
        messages=[
            Message(role="user", content="原始任务"),
            Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="tc1", name="PowerShell", arguments={"command": "sleep 10"})],
            ),
        ],
        active_skills=["skill-a"],
        selected_mcps=["mcp-x"],
        history_path="/should/not/be/copied",
    )


def test_clone_is_independent_snapshot() -> None:
    source = _make_source_session()
    clone = _clone_session(source)

    # 独立 id、不落盘
    assert clone.id != source.id
    assert clone.history_path is None
    # system_prompt 追加了只读提示
    assert SIDE_READONLY_HINT in clone.system_prompt
    assert clone.system_prompt.startswith(source.system_prompt)
    # skills / mcps 是拷贝而非同一引用
    assert clone.active_skills == source.active_skills
    assert clone.active_skills is not source.active_skills
    assert clone.selected_mcps == source.selected_mcps
    assert clone.selected_mcps is not source.selected_mcps
    # messages 是新列表（浅拷贝），改动 clone 不影响 source
    assert clone.messages is not source.messages
    clone.messages.append(Message(role="user", content="侧路新增"))
    assert len(source.messages) == 2


@pytest.mark.asyncio
async def test_side_conversation_does_not_touch_main_session() -> None:
    source = _make_source_session()
    before = list(source.messages)
    comm = FakeCommPanel()
    registry = ToolRegistry()
    registry.register(ReadOnlyProbeTool())

    await run_side_conversation(
        comm,
        source,
        TextOnlyProvider("这是并发只读回复"),
        registry,
        256_000,
        "工具跑着的时候顺便问一句",
    )

    # 主 session 完全不受影响
    assert source.messages == before
    assert len(source.messages) == 2
    # 回复走了交流窗回调
    assert comm.begun == ["工具跑着的时候顺便问一句"]
    assert comm.appended
    final_reply, error = comm.appended[-1]
    assert error is None
    assert "并发只读回复" in final_reply
    # busy 有开有关
    assert comm.busy_states[0] is True
    assert comm.busy_states[-1] is False


@pytest.mark.asyncio
async def test_side_conversation_is_read_only() -> None:
    """side session 强制只读：写工具被拦，run() 绝不执行。"""
    source = _make_source_session()
    before = list(source.messages)
    comm = FakeCommPanel()
    registry = ToolRegistry()
    registry.register(ReadOnlyProbeTool())
    registry.register(WriteProbeTool())

    class WriteAttemptProvider(TextOnlyProvider):
        def __init__(self) -> None:
            super().__init__()
            self.turn = 0

        async def chat_stream_with_tools(self, messages, tools=None, **kwargs):
            self.turn += 1
            if self.turn == 1:
                yield {
                    "type": "tool_call",
                    "data": ToolCall(id="w1", name="Write", arguments={"path": "x", "content": "y"}),
                }
                return
            yield {"type": "content", "content": "只读模式下无法写入，已说明思路"}

    # 不应抛出（WriteProbeTool.run 会 assert）；只读模式拦截在执行前
    await run_side_conversation(
        comm,
        source,
        WriteAttemptProvider(),
        registry,
        256_000,
        "帮我改下文件",
    )

    # 最终仍给出了回复，主 session 未受影响
    assert comm.appended
    assert source.messages == before
