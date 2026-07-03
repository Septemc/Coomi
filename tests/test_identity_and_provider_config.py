from __future__ import annotations

import json
from pathlib import Path

import pytest

from coomi.engine.session import SessionManager, build_system_prompt
from coomi.services.llm import config as llm_config
from coomi.services.llm.config import ConfigManager, ProviderConfig
from coomi.types import Session


@pytest.mark.asyncio
async def test_system_prompt_identifies_coomi_agent() -> None:
    prompt = await build_system_prompt(cwd="C:/work")

    assert "You are Coomi Agent." in prompt
    assert "Coomi Agent is your only product identity." in prompt
    assert "You are a helpful assistant" not in prompt


def test_session_defaults_identify_coomi_agent() -> None:
    manager = SessionManager(persist_history=False)

    assert Session(id="s").system_prompt == "You are Coomi Agent."
    assert manager.create_session().system_prompt == "You are Coomi Agent."


def test_add_provider_sets_active_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_config.Path, "home", lambda: tmp_path)
    manager = ConfigManager()

    provider = ProviderConfig(
        id="mimo",
        type="generic",
        display="MIMO",
        api_key="test-key",
        base_url="https://example.com/v1",
        model="mimo-model",
    )
    manager.add_provider(provider)

    assert manager.data["active"] == "mimo"
    saved = json.loads(manager.config_path.read_text(encoding="utf-8"))
    assert saved["active"] == "mimo"


def test_reload_reads_provider_config_from_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_config.Path, "home", lambda: tmp_path)
    manager = ConfigManager()
    manager.config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "active": "updated",
                "providers": {
                    "updated": {
                        "type": "generic",
                        "display": "Updated",
                        "api_key": "test-key",
                        "model": "updated-model",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    manager.reload()

    assert manager.data["active"] == "updated"
    assert manager.get_active().model == "updated-model"
