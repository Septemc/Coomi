from __future__ import annotations

from rich.segment import Segment
from rich.style import Style
from textual.strip import Strip

from coomi.ui.widgets.selectable_rich_log import SelectableRichLog, _slice_text_cells


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
