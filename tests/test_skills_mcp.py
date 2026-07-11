from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from coomi.engine.session import SessionManager, build_system_prompt
from coomi.services.session_history import append_session_state, load_session_from_jsonl
from coomi.services.mcp.config import McpConfigStore
from coomi.services.mcp.client import StdioMcpClient
from coomi.services.mcp.manager import McpManager
from coomi.services.mcp.models import McpServerConfig, McpToolSpec
from coomi.services.mcp import manager as mcp_manager_module
from coomi.services.mcp import tool_adapter as mcp_adapter_module
from coomi.services.skills.config import SkillConfig
from coomi.services.skills.installer import parse_github_url, resolve_github_commit
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


def test_github_skill_short_commit_is_treated_as_fixed_version():
    commit, immutable = resolve_github_commit(
        "https://github.com/org/repo/tree/abcdef1/skills/python"
    )

    assert commit == "abcdef1"
    assert immutable is True


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

    catalog_server = manager.add_catalog_config(
        {
            "name": "catalog-demo",
            "transport": "stdio",
            "command": "demo",
            "args": ["--stdio"],
            "env": {},
            "headers": {},
            "catalog_id": "catalog-demo",
            "catalog_signature": "signature",
        }
    )
    persisted = store.get(catalog_server.name)
    assert persisted.source_type == "catalog"
    assert persisted.catalog_id == "catalog-demo"
    assert persisted.catalog_signature == "signature"

    manager.enable("demo", enabled=False)
    assert store.get("demo").enabled is False

    removed = manager.remove("demo")
    assert removed.name == "demo"
    assert store.get("demo") is None


def test_stdio_mcp_client_reads_json_lines_and_legacy_content_length():
    client = StdioMcpClient(McpServerConfig(name="demo", command="fake"))
    payload = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    body = json.dumps(payload).encode("utf-8")

    assert client._read_stream_message(io.BytesIO(body + b"\n")) == payload

    framed = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
    assert client._read_stream_message(io.BytesIO(framed)) == payload


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


def test_mcp_manager_redacts_configured_secrets_from_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingClient:
        def __init__(self, server):
            raise RuntimeError(f"connection rejected token={server.env['TOKEN']}")

    monkeypatch.setattr(mcp_manager_module, "open_mcp_client", FailingClient)
    store = McpConfigStore(config_path=tmp_path / "mcp.json")
    manager = McpManager(store)
    manager.add_stdio("secret-demo", "fake", env={"TOKEN": "top-secret"})

    ok, message = manager.test("secret-demo")

    assert ok is False
    assert "top-secret" not in message
    assert "***" in message
    assert "top-secret" not in store.get("secret-demo").last_error


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


def test_extension_command_parser_activates_skill_for_session(tmp_path: Path) -> None:
    source = _make_skill(tmp_path / "source", "Frontend Design")
    app = CoomiApp()
    app._skill_manager = SkillManager(
        SkillConfig(config_path=tmp_path / "skills.json", skills_dir=tmp_path / "installed")
    )
    record = app._skill_manager.install(str(source), name="frontend-design")
    app._session = SessionManager(history_dir=tmp_path).create_session()

    assert app._prepare_extension_request("/skill frontend-design") is None
    assert app._session.active_skills == []
    assert app._prepare_extension_request("/skill frontend-design 设计登录页") == "设计登录页"
    assert app._session.active_skills == [record.name]


def test_session_extension_state_round_trip(tmp_path: Path) -> None:
    session = SessionManager(history_dir=tmp_path).create_session()
    session.active_skills = ["frontend-design"]
    session.selected_mcps = ["memory"]
    append_session_state(session)

    loaded = load_session_from_jsonl(session.history_path)
    assert loaded.active_skills == ["frontend-design"]
    assert loaded.selected_mcps == ["memory"]


def test_complete_extension_commands_are_submittable() -> None:
    assert CoomiApp._is_complete_extension_command("/skill frontend-design 测试")
    assert CoomiApp._is_complete_extension_command("/mcp memory 查询信息 项目名称")
    assert not CoomiApp._is_complete_extension_command("/skill frontend-design")
    assert not CoomiApp._is_complete_extension_command("/mcp memory")


@pytest.mark.asyncio
async def test_online_extension_discovery_includes_coomi_install_guidance() -> None:
    prompt = await build_system_prompt(current_context="请联网检索适合 Coomi 的 skills 和 MCP")
    assert "Skill and MCP Discovery" in prompt
    assert "Coomi-compatible installation method" in prompt
    assert "/mcp add <name> stdio" in prompt
