from __future__ import annotations

import json
import os
from importlib.resources import files
from pathlib import Path

import pytest
from textual.app import App
from textual.widgets import Input, OptionList, Static

from coomi.catalogs import (
    CatalogInput,
    McpCatalogEntry,
    SkillCatalogEntry,
    load_mcp_catalog,
    load_skill_catalog,
)
from coomi.services.mcp.models import McpServerConfig
from coomi.services.skills import installer as skill_installer
from coomi.services.skills.config import SkillConfig
from coomi.services.skills.manager import SkillManager
from coomi.services.skills.models import SkillRecord, SkillUpdateStatus
from coomi.ui.screens.mcp_marketplace_screen import (
    McpInstallConfigScreen,
    McpMarketplaceScreen,
)
from coomi.ui.screens.provider_edit_screen import FIELD_HELP, ProviderEditScreen
from coomi.ui.screens.skill_marketplace_screen import SkillMarketplaceScreen


class ScreenHost(App[None]):
    CSS_PATH = str(Path("coomi-py/coomi/ui/tcss/coomi.tcss").resolve())

    def __init__(self, screen) -> None:
        super().__init__()
        self._screen_to_push = screen

    async def on_mount(self) -> None:
        self.push_screen(self._screen_to_push)


class FakeSkillManager:
    def __init__(self) -> None:
        self.records: dict[str, SkillRecord] = {}
        self.install_calls: list[str] = []
        self.check_calls: list[str] = []
        self.update_calls: list[str] = []
        self.remove_calls: list[str] = []

    def list(self) -> list[SkillRecord]:
        return list(self.records.values())

    def install(self, source: str, name: str, enabled: bool = True) -> SkillRecord:
        self.install_calls.append(name)
        record = SkillRecord(
            name=name,
            path=f"/tmp/{name}",
            enabled=enabled,
            source_type="github",
            source=source,
            commit="old",
        )
        self.records[name] = record
        return record

    def check_update(self, name: str) -> SkillUpdateStatus:
        self.check_calls.append(name)
        return SkillUpdateStatus(
            name=name,
            source_type="github",
            current_commit="old",
            remote_commit="new",
            update_available=True,
            message="发现新版本。",
        )

    def update(self, name: str) -> SkillRecord:
        self.update_calls.append(name)
        self.records[name].commit = "new"
        return self.records[name]

    def remove(self, name: str) -> SkillRecord:
        self.remove_calls.append(name)
        return self.records.pop(name)


class FakeMcpManager:
    def __init__(self) -> None:
        self.servers: dict[str, McpServerConfig] = {}
        self.add_calls: list[str] = []
        self.test_calls: list[str] = []
        self.remove_calls: list[str] = []

    def list(self) -> list[McpServerConfig]:
        return list(self.servers.values())

    def add_catalog_config(self, rendered: dict) -> McpServerConfig:
        name = rendered["name"]
        self.add_calls.append(name)
        server = McpServerConfig(
            name=name,
            transport=rendered["transport"],
            command=rendered["command"],
            args=rendered["args"],
            env=rendered["env"],
            source_type="catalog",
            catalog_id=rendered["catalog_id"],
            catalog_signature=rendered["catalog_signature"],
        )
        self.servers[name] = server
        return server

    def test(self, name: str) -> tuple[bool, str]:
        self.test_calls.append(name)
        server = self.servers[name]
        server.tools_count = 3
        server.last_checked_at = "2026-07-11T00:00:00Z"
        server.last_error = ""
        return True, "Connected. Tools discovered: 3"

    def remove(self, name: str) -> McpServerConfig:
        self.remove_calls.append(name)
        return self.servers.pop(name)


class FakeProviderConfigManager:
    def __init__(self) -> None:
        self.data = {"active": "", "providers": {}}

    def add_provider(self, provider) -> None:
        self.data["providers"][provider.id] = provider.to_dict()

    def remove_provider(self, provider_id: str) -> bool:
        return self.data["providers"].pop(provider_id, None) is not None

    def set_active(self, provider_id: str) -> bool:
        self.data["active"] = provider_id
        return True


def _skill_entry(entry_id: str = "demo") -> SkillCatalogEntry:
    return SkillCatalogEntry(
        id=entry_id,
        name="Demo Skill",
        description="Demo description",
        source_url="https://github.com/example/demo/tree/main/skills/demo",
        repository="example/demo",
        ref="main",
        subdir="skills/demo",
        homepage="https://github.com/example/demo",
        author="Example",
        verified=True,
        license="MIT",
    )


def _mcp_entry(*, inputs: tuple[CatalogInput, ...] = ()) -> McpCatalogEntry:
    return McpCatalogEntry(
        id="demo-mcp",
        name="Demo MCP",
        description="Demo server",
        homepage="https://github.com/example/mcp",
        transport="stdio",
        command="npx",
        args=("-y", "demo-mcp", "{{workspace}}") if inputs else ("-y", "demo-mcp"),
        required_parameters=inputs,
        verified=True,
        license="MIT",
    )


def test_builtin_skill_catalog_has_twenty_unique_valid_entries():
    entries = load_skill_catalog()

    assert len(entries) == 20
    assert len({entry.id for entry in entries}) == 20
    for entry in entries:
        assert entry.verified is True
        assert entry.source_url.startswith("https://github.com/")
        assert entry.repository.count("/") == 1
        assert entry.ref
        assert entry.subdir
        assert entry.license

    raw = json.loads(files("coomi.catalogs").joinpath("skills.json").read_text("utf-8"))
    required = {
        "id",
        "name",
        "description",
        "source_url",
        "repository",
        "ref",
        "subdir",
        "homepage",
        "author",
        "tags",
        "requirements",
        "verified",
        "license",
        "install_notes",
    }
    assert all(required <= item.keys() for item in raw["entries"])


def test_builtin_mcp_catalog_has_required_categories_and_no_secrets():
    entries = load_mcp_catalog()
    ids = {entry.id for entry in entries}

    assert {
        "filesystem",
        "git",
        "github",
        "fetch",
        "memory",
        "sequential-thinking",
        "playwright",
        "postgresql",
        "sqlite",
        "brave-search",
        "slack",
        "notion",
    } <= ids
    for entry in entries:
        assert entry.verified is True
        assert entry.transport in {"stdio", "http", "sse"}
        assert entry.homepage.startswith("https://")
        for value in entry.env.values():
            assert "{{" in value or value == ""

    raw = json.loads(files("coomi.catalogs").joinpath("mcp.json").read_text("utf-8"))
    required = {
        "id",
        "name",
        "description",
        "homepage",
        "transport",
        "command",
        "args",
        "url_template",
        "required_env",
        "required_parameters",
        "runtime_requirements",
        "platforms",
        "official",
        "verified",
        "install_notes",
    }
    assert all(required <= item.keys() for item in raw["entries"])


def test_mcp_catalog_render_requires_values_and_signature_excludes_secret():
    entry = next(item for item in load_mcp_catalog() if item.id == "github")

    with pytest.raises(ValueError, match="GitHub Personal Access Token"):
        entry.render({})

    rendered = entry.render({"github_token": "github_pat_secret"})
    assert rendered["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"] == "github_pat_secret"
    assert "github_pat_secret" not in entry.signature
    if os.name == "nt":
        npx_entry = next(item for item in load_mcp_catalog() if item.id == "memory")
        windows_rendered = npx_entry.render({})
        assert windows_rendered["command"].lower().endswith(("cmd", "cmd.exe"))
        assert windows_rendered["args"][:4] == ["/d", "/s", "/c", "npx"]

    filesystem = next(item for item in load_mcp_catalog() if item.id == "filesystem")
    with pytest.raises(ValueError, match="不存在"):
        filesystem.render({"allowed_path": str(Path.cwd() / "missing-directory")})


def test_atomic_skill_copy_preserves_existing_install_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("# New", encoding="utf-8")
    destination = tmp_path / "skills" / "demo"
    destination.mkdir(parents=True)
    (destination / "SKILL.md").write_text("# Existing", encoding="utf-8")

    def fail_copy(*args, **kwargs):
        raise OSError("copy failed")

    monkeypatch.setattr(skill_installer.shutil, "copytree", fail_copy)

    with pytest.raises(OSError, match="copy failed"):
        skill_installer.copy_skill_tree(source, destination)

    assert (destination / "SKILL.md").read_text(encoding="utf-8") == "# Existing"


def test_skill_manager_check_update_reports_remote_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    manager = SkillManager(
        SkillConfig(config_path=tmp_path / "skills.json", skills_dir=tmp_path / "skills")
    )
    manager.config.put(
        SkillRecord(
            name="demo",
            path=str(tmp_path / "skills" / "demo"),
            source_type="github",
            source="https://github.com/example/demo/tree/main/skills/demo",
            branch="main",
            commit="old",
        )
    )
    monkeypatch.setattr(
        "coomi.services.skills.manager.resolve_github_commit",
        lambda _source: ("new", False),
    )

    status = manager.check_update("demo")

    assert status.update_available is True
    assert status.current_commit == "old"
    assert status.remote_commit == "new"


@pytest.mark.asyncio
async def test_skill_marketplace_enter_install_check_update_and_delete():
    manager = FakeSkillManager()
    screen = SkillMarketplaceScreen(manager, catalog=[_skill_entry()])
    app = ScreenHost(screen)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        options = screen.query_one("#skill-marketplace-list", OptionList)
        screen.action_move_up()
        assert options.highlighted == 0

        await pilot.press("enter")
        await pilot.pause()
        assert manager.install_calls == ["demo"]

        await pilot.press("enter")
        await pilot.pause()
        assert manager.check_calls == ["demo"]
        assert manager.update_calls == []

        await pilot.press("enter")
        await pilot.pause()
        assert manager.update_calls == ["demo"]

        await pilot.press("delete", "delete")
        await pilot.pause()
        assert manager.remove_calls == ["demo"]


@pytest.mark.asyncio
async def test_skill_marketplace_plan_mode_is_read_only():
    manager = FakeSkillManager()
    screen = SkillMarketplaceScreen(manager, catalog=[_skill_entry()], plan_mode=True)
    app = ScreenHost(screen)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert manager.install_calls == []
        assert "Plan Mode" in str(screen.query_one("#skill-marketplace-detail", Static).render())


@pytest.mark.asyncio
async def test_mcp_marketplace_enter_configures_tests_and_delete_removes():
    manager = FakeMcpManager()
    registry_refreshes: list[bool] = []
    screen = McpMarketplaceScreen(
        manager,
        catalog=[_mcp_entry()],
        on_registry_refresh=lambda: registry_refreshes.append(True),
    )
    app = ScreenHost(screen)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert manager.add_calls == ["demo-mcp"]
        assert manager.test_calls == ["demo-mcp"]

        await pilot.press("enter")
        await pilot.pause()
        assert manager.test_calls == ["demo-mcp", "demo-mcp"]

        await pilot.press("delete", "delete")
        await pilot.pause()
        assert manager.remove_calls == ["demo-mcp"]
        assert len(registry_refreshes) == 3


@pytest.mark.asyncio
async def test_mcp_install_form_blocks_missing_required_value():
    entry = _mcp_entry(
        inputs=(
            CatalogInput(
                key="workspace",
                label="Workspace",
                description="Required path",
                required=True,
            ),
        )
    )
    screen = McpInstallConfigScreen(entry)
    app = ScreenHost(screen)

    async with app.run_test(size=(90, 28)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        error = screen.query_one("#mcp-install-error", Static)
        assert "Workspace" in str(error.render())


@pytest.mark.asyncio
async def test_mcp_marketplace_plan_mode_is_read_only():
    manager = FakeMcpManager()
    screen = McpMarketplaceScreen(manager, catalog=[_mcp_entry()], plan_mode=True)
    app = ScreenHost(screen)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert manager.add_calls == []
        assert "Plan Mode" in str(screen.query_one("#mcp-marketplace-detail", Static).render())


@pytest.mark.asyncio
async def test_provider_edit_context_help_and_inline_validation():
    screen = ProviderEditScreen(FakeProviderConfigManager())
    app = ScreenHost(screen)

    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        api_key = screen.query_one("#field-api_key", Input)
        api_key.focus()
        await pilot.pause()

        help_panel = screen.query_one("#provider-field-help", Static)
        assert "~/.coomi/config/providers.json" in str(help_panel.render())
        assert api_key.password is True

        screen.action_save()
        error = screen.query_one("#provider-edit-error", Static)
        assert "Provider ID" in str(error.render())


@pytest.mark.asyncio
async def test_marketplace_screens_mount_in_narrow_terminal():
    skill_screen = SkillMarketplaceScreen(FakeSkillManager(), catalog=[_skill_entry()])
    async with ScreenHost(skill_screen).run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        assert skill_screen.query_one(OptionList).option_count == 1

    mcp_screen = McpMarketplaceScreen(FakeMcpManager(), catalog=[_mcp_entry()])
    async with ScreenHost(mcp_screen).run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        assert mcp_screen.query_one(OptionList).option_count == 1


def test_provider_help_covers_every_field_and_tool_protocol_modes():
    assert {"id", "type", "tool_protocol", "display", "api_key", "base_url", "model", "fast_model", "preset"} <= FIELD_HELP.keys()
    protocol_help = FIELD_HELP["tool_protocol"]
    for mode in ("auto", "native", "structured", "mimo", "disabled"):
        assert mode in protocol_help


def test_readme_uses_absolute_pypi_safe_image_and_documents_marketplace():
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "https://raw.githubusercontent.com/Septemc/Coomi/main/image/README/" in readme
    assert "](image/README/" not in readme
    assert "Skill 与 MCP 管理中心" in readme
    assert "`Delete`" in readme


@pytest.mark.asyncio
async def test_skill_marketplace_supports_left_right_action_focus():
    manager = FakeSkillManager()
    manager.records["demo"] = SkillRecord(name="demo", path="/tmp/demo", enabled=True)
    screen = SkillMarketplaceScreen(manager, catalog=[_skill_entry()])
    async with ScreenHost(screen).run_test(size=(100, 32)) as pilot:
        await pilot.press("right")
        await pilot.pause()
        assert screen._action_index == 1
        await pilot.press("left")
        assert screen._action_index == 0
