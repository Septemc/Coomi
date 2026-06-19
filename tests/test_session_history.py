from __future__ import annotations

import json
import re
from pathlib import Path

from coomi.engine.session import SessionManager, add_assistant_message, add_tool_result, add_user_message
from coomi.services.llm.config import ProviderConfig
from coomi.services.session_history import list_session_records, load_session_from_jsonl


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
