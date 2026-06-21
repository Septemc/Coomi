from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from coomi.engine.loop import AgentLoop
from coomi.engine.loop_runner import _step_completion_confirmed
from coomi.engine.tool_executor import ToolExecutor
from coomi.security import PermissionLevel, PermissionMode, PermissionSystem
from coomi.services.context.compressor import ContextCompressor
from coomi.services.context.message_guard import SYNTHETIC_TOOL_RESULT
from coomi.services.llm.generic import ThinkingTagFilter, _strip_thinking_tags
from coomi.services.llm.provider import LLMProvider
from coomi.services.llm.text_tool_calls import TextToolCallFilter, parse_text_tool_call
from coomi.tools.base import BaseTool, ToolAccess, ToolConcurrency, ToolResult
from coomi.tools.registry import ToolRegistry
from coomi.ui.events import TextChunk
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


class GlobCountingTool(CountingTool):
    name = "Glob"

    def get_parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["pattern"],
        }


class WebSearchCountingTool(CountingTool):
    name = "WebSearch"

    def get_parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "freshness": {"type": "string"},
            },
            "required": ["query"],
        }


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


class TextToolCallProvider(LLMProvider):
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
                "type": "content",
                "content": "I will search. <tool_call> <function=web_",
            }
            yield {
                "type": "content",
                "content": "search> <parameter=query>coomi software project "
                "<parameter=freshness>all </tool_call>",
            }
        else:
            yield {"type": "content", "content": "done"}

    def switch_model(self, model_name: str) -> str:
        return model_name

    def get_model_display_name(self) -> str:
        return "fake"


class MimoToolCodeProvider(LLMProvider):
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
                "type": "content",
                "content": (
                    'Let me inspect. <tool_code> glob(pattern="**/*", '
                    'path="F:\\_WorkSpace\\Projects\\Storyteller-App") </tool_code>'
                ),
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


def test_compressor_trimmed_payload_remains_provider_safe():
    compressor = ContextCompressor()
    session = Session(id="s", system_prompt="sys")
    session.messages = [Message(role="user", content="first")]
    for i in range(14):
        session.messages.append(Message(role="tool", content=f"orphan {i}", tool_call_id=f"old_{i}"))
        session.messages.append(Message(role="user", content=f"user {i}"))
    session.messages.extend(
        [
            Message(
                role="assistant",
                content=None,
                tool_calls=[ToolCall(id="call_keep", name="Read", arguments={"file_path": "x"})],
            ),
            Message(role="tool", content="result", tool_call_id="call_keep"),
        ]
    )

    session.messages = compressor._trim_old_messages(session.messages)
    payload = session.get_messages_for_api()

    assert payload[0] == {"role": "system", "content": "sys"}
    assert not any(msg["role"] == "tool" and msg["tool_call_id"].startswith("old_") for msg in payload)
    assistant_calls = [msg for msg in payload if msg.get("tool_calls")]
    assert assistant_calls[-1]["tool_calls"][0]["id"] == "call_keep"
    assert payload[-1]["role"] == "tool"
    assert payload[-1]["tool_call_id"] == "call_keep"


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


def test_text_tool_call_filter_parses_xml_style_tool_calls():
    stream_filter = TextToolCallFilter()
    visible_1, calls_1 = stream_filter.feed(
        "I will search <tool_call> <function=web_"
    )
    visible_2, calls_2 = stream_filter.feed(
        "search> <parameter=query>coomi software project "
        "<parameter=freshness>all </tool_call> after"
    )

    assert visible_1 == "I will search "
    assert calls_1 == []
    assert visible_2 == " after"
    assert len(calls_2) == 1
    assert calls_2[0]["name"] == "web_search"
    assert calls_2[0]["arguments"] == {
        "query": "coomi software project",
        "freshness": "all",
    }

    bash_call = parse_text_tool_call(
        "<tool_call> <function=bash> <parameter=command>dir /a "
        "<parameter=description>List files </tool_call>"
    )
    assert bash_call is not None
    assert bash_call["name"] == "bash"
    assert bash_call["arguments"]["command"] == "dir /a"


def test_text_tool_call_filter_parses_mimo_tool_code_and_single_tags():
    stream_filter = TextToolCallFilter()
    visible, calls = stream_filter.feed(
        '<tool_code> glob("**", "F:\\_WorkSpace\\Projects\\Storyteller-App") </tool_code>'
    )

    assert visible == ""
    assert len(calls) == 1
    assert calls[0]["name"] == "glob"
    assert calls[0]["arguments"] == {
        "pattern": "**",
        "path": "F:\\_WorkSpace\\Projects\\Storyteller-App",
    }

    keyword_call = parse_text_tool_call(
        '<tool_code> glob(pattern="**/*", path="F:\\_WorkSpace\\Projects\\Storyteller-App") '
        "</tool_code>"
    )
    assert keyword_call is not None
    assert keyword_call["name"] == "glob"
    assert keyword_call["arguments"]["pattern"] == "**/*"
    assert keyword_call["arguments"]["path"] == "F:\\_WorkSpace\\Projects\\Storyteller-App"

    read_call = parse_text_tool_call(
        "<read_file> F:\\_WorkSpace\\Projects\\Storyteller-App </read_file>"
    )
    assert read_call is not None
    assert read_call["name"] == "read_file"
    assert read_call["arguments"] == {
        "file_path": "F:\\_WorkSpace\\Projects\\Storyteller-App"
    }


@pytest.mark.asyncio
async def test_agent_loop_executes_text_tool_call_without_leaking_markup(tmp_path: Path):
    registry = ToolRegistry()
    tool = WebSearchCountingTool(output="search result")
    registry.register(tool)
    session = Session(id="s", system_prompt="sys")
    permissions = PermissionSystem()
    permissions.set_mode(PermissionMode.APPROVE_FOR_ME)
    agent = AgentLoop(
        TextToolCallProvider(),
        registry,
        project_path=str(tmp_path),
        permission_system=permissions,
    )

    events = [event async for event in agent.run_stream(session, "你知道 coomi 吗")]

    visible_text = "".join(event.content for event in events if isinstance(event, TextChunk))
    assert "<tool_call>" not in visible_text
    assert "<function=" not in visible_text
    assert tool.calls == 1

    assistant_tool_calls = [
        msg.tool_calls[0]
        for msg in session.messages
        if msg.role == "assistant" and msg.tool_calls
    ]
    assert assistant_tool_calls
    assert assistant_tool_calls[0].name == "WebSearch"
    assert assistant_tool_calls[0].arguments["query"] == "coomi software project"
    assert any(msg.role == "tool" and msg.content == "search result" for msg in session.messages)


@pytest.mark.asyncio
async def test_agent_loop_executes_mimo_tool_code_without_leaking_markup(tmp_path: Path):
    registry = ToolRegistry()
    tool = GlobCountingTool(output="glob result")
    registry.register(tool)
    session = Session(id="s", system_prompt="sys")
    agent = AgentLoop(MimoToolCodeProvider(), registry, project_path=str(tmp_path))

    events = [event async for event in agent.run_stream(session, "什么情况")]

    visible_text = "".join(event.content for event in events if isinstance(event, TextChunk))
    assert "<tool_code>" not in visible_text
    assert "glob(" not in visible_text
    assert tool.calls == 1

    assistant_tool_calls = [
        msg.tool_calls[0]
        for msg in session.messages
        if msg.role == "assistant" and msg.tool_calls
    ]
    assert assistant_tool_calls
    assert assistant_tool_calls[0].name == "Glob"
    assert assistant_tool_calls[0].arguments == {
        "pattern": "**/*",
        "path": "F:\\_WorkSpace\\Projects\\Storyteller-App",
    }
    assert any(msg.role == "tool" and msg.content == "glob result" for msg in session.messages)


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


def test_loop_step_completion_marker_must_follow_tool_round():
    session = Session(id="loop")
    session.messages.extend(
        [
            Message(role="assistant", content="Step 1 complete: premature"),
            Message(
                role="assistant",
                tool_calls=[ToolCall(id="call_1", name="Read", arguments={"file_path": "x"})],
            ),
            Message(role="tool", content="Error: failed", tool_call_id="call_1"),
        ]
    )

    assert not _step_completion_confirmed(
        "Step 1 complete: premature",
        session,
        0,
        step_index=0,
        total_steps=2,
        tool_error_occurred=True,
    )

    session.messages.append(Message(role="assistant", content="Step 1 complete: fixed"))
    assert _step_completion_confirmed(
        "",
        session,
        0,
        step_index=0,
        total_steps=2,
        tool_error_occurred=True,
    )


@pytest.mark.asyncio
async def test_plan_mode_blocks_write_tools_before_permission(tmp_path: Path):
    registry = ToolRegistry()
    tool = WriteCountingTool()
    registry.register(tool)
    executor = ToolExecutor(registry, project_path=str(tmp_path), read_only_mode=True)
    session = Session(id="s")

    outcome = await executor.execute(
        session,
        ToolCall(id="call_1", name="Write", arguments={"file_path": "x"}),
    )

    assert outcome.is_error
    assert "Plan Mode is active" in outcome.result_text
    assert tool.calls == 0


def test_permission_modes_change_tool_policy():
    permissions = PermissionSystem()

    permissions.set_mode(PermissionMode.ASK_APPROVAL)
    assert permissions.check_permission("Read", {"file_path": "x"}) == PermissionLevel.AUTO
    assert permissions.check_permission("WebFetch", {"url": "https://example.com"}) == PermissionLevel.ASK

    permissions.set_mode(PermissionMode.APPROVE_FOR_ME)
    assert permissions.check_permission("Write", {"file_path": "x"}) == PermissionLevel.AUTO
    assert permissions.check_permission("Bash", {"command": "python -m pytest"}) == PermissionLevel.AUTO
    assert permissions.check_permission("Bash", {"command": "rm -rf /"}) == PermissionLevel.ASK

    permissions.set_mode(PermissionMode.FULL_ACCESS)
    assert permissions.check_permission("Bash", {"command": "rm -rf /"}) == PermissionLevel.AUTO


def test_thinking_tags_are_removed_from_visible_generic_content():
    content, reasoning = _strip_thinking_tags("<think>hidden</think>visible")
    assert content == "visible"
    assert reasoning == "hidden"

    stream_filter = ThinkingTagFilter()
    reasoning_1, content_1 = stream_filter.feed("<thi")
    reasoning_2, content_2 = stream_filter.feed("nk>hidden</think>visible")

    assert reasoning_1 == ""
    assert content_1 == ""
    assert reasoning_2 == "hidden"
    assert content_2 == "visible"
