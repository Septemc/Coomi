from __future__ import annotations

from pathlib import Path

import pytest

from coomi.engine.session import build_system_prompt
from coomi.services.mcp.config import McpConfigStore
from coomi.services.mcp.manager import McpManager
from coomi.services.mcp.models import McpServerConfig, McpToolSpec
from coomi.services.mcp import manager as mcp_manager_module
from coomi.services.mcp import tool_adapter as mcp_adapter_module
from coomi.services.skills.config import SkillConfig
from coomi.services.skills.installer import parse_github_url
from coomi.services.skills.manager import SkillManager
from coomi.tools.registry import ToolRegistry
from coomi.ui.textual_app import CoomiApp


def _make_skill(path: Path, name: str = "Demo Skill") -> Path:
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(
        f"# {name}\nDescription: helps with demo work.\n\nUse this skill carefully.",
        encoding="utf-8",
    )
    return path


def test_skill_local_install_enable_disable_remove(tmp_path: Path) -> None:
    source = _make_skill(tmp_path / "source")
    config = SkillConfig(
        config_path=tmp_path / "config" / "skills.json",
        skills_dir=tmp_path / "skills",
    )
    manager = SkillManager(config)

    record = manager.install(str(source))

    assert record.name == "Demo-Skill"
    assert record.enabled is True
    assert Path(record.path, "SKILL.md").exists()
    assert config.get(record.name).source_type == "local"

    manager.enable(record.name, enabled=False)
    assert manager.get(record.name).enabled is False

    manager.enable(record.name, enabled=True)
    assert manager.get(record.name).enabled is True

    removed = manager.remove(record.name)
    assert removed.name == record.name
    assert manager.get(record.name) is None
    assert not Path(record.path).exists()


@pytest.mark.asyncio
async def test_enabled_skills_are_progressively_added_to_prompt(tmp_path: Path) -> None:
    alpha = _make_skill(tmp_path / "alpha", "Alpha")
    beta = _make_skill(tmp_path / "beta", "Beta")
    config = SkillConfig(
        config_path=tmp_path / "config" / "skills.json",
        skills_dir=tmp_path / "skills",
    )
    manager = SkillManager(config)
    manager.install(str(alpha), name="alpha")
    manager.install(str(beta), name="beta")
    manager.enable("beta", enabled=False)

    index_prompt = await build_system_prompt(skill_manager=manager, current_context="hello")
    assert "$alpha" in index_prompt
    assert "$beta" not in index_prompt
    assert "Use this skill carefully." not in index_prompt

    loaded_prompt = await build_system_prompt(skill_manager=manager, current_context="use $alpha")
    assert "## Loaded Skill Instructions" in loaded_prompt
    assert "Use this skill carefully." in loaded_prompt


def test_github_skill_url_supports_ref_and_subdir() -> None:
    source = parse_github_url("https://github.com/org/repo/tree/main/skills/python?ref=dev")

    assert source.clone_url == "https://github.com/org/repo.git"
    assert source.ref == "dev"
    assert source.subdir == "skills/python"


def test_mcp_config_manager_add_enable_remove(tmp_path: Path) -> None:
    store = McpConfigStore(config_path=tmp_path / "mcp_servers.json")
    manager = McpManager(store)

    server = manager.add_stdio("demo", "python", args=["server.py"])
    assert server.name == "demo"
    assert store.get("demo").command == "python"

    http_server = manager.add_http("remote", "https://example.com/mcp")
    assert http_server.transport == "http"
    assert store.get("remote").url == "https://example.com/mcp"

    sse_server = manager.add_sse("events", "https://example.com/sse")
    assert sse_server.transport == "sse"
    assert store.get("events").url == "https://example.com/sse"

    manager.enable("demo", enabled=False)
    assert store.get("demo").enabled is False

    removed = manager.remove("demo")
    assert removed.name == "demo"
    assert store.get("demo") is None


def test_mcp_manager_registers_tool_adapters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
                    description="Echo text",
                    input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
                )
            ]

    monkeypatch.setattr(mcp_manager_module, "open_mcp_client", FakeClient)
    store = McpConfigStore(config_path=tmp_path / "mcp_servers.json")
    manager = McpManager(store)
    manager.add_stdio("demo", "fake")
    registry = ToolRegistry()

    registered = manager.register_enabled_tools(registry)

    assert registered == ["mcp__demo__echo"]
    assert registry.get("mcp__demo__echo") is not None

    registry.unregister_prefix("mcp__demo__")
    assert registry.get("mcp__demo__echo") is None


def test_mcp_tool_adapter_calls_remote_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        def __init__(self, server):
            self.server = server

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def call_tool(self, name, arguments):
            return {"content": [{"type": "text", "text": f"{name}:{arguments['text']}"}]}

    monkeypatch.setattr(mcp_adapter_module, "open_mcp_client", FakeClient)
    server = McpServerConfig(name="demo", command="fake")
    spec = McpToolSpec(server_name="demo", name="echo", input_schema={"type": "object"})
    tool = mcp_adapter_module.McpToolAdapter(server, spec)

    result = tool.run({"text": "hello"})

    assert result.success is True
    assert result.output == "echo:hello"


@pytest.mark.asyncio
async def test_plan_mode_blocks_skill_and_mcp_mutations(tmp_path: Path) -> None:
    app = CoomiApp()
    app._plan_mode = True
    app._skill_manager = SkillManager(
        SkillConfig(config_path=tmp_path / "skills.json", skills_dir=tmp_path / "skills")
    )
    app._mcp_manager = McpManager(McpConfigStore(config_path=tmp_path / "mcp.json"))

    skill_result = await app._handle_skill_command("install ./somewhere")
    mcp_result = await app._handle_mcp_command("add demo stdio fake")

    assert "Plan Mode is active" in skill_result
    assert "Plan Mode is active" in mcp_result
