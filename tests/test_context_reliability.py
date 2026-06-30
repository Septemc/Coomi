from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from coomi.engine.loop import AgentLoop, _should_omit_tools_for_input
from coomi.engine.loop_runner import _step_completion_confirmed
from coomi.engine.tool_executor import ToolExecutor, _summarize_arguments
from coomi.security import PermissionLevel, PermissionMode, PermissionSystem
from coomi.services.context.compressor import ContextCompressor
from coomi.services.context.message_guard import SYNTHETIC_TOOL_RESULT
from coomi.services.llm.config import ProviderConfig
from coomi.services.llm.generic import ThinkingTagFilter, _strip_thinking_tags
from coomi.services.llm.provider import LLMProvider
from coomi.services.llm.text_tool_calls import TextToolCallFilter, parse_text_tool_call
from coomi.tools.base import BaseTool, ToolAccess, ToolConcurrency, ToolResult
from coomi.tools.registry import ToolRegistry, create_default_registry
from coomi.tools.file_ops.edit import EditTool
from coomi.tools.shell import BashTool, PowerShellTool
from coomi.tools.user import AskUserQuestionTool
from coomi.ui.events import ReasoningChunk, TextChunk
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


class BashCountingTool(CountingTool):
    name = "Bash"

    def get_parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
            },
            "required": ["command"],
        }


class FakeQuestionApp:
    def __init__(self):
        self.questions: list[dict] | None = None

    async def _handle_ask_questions(self, questions: list[dict]) -> dict:
        self.questions = questions
        return {0: {"option": "A", "label": "A", "other_text": None}}


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

    def get_text_tool_mode(self) -> str:
        return "disabled"


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

    def get_text_tool_mode(self) -> str:
        return "structured"


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

    def get_text_tool_mode(self) -> str:
        return "mimo"


class DsmlToolCallProvider(LLMProvider):
    def __init__(self):
        self.calls = 0

    async def chat(self, messages, tools=None, **kwargs):
        return LLMResponse(content="ok")

    async def chat_stream(self, messages, **kwargs):
        yield "ok"

    async def chat_stream_with_tools(self, messages, tools=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield {"type": "content", "content": "Checking <| | DSML | | tool"}
            yield {
                "type": "content",
                "content": (
                    '_calls><| | DSML | | invoke name="Glob">'
                    '<| | DSML | | parameter name="pattern" string="true">**/*.py'
                    '</| | DSML | | parameter>'
                    '<| | DSML | | parameter name="path" string="true">'
                    'F:\\_WorkSpace\\Projects\\Coomi'
                    '</| | DSML | | parameter>'
                    '</| | DSML | | invoke></| | DSML | | tool_calls> done'
                ),
            }
        else:
            yield {"type": "content", "content": "done"}

    def switch_model(self, model_name: str) -> str:
        return model_name

    def get_model_display_name(self) -> str:
        return "fake"

    def get_text_tool_mode(self) -> str:
        return "structured"


class NativeConfiguredDsmlProvider(DsmlToolCallProvider):
    def __init__(self):
        super().__init__()
        self.config = ProviderConfig(
            id="native-generic",
            type="generic",
            display="Native Generic",
            api_key="test",
            model="native-model",
            tool_protocol="native",
        )

    def get_text_tool_mode(self) -> str:
        return LLMProvider.get_text_tool_mode(self)


class ReasoningDsmlToolCallProvider(DsmlToolCallProvider):
    async def chat_stream_with_tools(self, messages, tools=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield {"type": "reasoning_content", "content": "Thinking <| | DSML | | tool"}
            yield {
                "type": "reasoning_content",
                "content": (
                    '_calls><| | DSML | | invoke name="Glob">'
                    '<| | DSML | | parameter name="pattern" string="true">**/*.py'
                    '</| | DSML | | parameter>'
                    '</| | DSML | | invoke></| | DSML | | tool_calls>'
                ),
            }
        else:
            yield {"type": "content", "content": "done"}


class JsonTextToolCallProvider(LLMProvider):
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
                "content": 'Looking {"name":"Read","arguments":{"file_path":"F:\\\\tmp\\\\a.txt"}} done',
            }
        else:
            yield {"type": "content", "content": "done"}

    def switch_model(self, model_name: str) -> str:
        return model_name

    def get_model_display_name(self) -> str:
        return "fake"

    def get_text_tool_mode(self) -> str:
        return "structured"


class FunctionTextToolCallProvider(LLMProvider):
    def __init__(self):
        self.calls = 0

    async def chat(self, messages, tools=None, **kwargs):
        return LLMResponse(content="ok")

    async def chat_stream(self, messages, **kwargs):
        yield "ok"

    async def chat_stream_with_tools(self, messages, tools=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield {"type": "content", "content": 'I will read Read(file_path="F:\\tmp\\fn.txt")'}
        else:
            yield {"type": "content", "content": "done"}

    def switch_model(self, model_name: str) -> str:
        return model_name

    def get_model_display_name(self) -> str:
        return "fake"

    def get_text_tool_mode(self) -> str:
        return "structured"


class MalformedThenValidTextToolProvider(LLMProvider):
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
                "content": "<tool_call><function=Read><parameter=file_path>F:\\tmp\\missing-close.txt",
            }
        elif self.calls == 2:
            yield {
                "type": "content",
                "content": (
                    '<tool_call>{"name":"Read","arguments":{"file_path":"F:\\\\tmp\\\\fixed.txt"}}'
                    "</tool_call>"
                ),
            }
        else:
            yield {"type": "content", "content": "done"}

    def switch_model(self, model_name: str) -> str:
        return model_name

    def get_model_display_name(self) -> str:
        return "fake"

    def get_text_tool_mode(self) -> str:
        return "structured"


class AlwaysMalformedTextToolProvider(MalformedThenValidTextToolProvider):
    async def chat_stream_with_tools(self, messages, tools=None, **kwargs):
        self.calls += 1
        yield {
            "type": "content",
            "content": '{"name":"Read","arguments":',
        }


class DisabledTextToolCodeProvider(MimoToolCodeProvider):
    def get_text_tool_mode(self) -> str:
        return "disabled"


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
async def test_ask_user_question_bypasses_permission_prompt():
    registry = ToolRegistry()
    registry.register(AskUserQuestionTool())
    app = FakeQuestionApp()
    executor = ToolExecutor(registry, app_context=app)
    session = Session(id="s")
    questions = [
        {
            "header": "基础",
            "question": "你目前的深度学习基础在什么水平？",
            "options": [
                {"label": "入门级", "description": "了解基本概念"},
                {"label": "中级", "description": "做过项目"},
            ],
        }
    ]

    outcome = await executor.execute(
        session,
        ToolCall(id="call_ask", name="AskUserQuestion", arguments={"questions": questions}),
    )

    assert not outcome.is_error
    assert app.questions == questions
    assert '"label": "A"' in outcome.result_text


def test_permission_summary_formats_questions_without_raw_dict():
    summary = _summarize_arguments(
        {
            "questions": [
                {
                    "header": "基础",
                    "question": "你目前的深度学习基础在什么水平？",
                    "options": [
                        {"label": "入门级", "description": "了解基本概念"},
                        {"label": "中级", "description": "做过项目"},
                    ],
                },
                {
                    "header": "方向",
                    "question": "你想重点学习哪些方向？",
                    "multiSelect": True,
                    "options": [
                        {"label": "CV", "description": "图像分类"},
                        {"label": "NLP", "description": "文本分类"},
                    ],
                },
            ]
        },
        tool_name="AskUserQuestion",
    )

    assert "Questions: 2" in summary
    assert "Q1: 你目前的深度学习基础在什么水平？ (2 options)" in summary
    assert "Q2: 你想重点学习哪些方向？ (2 options multi-select)" in summary
    assert "{'questions'" not in summary


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
    assert bash_call["source"] == "text_fallback"


def test_text_tool_call_filter_parses_mimo_tool_code_and_single_tags():
    stream_filter = TextToolCallFilter(mode="mimo")
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
        "<read_file> F:\\_WorkSpace\\Projects\\Storyteller-App </read_file>",
        mode="mimo",
    )
    assert read_call is not None
    assert read_call["name"] == "read_file"
    assert read_call["arguments"] == {
        "file_path": "F:\\_WorkSpace\\Projects\\Storyteller-App"
    }


def test_text_tool_call_filter_parses_dsml_tool_calls_without_leaking_markup():
    stream_filter = TextToolCallFilter()
    visible_1, calls_1 = stream_filter.feed("Before <| | DSML | | tool")
    visible_2, calls_2 = stream_filter.feed(
        '_calls><| | DSML | | invoke name="Edit">'
        '<| | DSML | | parameter name="file_path" string="true">F:\\tmp\\index.html'
        '</| | DSML | | parameter>'
        '<| | DSML | | parameter name="old_string" string="true">window.x = null;'
        '</| | DSML | | parameter>'
        '<| | DSML | | parameter name="new_string" string="true">window.x = true;'
        '</| | DSML | | parameter>'
        '</| | DSML | | invoke></| | DSML | | tool_calls> after'
    )

    assert visible_1 == "Before "
    assert calls_1 == []
    assert visible_2 == " after"
    assert len(calls_2) == 1
    assert calls_2[0]["name"] == "Edit"
    assert calls_2[0]["source"] == "text_fallback"
    assert calls_2[0]["arguments"] == {
        "file_path": "F:\\tmp\\index.html",
        "old_string": "window.x = null;",
        "new_string": "window.x = true;",
    }


def test_text_tool_call_filter_preserves_dsml_string_whitespace():
    stream_filter = TextToolCallFilter()
    visible, calls = stream_filter.feed(
        '<| | DSML | | tool_calls><| | DSML | | invoke name="Edit">'
        '<| | DSML | | parameter name="file_path" string="true">F:\\tmp\\index.html'
        '</| | DSML | | parameter>'
        '<| | DSML | | parameter name="old_string" string="true">    <link rel="icon" '
        'href="assets/tensorhub.png" type="image/png">'
        '</| | DSML | | parameter>'
        '<| | DSML | | parameter name="new_string" string="true">    <link rel="icon" '
        'href="assets/tensorhub_icon.png" type="image/png">'
        '</| | DSML | | parameter>'
        '</| | DSML | | invoke></| | DSML | | tool_calls>'
    )

    assert visible == ""
    assert len(calls) == 1
    assert calls[0]["arguments"]["old_string"].startswith("    <link")
    assert calls[0]["arguments"]["new_string"].startswith("    <link")


def test_text_tool_call_filter_parses_screenshot_bash_dsml_and_variants():
    screenshot_call = (
        '<| | DSML | | tool_calls><| | DSML | | invoke name="Bash">'
        '<| | DSML | | parameter name="command" string="true">'
        'cd "F:\\_WorkSpace\\Projects\\TensorHub" && for %f in '
        '(assets\\tensorhub.png assets\\tensorhub_icon.png) do @echo %~nxf: %~zf bytes'
        '</| | DSML | | parameter>'
        '<| | DSML | | parameter name="description" string="true">Check image file sizes'
        '</| | DSML | | parameter></| | DSML | | invoke></| | DSML | | tool_calls>'
    )
    stream_filter = TextToolCallFilter()
    visible, calls = stream_filter.feed(screenshot_call)

    assert visible == ""
    assert len(calls) == 1
    assert calls[0]["name"] == "Bash"
    assert calls[0]["source"] == "text_fallback"
    assert calls[0]["parse_error"] is None
    assert calls[0]["arguments"] == {
        "command": (
            'cd "F:\\_WorkSpace\\Projects\\TensorHub" && for %f in '
            "(assets\\tensorhub.png assets\\tensorhub_icon.png) do @echo %~nxf: %~zf bytes"
        ),
        "description": "Check image file sizes",
    }

    for raw in (
        '<||DSML||tool_calls><||DSML||invoke name="Read">'
        '<||DSML||parameter name="file_path" string="true">F:\\tmp\\a.txt'
        '</||DSML||parameter></||DSML||invoke></||DSML||tool_calls>',
        '< | | DSML | | tool_calls >< | | DSML | | invoke name="Read" >'
        '< | | DSML | | parameter name="file_path" string="true" >F:\\tmp\\b.txt'
        '</ | | DSML | | parameter ></ | | DSML | | invoke ></ | | DSML | | tool_calls >',
        '<\uff5c \uff5c DSML \uff5c \uff5c tool_calls><\uff5c \uff5c DSML \uff5c \uff5c invoke name="Read">'
        '<\uff5c \uff5c DSML \uff5c \uff5c parameter name="file_path" string="true">F:\\tmp\\c.txt'
        '</\uff5c \uff5c DSML \uff5c \uff5c parameter></\uff5c \uff5c DSML \uff5c \uff5c invoke>'
        '</\uff5c \uff5c DSML \uff5c \uff5c tool_calls>',
    ):
        variant_filter = TextToolCallFilter()
        visible, calls = variant_filter.feed(raw)
        assert visible == ""
        assert len(calls) == 1
        assert calls[0]["name"] == "Read"
        assert calls[0]["arguments"]["file_path"].startswith("F:\\tmp\\")


def test_text_tool_call_filter_parses_json_function_and_fenced_calls():
    json_filter = TextToolCallFilter()
    visible, calls = json_filter.feed(
        'Before {"name":"Read","arguments":{"file_path":"F:\\\\tmp\\\\a.txt"}} after'
    )
    assert visible == "Before  after"
    assert len(calls) == 1
    assert calls[0]["name"] == "Read"
    assert calls[0]["arguments"] == {"file_path": "F:\\tmp\\a.txt"}

    function_filter = TextToolCallFilter()
    visible, calls = function_filter.feed('Run Bash(command="echo hello") now')
    assert visible == "Run  now"
    assert len(calls) == 1
    assert calls[0]["name"] == "Bash"
    assert calls[0]["arguments"] == {"command": "echo hello"}

    fenced_call = parse_text_tool_call(
        '```json\n{"tool":"Read","input":{"file_path":"F:\\\\tmp\\\\b.txt"}}\n```'
    )
    assert fenced_call is not None
    assert fenced_call["name"] == "Read"
    assert fenced_call["arguments"] == {"file_path": "F:\\tmp\\b.txt"}


def test_text_tool_call_filter_turns_malformed_calls_into_correction():
    stream_filter = TextToolCallFilter()
    visible, calls = stream_filter.feed('{"name":"Read","arguments":')
    tail, tail_calls = stream_filter.flush()

    assert visible + tail == ""
    all_calls = calls + tail_calls
    assert len(all_calls) == 1
    assert all_calls[0]["name"] == "InvalidToolCall"
    assert "Malformed text tool call detected" in all_calls[0]["parse_error"]
    assert "Read: file_path" in all_calls[0]["parse_error"]


def test_text_tool_call_filter_does_not_parse_single_tags_by_default():
    stream_filter = TextToolCallFilter()
    visible, calls = stream_filter.feed("Example: <bash>echo hello</bash> is a tag.")
    tail, tail_calls = stream_filter.flush()

    assert visible + tail == "Example: <bash>echo hello</bash> is a tag."
    assert calls + tail_calls == []
    assert parse_text_tool_call("<read_file> C:\\tmp\\a.txt </read_file>") is None


def test_text_tool_call_filter_does_not_misread_plain_language():
    stream_filter = TextToolCallFilter()
    visible, calls = stream_filter.feed(
        "I can read the plan, edit the summary, and search later without using a tool."
    )
    tail, tail_calls = stream_filter.flush()

    assert visible + tail == (
        "I can read the plan, edit the summary, and search later without using a tool."
    )
    assert calls + tail_calls == []


def test_provider_text_fallback_stays_enabled_for_native_protocol():
    native = ProviderConfig(
        id="native",
        type="generic",
        display="Native",
        api_key="test",
        model="native-model",
        tool_protocol="native",
    )
    disabled = ProviderConfig(
        id="disabled",
        type="generic",
        display="Disabled",
        api_key="test",
        model="disabled-model",
        tool_protocol="disabled",
    )

    assert native.resolved_tool_protocol() == "native"
    assert native.text_tool_mode() == "structured"
    assert disabled.text_tool_mode() == "disabled"


def test_short_visual_edit_prompts_keep_tools_available():
    assert not _should_omit_tools_for_input("\u6536\u85cf\u5939\u56fe\u6807\u6211\u9700\u8981\u4f7f\u7528\u5706\u89d2\u56fe\u6807")
    assert not _should_omit_tools_for_input("\u5c06\u6536\u85cf\u5939\u56fe\u6807\u8bbe\u8ba1\u4e3a\u5706\u89d2\u56fe\u6807")
    assert not _should_omit_tools_for_input("\u628a favicon \u6539\u6210\u5706\u89d2\u56fe\u6807")


def test_default_registry_contains_core_tools_and_aliases():
    registry = create_default_registry()

    assert registry.get("Bash") is not None
    assert registry.get("PowerShell") is not None
    assert registry.get("Read") is not None
    assert registry.get("Edit") is not None
    assert registry.canonical_name("bash") == "Bash"
    assert registry.canonical_name("pwsh") == "PowerShell"
    assert registry.canonical_name("read_file") == "Read"
    assert registry.canonical_name("editfile") == "Edit"


def test_edit_tool_rejects_empty_old_string(tmp_path: Path):
    target = tmp_path / "index.html"
    target.write_text("abc", encoding="utf-8")

    result = EditTool().run(
        {
            "file_path": str(target),
            "old_string": "",
            "new_string": "x",
        }
    )

    assert not result.success
    assert "old_string must not be empty" in (result.error or "")
    assert target.read_text(encoding="utf-8") == "abc"


def test_shell_tool_descriptions_route_windows_commands_to_powershell():
    bash = BashTool()
    powershell = PowerShellTool()

    assert "PowerShell" in bash.description
    assert "Windows" in bash.description
    assert "preferred on Windows" in powershell.get_parameters_schema()["properties"]["command"]["description"]


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

    events = [event async for event in agent.run_stream(session, "please web search coomi")]

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
    assert assistant_tool_calls[0].source == "text_fallback"
    assert assistant_tool_calls[0].arguments["query"] == "coomi software project"
    assert any(msg.role == "tool" and msg.content == "search result" for msg in session.messages)


@pytest.mark.asyncio
async def test_agent_loop_executes_mimo_tool_code_without_leaking_markup(tmp_path: Path):
    registry = ToolRegistry()
    tool = GlobCountingTool(output="glob result")
    registry.register(tool)
    session = Session(id="s", system_prompt="sys")
    agent = AgentLoop(MimoToolCodeProvider(), registry, project_path=str(tmp_path))

    events = [event async for event in agent.run_stream(session, "please search project files")]

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
    assert assistant_tool_calls[0].source == "text_fallback"
    assert assistant_tool_calls[0].arguments == {
        "pattern": "**/*",
        "path": "F:\\_WorkSpace\\Projects\\Storyteller-App",
    }
    assert any(msg.role == "tool" and msg.content == "glob result" for msg in session.messages)


@pytest.mark.asyncio
async def test_agent_loop_executes_json_text_tool_call_without_leaking_markup(tmp_path: Path):
    registry = ToolRegistry()
    tool = CountingTool(output="read result")
    registry.register(tool)
    session = Session(id="s", system_prompt="sys")
    agent = AgentLoop(JsonTextToolCallProvider(), registry, project_path=str(tmp_path))

    events = [event async for event in agent.run_stream(session, "please read a file")]

    visible_text = "".join(event.content for event in events if isinstance(event, TextChunk))
    assert '{"name"' not in visible_text
    assert tool.calls == 1
    assistant_tool_calls = [
        msg.tool_calls[0]
        for msg in session.messages
        if msg.role == "assistant" and msg.tool_calls
    ]
    assert assistant_tool_calls[0].name == "Read"
    assert assistant_tool_calls[0].arguments == {"file_path": "F:\\tmp\\a.txt"}


@pytest.mark.asyncio
async def test_agent_loop_executes_function_text_tool_call_without_leaking_markup(tmp_path: Path):
    registry = ToolRegistry()
    tool = CountingTool(output="read result")
    registry.register(tool)
    session = Session(id="s", system_prompt="sys")
    agent = AgentLoop(
        FunctionTextToolCallProvider(),
        registry,
        project_path=str(tmp_path),
    )

    events = [event async for event in agent.run_stream(session, "please run a command")]

    visible_text = "".join(event.content for event in events if isinstance(event, TextChunk))
    assert "Read(" not in visible_text
    assert tool.calls == 1
    assistant_tool_calls = [
        msg.tool_calls[0]
        for msg in session.messages
        if msg.role == "assistant" and msg.tool_calls
    ]
    assert assistant_tool_calls[0].name == "Read"
    assert assistant_tool_calls[0].arguments == {"file_path": "F:\\tmp\\fn.txt"}


@pytest.mark.asyncio
async def test_agent_loop_executes_dsml_tool_call_without_leaking_markup(tmp_path: Path):
    registry = ToolRegistry()
    tool = GlobCountingTool(output="glob result")
    registry.register(tool)
    session = Session(id="s", system_prompt="sys")
    agent = AgentLoop(DsmlToolCallProvider(), registry, project_path=str(tmp_path))

    events = [event async for event in agent.run_stream(session, "please search project files")]

    visible_text = "".join(event.content for event in events if isinstance(event, TextChunk))
    assert "DSML" not in visible_text
    assert "invoke" not in visible_text
    assert tool.calls == 1

    assistant_tool_calls = [
        msg.tool_calls[0]
        for msg in session.messages
        if msg.role == "assistant" and msg.tool_calls
    ]
    assert assistant_tool_calls
    assert assistant_tool_calls[0].name == "Glob"
    assert assistant_tool_calls[0].source == "text_fallback"
    assert assistant_tool_calls[0].arguments == {
        "pattern": "**/*.py",
        "path": "F:\\_WorkSpace\\Projects\\Coomi",
    }
    assert any(msg.role == "tool" and msg.content == "glob result" for msg in session.messages)


@pytest.mark.asyncio
async def test_agent_loop_captures_dsml_when_provider_protocol_is_native(tmp_path: Path):
    registry = ToolRegistry()
    tool = GlobCountingTool(output="glob result")
    registry.register(tool)
    session = Session(id="s", system_prompt="sys")
    agent = AgentLoop(NativeConfiguredDsmlProvider(), registry, project_path=str(tmp_path))

    events = [event async for event in agent.run_stream(session, "please search project files")]

    visible_text = "".join(event.content for event in events if isinstance(event, TextChunk))
    assert "DSML" not in visible_text
    assert tool.calls == 1
    assistant_tool_calls = [
        msg.tool_calls[0]
        for msg in session.messages
        if msg.role == "assistant" and msg.tool_calls
    ]
    assert assistant_tool_calls[0].name == "Glob"
    assert assistant_tool_calls[0].source == "text_fallback"


@pytest.mark.asyncio
async def test_agent_loop_logs_when_text_tool_mode_disabled_but_content_looks_like_tool(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    registry = ToolRegistry()
    tool = GlobCountingTool(output="glob result")
    registry.register(tool)
    session = Session(id="s", system_prompt="sys")
    agent = AgentLoop(DisabledTextToolCodeProvider(), registry, project_path=str(tmp_path))

    with caplog.at_level("WARNING", logger="coomi.engine.loop"):
        events = [event async for event in agent.run_stream(session, "please search project files")]

    visible_text = "".join(event.content for event in events if isinstance(event, TextChunk))
    assert "<tool_code>" in visible_text
    assert tool.calls == 0
    assert "Text tool parsing is disabled" in caplog.text


@pytest.mark.asyncio
async def test_agent_loop_recovers_from_malformed_text_tool_call(tmp_path: Path):
    registry = ToolRegistry()
    tool = CountingTool(output="read result")
    registry.register(tool)
    session = Session(id="s", system_prompt="sys")
    agent = AgentLoop(MalformedThenValidTextToolProvider(), registry, project_path=str(tmp_path))

    events = [event async for event in agent.run_stream(session, "please read a file")]

    visible_text = "".join(event.content for event in events if isinstance(event, TextChunk))
    assert "missing-close" not in visible_text
    assert "Malformed text tool call detected" in "\n".join(
        msg.content or "" for msg in session.messages if msg.role == "tool"
    )
    assert tool.calls == 1
    assert any(msg.role == "tool" and msg.content == "read result" for msg in session.messages)


@pytest.mark.asyncio
async def test_agent_loop_stops_repeated_malformed_text_tool_calls(tmp_path: Path):
    registry = ToolRegistry()
    tool = CountingTool(output="read result")
    registry.register(tool)
    session = Session(id="s", system_prompt="sys")
    provider = AlwaysMalformedTextToolProvider()
    agent = AgentLoop(provider, registry, project_path=str(tmp_path))

    events = [event async for event in agent.run_stream(session, "please read a file")]

    assert provider.calls == 3
    assert tool.calls == 0
    assert any(
        getattr(event, "message", "").startswith("Stopped malformed text tool-call recovery")
        for event in events
    )


@pytest.mark.asyncio
async def test_agent_loop_executes_text_tool_call_when_native_tools_omitted(tmp_path: Path):
    registry = ToolRegistry()
    tool = GlobCountingTool(output="glob result")
    registry.register(tool)
    session = Session(id="s", system_prompt="sys")
    agent = AgentLoop(MimoToolCodeProvider(), registry, project_path=str(tmp_path))

    events = [event async for event in agent.run_stream(session, "hi")]

    visible_text = "".join(event.content for event in events if isinstance(event, TextChunk))
    assert "<tool_code>" not in visible_text
    assert tool.calls == 1
    assert any(msg.role == "tool" and msg.content == "glob result" for msg in session.messages)


@pytest.mark.asyncio
async def test_agent_loop_executes_reasoning_text_tool_call_without_leaking_markup(tmp_path: Path):
    registry = ToolRegistry()
    tool = GlobCountingTool(output="glob result")
    registry.register(tool)
    session = Session(id="s", system_prompt="sys")
    agent = AgentLoop(ReasoningDsmlToolCallProvider(), registry, project_path=str(tmp_path))

    events = [event async for event in agent.run_stream(session, "please search project files")]

    visible_text = "".join(event.content for event in events if isinstance(event, TextChunk))
    visible_reasoning = "".join(
        event.content for event in events if isinstance(event, ReasoningChunk)
    )
    assert "DSML" not in visible_text
    assert "DSML" not in visible_reasoning
    assert tool.calls == 1
    assert any(msg.role == "tool" and msg.content == "glob result" for msg in session.messages)


@pytest.mark.asyncio
async def test_agent_loop_respects_provider_disabled_text_tool_mode(tmp_path: Path):
    registry = ToolRegistry()
    tool = GlobCountingTool(output="glob result")
    registry.register(tool)
    session = Session(id="s", system_prompt="sys")
    agent = AgentLoop(DisabledTextToolCodeProvider(), registry, project_path=str(tmp_path))

    events = [event async for event in agent.run_stream(session, "please search project files")]

    visible_text = "".join(event.content for event in events if isinstance(event, TextChunk))
    assert "<tool_code>" in visible_text
    assert tool.calls == 0
    assert not any(msg.role == "tool" for msg in session.messages)


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


@pytest.mark.asyncio
async def test_tool_executor_forces_ask_for_text_fallback_write_even_full_access(tmp_path: Path):
    registry = ToolRegistry()
    tool = WriteCountingTool()
    registry.register(tool)
    permissions = PermissionSystem()
    permissions.set_mode(PermissionMode.FULL_ACCESS)
    executor = ToolExecutor(
        registry,
        permission_system=permissions,
        project_path=str(tmp_path),
    )
    session = Session(id="s")

    outcome = await executor.execute(
        session,
        ToolCall(
            id="call_1",
            name="Write",
            arguments={"file_path": "x"},
            source="text_fallback",
        ),
    )

    assert outcome.is_error
    assert "Permission required" in outcome.result_text
    assert tool.calls == 0


def test_permission_modes_change_tool_policy():
    permissions = PermissionSystem()

    permissions.set_mode(PermissionMode.ASK_APPROVAL)
    assert permissions.check_permission("Read", {"file_path": "x"}) == PermissionLevel.AUTO
    assert permissions.check_permission("AskUserQuestion", {"questions": []}) == PermissionLevel.AUTO
    assert permissions.check_permission("WebFetch", {"url": "https://example.com"}) == PermissionLevel.ASK

    permissions.set_mode(PermissionMode.APPROVE_FOR_ME)
    assert permissions.check_permission("AskUserQuestion", {"questions": []}) == PermissionLevel.AUTO
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
