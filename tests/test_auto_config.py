from __future__ import annotations

from pathlib import Path

import pytest

from coomi.security import PermissionMode
from coomi.services.auto_config import (
    INTENT_MCP,
    INTENT_NONE,
    INTENT_PROVIDER,
    INTENT_SKILL,
    InputIntentDetector,
    McpAutoConfigurator,
    ProviderAutoConfigurator,
    SkillAutoInstaller,
    normalize_mcp_config,
    normalize_provider_config,
)
from coomi.services.llm.config import ConfigManager
from coomi.services.mcp.config import McpConfigStore
from coomi.services.mcp.manager import McpManager
from coomi.services.mcp.models import McpToolSpec
from coomi.services.mcp import manager as mcp_manager_module
from coomi.services.skills.config import SkillConfig
from coomi.services.skills.manager import SkillManager
from coomi.tools.registry import ToolRegistry
from coomi.ui.textual_app import CoomiApp


def test_input_intent_detector_recognizes_supported_inputs(tmp_path: Path) -> None:
    detector = InputIntentDetector()
    local_skill = tmp_path / "demo-skill"
    local_skill.mkdir()

    assert detector.detect('{"id":"openai-main","api_key":"sk-test","model":"gpt-4.1"}').kind == INTENT_PROVIDER
    assert detector.detect('{"name":"files","transport":"stdio","command":"npx"}').kind == INTENT_MCP

    mcp_url = detector.detect("https://example.com/mcp")
    assert mcp_url.kind == INTENT_MCP
    assert mcp_url.data["config"]["url"] == "https://example.com/mcp"

    mcp_command = detector.detect("mcp add files stdio npx -y @modelcontextprotocol/server-filesystem F:\\Work")
    assert mcp_command.kind == INTENT_MCP
    assert mcp_command.data["command_text"].startswith("/mcp add")

    github_skill = detector.detect("https://github.com/owner/repo/tree/main/skills/demo")
    assert github_skill.kind == INTENT_SKILL

    local = detector.detect(str(local_skill))
    assert local.kind == INTENT_SKILL
    assert local.data["source"] == str(local_skill)

    assert detector.detect("please help me design a roadmap").kind == INTENT_NONE


def test_provider_auto_configurator_normalizes_redacts_and_activates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    manager = ConfigManager()
    result = ProviderAutoConfigurator(manager).configure(
        {
            "name": "DeepSeek Main",
            "provider": "deepseek",
            "key": "sk-abcdefghijklmnopqrstuvwxyz",
            "baseUrl": "https://api.deepseek.com",
            "model_name": "deepseek-chat",
            "protocol": "native",
        }
    )

    assert result.success is True
    assert manager.data["active"] == "DeepSeek-Main"
    assert manager.get_provider("DeepSeek-Main").model == "deepseek-chat"
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in result.message
    assert "sk-****wxyz" in result.message


def test_provider_normalization_rejects_invalid_tool_protocol() -> None:
    with pytest.raises(ValueError):
        normalize_provider_config(
            {"id": "bad", "api_key": "sk-test", "model": "demo", "tool_protocol": "unsafe"}
        )


def test_skill_auto_installer_repairs_missing_skill_md(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("# Demo Helper\n\nUseful helpers.", encoding="utf-8")
    manager = SkillManager(
        SkillConfig(config_path=tmp_path / "skills.json", skills_dir=tmp_path / "skills")
    )

    result = SkillAutoInstaller(manager).install(str(source))

    assert result.success is True
    assert "generated SKILL.md" in result.repairs
    record = manager.get("source")
    assert record is not None
    assert record.enabled is True
    assert Path(record.path, "SKILL.md").exists()
    assert "$source" in manager.build_prompt_context("hello")


def test_skill_auto_installer_returns_selection_for_multiple_candidates(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "skills" / "a").mkdir(parents=True)
    (root / "skills" / "b").mkdir(parents=True)
    (root / "skills" / "a" / "SKILL.md").write_text("# A\nDescription: A", encoding="utf-8")
    (root / "skills" / "b" / "SKILL.md").write_text("# B\nDescription: B", encoding="utf-8")
    manager = SkillManager(
        SkillConfig(config_path=tmp_path / "skills.json", skills_dir=tmp_path / "skills")
    )

    result = SkillAutoInstaller(manager).install(str(root))

    assert result.success is False
    assert result.status == "Needs selection"
    assert "Multiple SKILL.md files found" in result.message


def test_mcp_auto_configurator_registers_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        def __init__(self, server):
            self.server = server

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def list_tools(self):
            return [
                McpToolSpec(
                    server_name=self.server.name,
                    name="echo",
                    description="Echo",
                    input_schema={"type": "object"},
                )
            ]

    monkeypatch.setattr(mcp_manager_module, "open_mcp_client", FakeClient)
    manager = McpManager(McpConfigStore(config_path=tmp_path / "mcp.json"))
    registry = ToolRegistry()

    result = McpAutoConfigurator(manager, registry).configure(
        {"server": "docs", "url": "example.com/mcp"}
    )

    assert result.success is True
    assert manager.get("docs").url == "https://example.com/mcp"
    assert registry.get("mcp__docs__echo") is not None
    assert "Tools registered: 1" in result.message


def test_mcp_auto_configurator_reports_test_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingClient:
        def __init__(self, server):
            self.server = server

        def __enter__(self):
            raise RuntimeError("server unavailable")

        def __exit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(mcp_manager_module, "open_mcp_client", FailingClient)
    manager = McpManager(McpConfigStore(config_path=tmp_path / "mcp.json"))

    result = McpAutoConfigurator(manager).configure("mcp add files stdio fake-command --flag")

    assert result.success is False
    assert "Test: Failed" in result.message
    assert "server unavailable" in result.message
    assert manager.get("files").command == "fake-command"


def test_mcp_normalization_handles_command_and_aliases() -> None:
    command = normalize_mcp_config("/mcp add files stdio npx -y pkg F:\\Work")
    assert command["name"] == "files"
    assert command["transport"] == "stdio"
    assert command["command"] == "npx"
    assert command["args"] == ["-y", "pkg", "F:\\Work"]

    sse = normalize_mcp_config({"id": "events", "endpoint": "example.com/sse"})
    assert sse["transport"] == "sse"
    assert sse["url"] == "https://example.com/sse"


@pytest.mark.asyncio
async def test_auto_config_plan_mode_does_not_write_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app = CoomiApp()
    app._plan_mode = True
    app._config_mgr = ConfigManager()

    result = await app._handle_auto_config_input(
        '{"id":"plan-only","api_key":"sk-plan","model":"gpt-4.1"}'
    )

    assert "Plan Mode is active" in result
    assert app._config_mgr.get_provider("plan-only") is None


@pytest.mark.asyncio
async def test_auto_config_ask_approval_deny_does_not_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app = CoomiApp()
    app._config_mgr = ConfigManager()

    async def deny(_questions):
        return {0: {"option": "deny"}}

    app._handle_ask_questions = deny

    result = await app._handle_auto_config_input(
        '{"id":"denied","api_key":"sk-denied","model":"gpt-4.1"}'
    )

    assert "Permission denied" in result
    assert app._config_mgr.get_provider("denied") is None


@pytest.mark.asyncio
async def test_auto_config_ask_approval_allow_writes_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app = CoomiApp()
    app._config_mgr = ConfigManager()

    async def allow(questions):
        question = questions[0]
        assert question["options"][0]["description"].startswith("Coomi will perform")
        assert "sk-****oved" in question["question"]
        return {0: {"option": "allow"}}

    app._handle_ask_questions = allow

    result = await app._handle_auto_config_input(
        '{"id":"approved","api_key":"sk-approved","model":"gpt-4.1"}'
    )

    assert "Added and activated" in result
    assert app._config_mgr.get_provider("approved") is not None


@pytest.mark.asyncio
async def test_auto_config_full_access_executes_without_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app = CoomiApp()
    app._config_mgr = ConfigManager()
    app._permission_system.set_mode(PermissionMode.FULL_ACCESS)

    async def fail_if_called(_questions):
        raise AssertionError("Full Access should not ask for approval")

    app._handle_ask_questions = fail_if_called

    result = await app._handle_auto_config_input(
        '{"id":"full","api_key":"sk-full","model":"gpt-4.1"}'
    )

    assert "Added and activated" in result
    assert app._config_mgr.get_provider("full") is not None
