from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from coomi.engine.loop import AgentLoop
from coomi.engine.loop_runner import _step_completion_confirmed
from coomi.engine.tool_executor import ToolExecutor
from coomi.services.context.compressor import ContextCompressor
from coomi.services.context.message_guard import SYNTHETIC_TOOL_RESULT
from coomi.services.llm.provider import LLMProvider
from coomi.tools.base import BaseTool, ToolAccess, ToolConcurrency, ToolResult
from coomi.tools.registry import ToolRegistry
from coomi.types import LLMResponse, Message, Session, ToolCall


class FakeSummaryLLM:
    async def chat(self, messages: list[dict[str, Any]], tools=None, **kwargs):
        return LLMResponse(content="summary")


class CountingTool(BaseTool):
    name = "Read"
    description = "counting read"
    access = ToolAccess.READ_ONLY
    concurrency = ToolConcurrency.PARALLEL
    requires_confirmation = False

    def __init__(self, output: str = "ok"):
        self.output = output
        self.calls = 0

    def get_parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
            },
            "required": ["file_path"],
        }

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        self.calls += 1
        return ToolResult(success=True, output=self.output)


class WriteCountingTool(CountingTool):
    name = "Write"
    access = ToolAccess.WRITE
    concurrency = ToolConcurrency.BLOCKING


class ParseErrorProvider(LLMProvider):
    def __init__(self):
        self.calls = 0

    async def chat(self, messages, tools=None, **kwargs):
        return LLMResponse(content="ok")

    async def chat_stream(self, messages, **kwargs):
        yield "ok"

    async def chat_stream_with_tools(self, messages, tools=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield {
                "type": "tool_call",
                "data": {
                    "id": "bad_json",
                    "name": "Read",
                    "arguments": {},
                    "raw_arguments": "{",
                    "parse_error": "Expecting property name",
                },
            }
        else:
            yield {"type": "content", "content": "done"}

    def switch_model(self, model_name: str) -> str:
        return model_name

    def get_model_display_name(self) -> str:
        return "fake"


def test_message_guard_repairs_tool_pairing_and_strips_reasoning():
    session = Session(id="s", system_prompt="sys")
    session.messages = [
        Message(role="tool", content="orphan", tool_call_id="orphan"),
        Message(
            role="assistant",
            content=None,
            reasoning_content="private",
            tool_calls=[ToolCall(id="call_1", name="Read", arguments={"file_path": "a"})],
        ),
        Message(
            role="assistant",
            content=None,
            tool_calls=[ToolCall(id="call_1", name="Read", arguments={"file_path": "b"})],
        ),
        Message(role="tool", content="duplicate result", tool_call_id="call_1"),
    ]

    payload = session.get_messages_for_api()

    assert payload[0] == {"role": "system", "content": "sys"}
    assert all("reasoning_content" not in msg for msg in payload)
    assert not any(msg.get("content") == "orphan" for msg in payload)
    tool_results = [msg for msg in payload if msg["role"] == "tool"]
    assert len(tool_results) == 1
    assert tool_results[0]["tool_call_id"] == "call_1"
    assert tool_results[0]["content"] == SYNTHETIC_TOOL_RESULT


def test_compressor_trim_keeps_tool_call_group_together():
    compressor = ContextCompressor()
    messages = [Message(role="user", content="first")]
    for i in range(12):
        messages.append(Message(role="user", content=f"user {i}"))
    messages.extend(
        [
            Message(
                role="assistant",
                tool_calls=[ToolCall(id="call_keep", name="Read", arguments={"file_path": "x"})],
            ),
            Message(role="tool", content="result", tool_call_id="call_keep"),
        ]
    )

    trimmed = compressor._trim_old_messages(messages)
    ids = [msg.tool_call_id for msg in trimmed if msg.role == "tool"]
    assert "call_keep" in ids
    assistant_ids = [
        tool_call.id
        for msg in trimmed
        if msg.role == "assistant" and msg.tool_calls
        for tool_call in msg.tool_calls
    ]
    assert "call_keep" in assistant_ids


@pytest.mark.asyncio
async def test_llm_summarize_does_not_restore_bare_tool_messages():
    compressor = ContextCompressor(FakeSummaryLLM())
    session = Session(id="s", system_prompt="sys")
    session.messages = [
        Message(role="user", content="first"),
        Message(
            role="assistant",
            tool_calls=[ToolCall(id="call_1", name="Read", arguments={"file_path": "x"})],
        ),
        Message(role="tool", content="tool result", tool_call_id="call_1"),
        Message(role="user", content="recent"),
    ]

    compressed = await compressor.compress(session, context_window_size=10, force=True)

    assert compressed[0].role == "user"
    assert "summary" in (compressed[0].content or "")
    assert all(msg.role != "tool" for msg in compressed)


@pytest.mark.asyncio
async def test_tool_executor_denies_ask_permission_without_ui():
    registry = ToolRegistry()
    tool = WriteCountingTool()
    registry.register(tool)
    executor = ToolExecutor(registry)
    session = Session(id="s")

    outcome = await executor.execute(
        session,
        ToolCall(id="call_1", name="Write", arguments={"file_path": "x"}),
    )

    assert outcome.is_error
    assert "Permission required" in outcome.result_text
    assert tool.calls == 0


@pytest.mark.asyncio
async def test_tool_executor_persists_large_results_per_session(tmp_path: Path):
    registry = ToolRegistry()
    large_output = "x" * (60 * 1024)
    tool = CountingTool(output=large_output)
    registry.register(tool)
    executor = ToolExecutor(registry, project_path=str(tmp_path))

    first = await executor.execute(
        Session(id="s1"),
        ToolCall(id="call_1", name="Read", arguments={"file_path": "x"}),
    )
    second = await executor.execute(
        Session(id="s2"),
        ToolCall(id="call_2", name="Read", arguments={"file_path": "x"}),
    )

    assert tool.calls == 2
    assert "Full output saved to" in first.result_text
    assert "Full output saved to" in second.result_text
    assert (tmp_path / ".coomi" / "sessions" / "s1" / "tool_results" / "call_1.txt").exists()
    assert (tmp_path / ".coomi" / "sessions" / "s2" / "tool_results" / "call_2.txt").exists()


@pytest.mark.asyncio
async def test_agent_loop_turns_invalid_tool_json_into_tool_result(tmp_path: Path):
    registry = ToolRegistry()
    tool = CountingTool()
    registry.register(tool)
    session = Session(id="s", system_prompt="sys")
    agent = AgentLoop(ParseErrorProvider(), registry, project_path=str(tmp_path))

    events = [event async for event in agent.run_stream(session, "use tool")]

    assert tool.calls == 0
    assert any(getattr(event, "is_error", False) for event in events)
    tool_messages = [msg for msg in session.messages if msg.role == "tool"]
    assert len(tool_messages) == 1
    assert "Invalid JSON arguments" in (tool_messages[0].content or "")
    payload = session.get_messages_for_api()
    assistant_tool_calls = [msg for msg in payload if msg.get("tool_calls")]
    assert assistant_tool_calls
    assert any(msg["role"] == "tool" and msg["tool_call_id"] == "bad_json" for msg in payload)


def test_loop_step_requires_explicit_completion_marker():
    session = Session(id="loop")
    session.messages.append(Message(role="assistant", content="I made progress."))

    assert not _step_completion_confirmed("", session, 0, step_index=0, total_steps=2)
    assert _step_completion_confirmed(
        "Step 1 complete: done",
        session,
        0,
        step_index=0,
        total_steps=2,
    )

