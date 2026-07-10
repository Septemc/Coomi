from __future__ import annotations

from unittest.mock import patch

import pytest
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
from coomi.ui.textual_app import CoomiApp
from coomi.ui.widgets.custom_header import CustomHeader
from coomi.ui.widgets.selectable_rich_log import SelectableRichLog, _slice_text_cells
from coomi.ui.widgets.plan_panel import PlanPanel
from coomi.ui.widgets.prompt_text_area import PromptTextArea
from coomi.ui.widgets.welcome_panel import WelcomePanel


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
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()

        header = app.screen.query_one(CustomHeader)
        await pilot.click(header, offset=(header._home_start, 0))
        await pilot.click(header, offset=(header._setting_start, 0))
        await pilot.pause()

        assert app.home_clicks == 1
        assert app.settings_clicks == 1


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
