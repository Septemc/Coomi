from __future__ import annotations

import pytest
from rich.console import Console
from rich.segment import Segment
from rich.style import Style
from textual.app import App, ComposeResult
from textual.strip import Strip
from textual.widgets.text_area import Selection

from coomi.ui.screens.main_screen import MainScreen
from coomi.ui.status_line import StatusLine
from coomi.ui.textual_app import CoomiApp
from coomi.ui.widgets.custom_header import CustomHeader
from coomi.ui.widgets.selectable_rich_log import SelectableRichLog, _slice_text_cells
from coomi.ui.widgets.plan_panel import PlanPanel
from coomi.ui.widgets.prompt_text_area import PromptTextArea


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
async def test_main_screen_focuses_prompt_on_mount():
    app = MainScreenCopyTestApp()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()

        prompt = app.screen.query_one("#prompt-input", PromptTextArea)
        await pilot.press("h", "i")
        await pilot.pause()

        assert app.focused is prompt
        assert prompt.text == "hi"


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
