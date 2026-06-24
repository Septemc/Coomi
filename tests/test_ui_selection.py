from __future__ import annotations

from rich.segment import Segment
from rich.style import Style
from textual.strip import Strip

from coomi.ui.widgets.selectable_rich_log import SelectableRichLog, _slice_text_cells
from coomi.ui.widgets.plan_panel import PlanPanel


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
