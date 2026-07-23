from __future__ import annotations

from unittest.mock import patch

import pytest
from rich.cells import cell_len
from rich.console import Console
from rich.segment import Segment
from rich.style import Style
from textual.app import App, ComposeResult
from textual.strip import Strip
from textual.widgets.text_area import Selection

from coomi.services.update_check import (
    UpdateCheckResult,
    build_update_prompt_suffix,
    is_newer_version,
)
from coomi.ui.screens.main_screen import MainScreen
from coomi.ui.screens.main_screen import PROMPT_PLACEHOLDER
from coomi.ui.screens.settings_screen import SettingsScreen
from coomi.ui.status_line import StatusLine
from coomi.ui.textual_app import CoomiApp, strip_sgr_mouse_reports
from coomi.ui.widgets.custom_header import CustomHeader, _middle_ellipsis
from coomi.ui.widgets.selectable_rich_log import SelectableRichLog, _slice_text_cells
from coomi.ui.widgets.plan_panel import PlanPanel
from coomi.ui.widgets.prompt_text_area import PromptTextArea
from coomi.ui.widgets.status_panel import StatusPanel
from coomi.ui.widgets.welcome_panel import (
    SESSION_ACTION_DELETE,
    SESSION_ACTION_SELECT,
    WelcomePanel,
)
from coomi.ui.terminal_capabilities import supports_modified_enter
from coomi.services.llm.config import ProviderConfig


class UiTestProvider:
    def __init__(self) -> None:
        self.model = "test-model"
        self.config = ProviderConfig(
            id="test",
            type="generic",
            display="Test Model",
            api_key="test-key",
            model=self.model,
        )

    def get_model_display_name(self) -> str:
        return "Test Model"


class RecordingLog:
    def __init__(self) -> None:
        self.entries: list[object] = []

    def write(self, value: object) -> None:
        self.entries.append(value)


def test_reasoning_flush_resets_each_model_phase(monkeypatch: pytest.MonkeyPatch):
    app = CoomiApp()
    log = RecordingLog()
    app._reasoning_visible = True
    app._full_reasoning = "Inspecting the file"
    app._reasoning_start_time = 100.0
    monkeypatch.setattr("coomi.ui.textual_app.time.time", lambda: 102.5)

    assert app._flush_reasoning(log) is True
    assert app._full_reasoning == ""
    assert app._reasoning_start_time == 0.0
    assert len(log.entries) == 1
    assert "Thinking (2.5s)" in str(log.entries[0])


@pytest.fixture(autouse=True)
def _use_test_provider(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "coomi.ui.textual_app.get_llm_provider",
        lambda provider_id=None: UiTestProvider(),
    )


class SelectionTestApp(App):
    CSS = "#log { width: 60; height: 8; padding: 0 1; }"

    def compose(self) -> ComposeResult:
        yield SelectableRichLog(id="log", markup=True, wrap=True, highlight=True)

    async def on_mount(self) -> None:
        self.query_one("#log", SelectableRichLog).write("hello world")


class MainScreenCopyTestApp(App):
    async def on_mount(self) -> None:
        self.push_screen(MainScreen(status_line=StatusLine()))


class HeaderClickTestApp(App):
    def __init__(self) -> None:
        super().__init__()
        self.home_clicks = 0
        self.settings_clicks = 0

    async def on_mount(self) -> None:
        self.push_screen(MainScreen(status_line=StatusLine()))

    def action_go_home(self) -> None:
        self.home_clicks += 1

    def action_open_settings(self) -> None:
        self.settings_clicks += 1


def test_slice_text_cells_handles_wide_characters():
    assert _slice_text_cells("A北京B", 1, 5) == "北京"
    assert _slice_text_cells("A北京B", 0, 2) == "A北"


def test_apply_highlight_uses_terminal_cell_offsets():
    widget = SelectableRichLog()
    strip = Strip([Segment("A北京B")])

    highlighted = widget._apply_highlight(strip, 1, 5, Style(bgcolor="red"))

    assert highlighted.cell_length == strip.cell_length
    highlighted_text = "".join(segment.text for segment in highlighted._segments)
    assert highlighted_text == "A北京B"
    highlighted_segments = [segment for segment in highlighted._segments if segment.style]
    assert any("北京" in segment.text for segment in highlighted_segments)


@pytest.mark.asyncio
async def test_selectable_rich_log_left_drag_starts_from_padding():
    app = SelectionTestApp()
    async with app.run_test(size=(80, 24)) as pilot:
        log = app.query_one("#log", SelectableRichLog)
        await pilot.pause()

        await pilot.mouse_down(log, offset=(0, 0), button=1)
        await pilot.hover(log, offset=(6, 0))
        await pilot.mouse_up(log, offset=(6, 0))
        await pilot.pause()

        assert log.has_selection()
        assert log.get_selected_text() == "hello"


@pytest.mark.asyncio
async def test_selectable_rich_log_left_drag_from_trailing_space():
    app = SelectionTestApp()
    async with app.run_test(size=(80, 24)) as pilot:
        log = app.query_one("#log", SelectableRichLog)
        await pilot.pause()

        await pilot.mouse_down(log, offset=(30, 0), button=1)
        await pilot.hover(log, offset=(7, 0))
        await pilot.mouse_up(log, offset=(7, 0))
        await pilot.pause()

        assert log.has_selection()
        assert log.get_selected_text() == "world"


@pytest.mark.asyncio
async def test_main_screen_copy_selected_prefers_prompt_selection():
    app = MainScreenCopyTestApp()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()

        prompt = app.screen.query_one("#prompt-input", PromptTextArea)
        prompt.text = "copy me"
        prompt.selection = Selection((0, 0), (0, 4))

        app.screen.action_copy_selected()

        assert app.clipboard == "copy"


@pytest.mark.asyncio
async def test_main_screen_copy_selected_uses_log_highlight():
    app = MainScreenCopyTestApp()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()

        app.screen.hide_welcome_panel()
        log = app.screen.query_one("#message-log", SelectableRichLog)
        log.write("hello world")
        await pilot.pause()

        await pilot.mouse_down(log, offset=(0, 0), button=1)
        await pilot.hover(log, offset=(5, 0))
        await pilot.mouse_up(log, offset=(5, 0))
        await pilot.pause()

        app.screen.action_copy_selected()

        assert app.clipboard == "hello"


@pytest.mark.asyncio
async def test_coomi_app_ctrl_c_copies_log_highlight():
    app = CoomiApp()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        app.screen.hide_welcome_panel()
        log = app.screen.query_one("#message-log", SelectableRichLog)
        log.write("hello world")
        await pilot.pause()

        await pilot.mouse_down(log, offset=(0, 0), button=1)
        await pilot.hover(log, offset=(6, 0))
        await pilot.mouse_up(log, offset=(6, 0))
        await pilot.pause()
        await pilot.press("ctrl+c")
        await pilot.pause()

        assert app.clipboard == "hello"


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["shift+enter", "ctrl+enter", "ctrl+j"])
async def test_coomi_app_modified_enter_inserts_newline_without_submitting(key: str):
    app = CoomiApp()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        prompt = app.screen.query_one("#prompt-input", PromptTextArea)
        prompt.text = "first line"
        prompt.move_cursor(prompt.document.end)
        prompt.focus()

        await pilot.press(key)
        await pilot.pause()

        assert prompt.text == "first line\n"
        assert app._agent_running is False


def test_modified_enter_detection_is_conservative_and_overridable():
    assert supports_modified_enter({}) is False
    assert supports_modified_enter({"WT_SESSION": "demo"}) is False
    assert supports_modified_enter({"KITTY_WINDOW_ID": "1"}) is True
    assert supports_modified_enter({"TERM_PROGRAM": "ghostty"}) is True
    assert supports_modified_enter({"COOMI_MODIFIED_ENTER": "1"}) is True
    assert supports_modified_enter({"COOMI_MODIFIED_ENTER": "0", "KITTY_WINDOW_ID": "1"}) is False


def test_prompt_placeholder_advertises_reliable_multiline_fallback():
    assert "Enter 发送" in PROMPT_PLACEHOLDER
    assert "Shift+Enter / Ctrl+J 换行" in PROMPT_PLACEHOLDER


def test_sgr_mouse_reports_are_removed_from_prompt_text():
    leaked = "before[<35;68;33M[<35;75;30Mafter"
    assert strip_sgr_mouse_reports(leaked) == "beforeafter"
    assert strip_sgr_mouse_reports("normal [text]; keep it") == "normal [text]; keep it"


@pytest.mark.asyncio
async def test_coomi_app_ctrl_c_without_selection_never_exits():
    app = CoomiApp()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        with patch.object(app, "exit") as exit_mock:
            await pilot.press("ctrl+c")
            await pilot.pause()

        exit_mock.assert_not_called()


@pytest.mark.asyncio
async def test_main_screen_focuses_prompt_on_mount():
    app = MainScreenCopyTestApp()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()

        prompt = app.screen.query_one("#prompt-input", PromptTextArea)
        await pilot.press("h", "i")
        await pilot.pause()

        assert app.focused is prompt
        assert prompt.text == "hi"
        assert prompt.placeholder == PROMPT_PLACEHOLDER


def test_update_prompt_suffix_mentions_current_and_latest_versions():
    suffix = build_update_prompt_suffix(
        UpdateCheckResult(
            current_version="0.1.12",
            latest_version="0.1.13",
            update_available=True,
        )
    )

    assert suffix == "当前使用的是0.1.12，建议通过“pip install -U coomi-agent”更新到0.1.13"
    assert is_newer_version("0.1.13", "0.1.12")
    assert not is_newer_version("0.1.12", "0.1.12")


@pytest.mark.asyncio
async def test_coomi_app_routes_text_to_prompt_when_focus_drifts():
    app = CoomiApp()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        prompt = app.screen.query_one("#prompt-input", PromptTextArea)
        log = app.screen.query_one("#message-log", SelectableRichLog)
        log.display = True
        log.focus()

        await pilot.press("a", "space", "b")
        await pilot.pause()

        assert app.focused is prompt
        assert prompt.text == "a b"


@pytest.mark.asyncio
async def test_custom_header_home_and_setting_are_clickable():
    app = HeaderClickTestApp()
    app._cwd = "测试使用001"
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()

        header = app.screen.query_one(CustomHeader)
        rendered = header.render()
        home_x = cell_len(rendered.plain[: rendered.plain.index("Home")])
        setting_x = cell_len(rendered.plain[: rendered.plain.index("Setting")])

        assert rendered.cell_len == header.size.width
        assert home_x == header._home_start
        assert setting_x == header._setting_start

        await pilot.click(header, offset=(home_x, 0))
        await pilot.click(header, offset=(setting_x, 0))
        await pilot.pause()

        assert app.home_clicks == 1
        assert app.settings_clicks == 1


def test_custom_header_middle_ellipsis_uses_terminal_cell_width():
    rendered = _middle_ellipsis(r"F:\_WorkSpace\_Temp\测试使用001", 24)

    assert "..." in rendered
    assert cell_len(rendered) <= 24


def test_plan_panel_collects_multi_select_answers():
    panel = PlanPanel(
        [
            {
                "header": "方向",
                "question": "你想重点学习哪些方向？",
                "multiSelect": True,
                "options": [
                    {"label": "CV", "value": "cv", "description": "图像方向"},
                    {"label": "NLP", "value": "nlp", "description": "文本方向"},
                ],
            }
        ]
    )

    panel.toggle_current_option()
    panel.move_down()
    panel.toggle_current_option()

    answer = panel.get_all_answers()[0]
    assert answer["option"] == ["cv", "nlp"]
    assert answer["labels"] == ["CV", "NLP"]
    assert answer["label"] == "CV, NLP"


def test_plan_panel_renders_option_summary_before_detailed_description():
    panel = PlanPanel(
        [
            {
                "header": "Mode",
                "question": "Which implementation path should Coomi use?",
                "options": [
                    {
                        "label": "Conservative",
                        "summary": "Smallest change",
                        "description": (
                            "Use the existing code path and only adjust the narrow behavior. "
                            "This keeps risk low while still addressing the reported problem."
                        ),
                    },
                    {
                        "label": "Refactor",
                        "summary": "Broader cleanup",
                        "description": (
                            "Rework the surrounding structure so future features have a clearer place. "
                            "This takes longer and should be chosen when the current shape is blocking work."
                        ),
                    },
                ],
            }
        ]
    )
    console = Console(record=True, width=100)

    console.print(panel.render())
    rendered = console.export_text()

    assert "Conservative - Smallest change" in rendered
    assert "Use the existing code path and only adjust the narrow behavior." in rendered


def test_plan_panel_derives_summary_for_terse_question_options():
    panel = PlanPanel(
        [
            {
                "header": "Mode",
                "question": "Which path should Coomi use?",
                "options": [
                    {
                        "label": "Auto",
                        "description": (
                            "Automatically repair the installed copy. This keeps the source untouched "
                            "while making the skill usable immediately."
                        ),
                    },
                    {
                        "label": "Manual",
                        "description": (
                            "Stop and ask for edits. This gives the user full control before Coomi changes anything."
                        ),
                    },
                ],
            }
        ]
    )
    console = Console(record=True, width=100)

    console.print(panel.render())
    rendered = console.export_text()

    assert "Auto - Automatically repair the installed copy" in rendered
    assert "This keeps the source untouched" in rendered
    assert "skill usable immediately." in rendered


def test_welcome_panel_mentions_direct_paste_auto_configuration():
    panel = WelcomePanel()
    panel.set_context("demo-model", 12)
    console = Console(record=True, width=120)

    console.print(panel._render_bubble(60))
    rendered = console.export_text()

    assert "Provider JSON" in rendered
    assert "Skill 链接/本地路径" in rendered
    assert "MCP JSON/URL/stdio" in rendered
    assert "自动配置、测试并注册工具" in rendered


def test_welcome_panel_uses_left_right_for_select_and_delete(tmp_path):
    from datetime import datetime

    from coomi.services.session_history import SessionHistoryRecord

    panel = WelcomePanel()
    panel.set_context(
        "demo-model",
        12,
        [
            SessionHistoryRecord(
                path=tmp_path / "coomi-demo.jsonl",
                session_id="demo",
                title="Demo session",
                created_at=datetime.now(),
                updated_at=datetime.now(),
                message_count=2,
            )
        ],
    )

    assert panel.selected_session_action == SESSION_ACTION_SELECT
    panel.move_session_action(1)
    assert panel.selected_session_action == SESSION_ACTION_DELETE
    panel.move_session_action(-1)
    assert panel.selected_session_action == SESSION_ACTION_SELECT


def test_status_panel_shows_persistent_mode_badge_on_top_row():
    status = StatusPanel(StatusLine())
    status.set_plan_mode(True)
    status.set_executing()
    console = Console(record=True, width=120)

    console.print(status.render())
    rendered = console.export_text()

    assert "PLAN MODE" in rendered.splitlines()[0]
    status.set_idle()
    assert status.special_mode == "plan"

    status.set_plan_mode(False)
    status.set_loop_mode(True, total_steps=3)
    status.set_loop_progress(2, 3)
    loop_console = Console(record=True, width=120)
    loop_console.print(status.render())
    assert "LOOP MODE 2/3" in loop_console.export_text().splitlines()[0]


def test_settings_screen_guide_explains_llm_skill_and_mcp_usage():
    screen = SettingsScreen()

    llm_guide = screen._render_guide()
    assert "providers.json" in llm_guide
    assert "tool_protocol" in llm_guide
    assert "Provider JSON" in llm_guide
    assert "输入框直接配置" in llm_guide
    assert "自动添加、激活并刷新 LLM" in llm_guide
    assert "保存后配置会自动生效" in llm_guide

    screen._selected = 1
    skill_guide = screen._render_guide()
    assert "/skill install" in skill_guide
    assert "SKILL.md" in skill_guide
    assert "GitHub" in skill_guide
    assert "Skill 链接/路径" in skill_guide

    screen._selected = 2
    mcp_guide = screen._render_guide()
    assert "/mcp add" in mcp_guide
    assert "stdio" in mcp_guide
    assert "mcp__server__tool" in mcp_guide
    assert "MCP JSON/URL/stdio" in mcp_guide
