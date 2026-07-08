from __future__ import annotations

import json
import re
from pathlib import Path

from coomi.engine.session import SessionManager, add_assistant_message, add_tool_result, add_user_message
from coomi.services.llm.config import PRESET_PROVIDERS, ProviderConfig
from coomi.services.session_history import list_session_records, load_session_from_jsonl
from coomi.types import ToolCall


def test_session_manager_creates_jsonl_history_file(tmp_path):
    manager = SessionManager(history_dir=tmp_path)

    session = manager.create_session(system_prompt="sys", cwd="C:/work", model="mimo")

    assert session.history_path is not None
    assert re.match(
        r"coomi-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-"
        r"[0-9a-f-]{36}\.jsonl$",
        Path(session.history_path).name,
    )

    first_line = json.loads(Path(session.history_path).read_text(encoding="utf-8").splitlines()[0])
    assert first_line["type"] == "session"
    assert first_line["system_prompt"] == "sys"
    assert first_line["cwd"] == "C:/work"
    assert first_line["model"] == "mimo"


def test_session_history_appends_and_loads_messages(tmp_path):
    manager = SessionManager(history_dir=tmp_path)
    session = manager.create_session(system_prompt="sys")

    add_user_message(session, "Please fix the bug")
    add_assistant_message(session, "Done")
    add_tool_result(session, "tool_1", "ok")

    records = list_session_records(tmp_path)
    assert len(records) == 1
    assert records[0].title == "Please fix the bug"
    assert records[0].message_count == 3

    loaded = load_session_from_jsonl(records[0].path)
    assert loaded.id == session.id
    assert loaded.system_prompt == "sys"
    assert [msg.role for msg in loaded.messages] == ["user", "assistant", "tool"]
    assert loaded.messages[0].content == "Please fix the bug"
    assert loaded.messages[2].tool_call_id == "tool_1"
    assert loaded.history_path == str(records[0].path)


def test_session_history_preserves_tool_call_source(tmp_path):
    manager = SessionManager(history_dir=tmp_path)
    session = manager.create_session(system_prompt="sys")

    add_assistant_message(
        session,
        None,
        [
            ToolCall(
                id="text_call_1",
                name="Read",
                arguments={"file_path": "x"},
                source="text_fallback",
            )
        ],
    )

    records = list_session_records(tmp_path, include_empty=True)
    loaded = load_session_from_jsonl(records[0].path)

    assert loaded.messages[0].tool_calls is not None
    assert loaded.messages[0].tool_calls[0].source == "text_fallback"


def test_legacy_deepseek_type_is_normalized_to_generic():
    provider = ProviderConfig.from_dict(
        "deepseek",
        {
            "type": "deepseek",
            "display": "DeepSeek",
            "api_key": "sk",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-chat",
        },
    )

    assert provider.type == "generic"


def test_provider_tool_protocol_auto_infers_text_modes():
    mimo = ProviderConfig.from_dict(
        "mimo",
        {
            "type": "generic",
            "display": "MIMO V2.5 Pro",
            "api_key": "sk",
            "base_url": "https://token-plan-cn.xiaomimimo.com/v1",
            "model": "MiMo-V2.5-Pro",
        },
    )
    minimax = ProviderConfig.from_dict(
        "minimax",
        {
            "type": "generic",
            "display": "MiniMax",
            "api_key": "sk",
            "base_url": "https://api.minimaxi.com/v1",
            "model": "MiniMax-M2.7",
        },
    )
    generic = ProviderConfig.from_dict(
        "other",
        {
            "type": "generic",
            "display": "Other",
            "api_key": "sk",
            "base_url": "https://example.com/v1",
            "model": "model",
        },
    )

    assert mimo.resolved_tool_protocol() == "mimo"
    assert mimo.text_tool_mode() == "mimo"
    assert minimax.resolved_tool_protocol() == "native"
    assert minimax.text_tool_mode() == "structured"
    assert generic.resolved_tool_protocol() == "structured"
    assert generic.text_tool_mode() == "structured"


def test_deepseek_presets_cover_openai_and_anthropic_compatible_modes():
    openai_preset = PRESET_PROVIDERS["deepseek-openai"]
    anthropic_preset = PRESET_PROVIDERS["deepseek-anthropic"]

    assert openai_preset["type"] == "generic"
    assert openai_preset["base_url"] == "https://api.deepseek.com"
    assert openai_preset["tool_protocol"] == "structured"
    assert anthropic_preset["type"] == "anthropic"
    assert anthropic_preset["base_url"] == "https://api.deepseek.com/anthropic"
    assert anthropic_preset["tool_protocol"] == "native"
