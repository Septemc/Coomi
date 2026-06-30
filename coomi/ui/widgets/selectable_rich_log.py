"""SelectableRichLog — 支持鼠标拖拽选中 + 复制的 RichLog

绕过 Textual 的选择系统，自己管理选择状态。
支持部分选择、高亮保持、复制功能。
"""
from __future__ import annotations

from rich.cells import cell_len, get_character_cell_size
from rich.segment import Segment
from rich.style import Style
from textual import events
from textual.strip import Strip
from textual.widgets import RichLog


class SelectableRichLog(RichLog):
    """支持文本选择的 RichLog"""

    ALLOW_SELECT = True

    # 选择状态
    _selection_start: tuple[int, int] | None = None  # (line, cell_col)
    _selection_end: tuple[int, int] | None = None  # (line, cell_col)
    _is_selecting: bool = False

    # 高亮颜色：海蓝色
    HIGHLIGHT_STYLE = Style(bgcolor="#0077b6")

    @property
    def text(self) -> str:
        """从内部 Strip 行重建纯文本，供选择系统使用"""
        lines = []
        for strip in self.lines:
            lines.append(strip.text)
        return "\n".join(lines)

    def get_selection(self, selection) -> tuple[str, str] | None:
        """提取选中的文本"""
        text = self.text
        if not text:
            return None
        return selection.extract(text), "\n"

    def on_mouse_down(self, event: events.MouseDown) -> None:
        """鼠标按下：开始选择"""
        if event.button not in (1, 3):
            return
        pos = self._get_position(event, require_text=True)
        if pos is None:
            self.clear_selection()
            return
        line, col = pos
        self._selection_start = (line, col)
        self._selection_end = (line, col)
        self._is_selecting = True
        self.focus()
        self.capture_mouse()
        event.stop()
        self.refresh()  # 触发重绘

    def on_mouse_move(self, event: events.MouseMove) -> None:
        """鼠标移动：更新选择范围"""
        if self._is_selecting:
            pos = self._get_position(event, require_text=False)
            if pos is None:
                return
            line, col = pos
            self._selection_end = (line, col)
            event.stop()
            self.refresh()  # 触发重绘

    def on_mouse_up(self, event: events.MouseUp) -> None:
        """鼠标松开：保持选择状态"""
        self._is_selecting = False
        self.release_mouse()
        event.stop()
        # 保持选择状态，不清除

    def _get_position(
        self,
        event: events.MouseEvent,
        require_text: bool = False,
    ) -> tuple[int, int] | None:
        """将屏幕坐标转换为 (line, cell_col)。"""
        if require_text:
            offset = event.get_content_offset(self)
            if offset is None:
                offset = event.get_content_offset_capture(self)
        else:
            offset = event.get_content_offset_capture(self)
        if offset is None:
            return None

        scroll_x, scroll_y = self.scroll_offset
        line = scroll_y + offset.y

        if not self.lines:
            return None

        if line < 0 or line >= len(self.lines):
            line = max(0, min(line, len(self.lines) - 1))

        line_text = self.lines[line].text.rstrip()
        line_cell_len = cell_len(line_text)
        if line_cell_len == 0:
            return line, 0

        col = max(0, offset.x + scroll_x)
        return line, min(col, line_cell_len)

    def render_line(self, y: int) -> Strip:
        """重写行渲染，在选中区域叠加高亮"""
        strip = super().render_line(y)

        # 如果没有选择状态，直接返回
        if self._selection_start is None or self._selection_end is None:
            return strip

        # 计算当前行在 content 中的索引
        scroll_x, scroll_y = self.scroll_offset
        line_index = scroll_y + y

        # 获取选择范围
        start_line, start_col = self._selection_start
        end_line, end_col = self._selection_end

        # 确保 start 在 end 之前
        if (start_line, start_col) > (end_line, end_col):
            start_line, start_col, end_line, end_col = end_line, end_col, start_line, start_col

        # 检查当前行是否在选择范围内
        if line_index < start_line or line_index > end_line:
            return strip

        # 计算该行的选择范围
        visible_line_len = cell_len(strip.text.rstrip())
        if visible_line_len == 0:
            return strip
        full_line_len = cell_len(self.lines[line_index].text.rstrip()) if 0 <= line_index < len(self.lines) else visible_line_len

        if line_index == start_line and line_index == end_line:
            col_start, col_end = start_col, end_col
        elif line_index == start_line:
            col_start, col_end = start_col, full_line_len
        elif line_index == end_line:
            col_start, col_end = 0, end_col
        else:
            col_start, col_end = 0, full_line_len

        col_start -= scroll_x
        col_end -= scroll_x
        col_start = max(0, min(col_start, visible_line_len))
        col_end = max(0, min(col_end, visible_line_len))
        if col_start == col_end:
            return strip

        # 应用高亮
        return self._apply_highlight(strip, col_start, col_end, self.HIGHLIGHT_STYLE)

    def _apply_highlight(self, strip: Strip, start_x: int, end_x: int, style: Style) -> Strip:
        """在指定范围内应用高亮样式"""
        new_segments = []
        col = 0

        for segment in strip._segments:
            seg_len = segment.cell_length
            seg_end = col + seg_len

            if segment.control or seg_len == 0 or seg_end <= start_x or col >= end_x:
                new_segments.append(segment)
            else:
                left_cut = max(0, start_x - col)
                right_cut = max(0, end_x - col)
                left, rest = segment.split_cells(left_cut)
                middle, right = rest.split_cells(max(0, right_cut - left_cut))
                if left.text or left.control:
                    new_segments.append(left)
                if middle.text or middle.control:
                    new_segments.append(
                        Segment(
                            middle.text,
                            middle.style + style if middle.style else style,
                            middle.control,
                        )
                    )
                if right.text or right.control:
                    new_segments.append(right)
            col = seg_end

        return Strip(new_segments)

    def get_selected_text(self) -> str | None:
        """获取选中的文本"""
        if self._selection_start is None or self._selection_end is None:
            return None
        if self._selection_start == self._selection_end:
            return None

        start_line, start_col = self._selection_start
        end_line, end_col = self._selection_end

        # 确保 start 在 end 之前
        if (start_line, start_col) > (end_line, end_col):
            start_line, start_col, end_line, end_col = end_line, end_col, start_line, start_col

        lines = self.text.split("\n")
        selected_lines = []

        for i in range(start_line, min(end_line + 1, len(lines))):
            line = lines[i].rstrip()
            line_len = cell_len(line)
            if i == start_line and i == end_line:
                selected_lines.append(_slice_text_cells(line, min(start_col, line_len), min(end_col, line_len)))
            elif i == start_line:
                selected_lines.append(_slice_text_cells(line, min(start_col, line_len), line_len))
            elif i == end_line:
                selected_lines.append(_slice_text_cells(line, 0, min(end_col, line_len)))
            else:
                selected_lines.append(line)

        selected = "\n".join(selected_lines)
        return selected if selected else None

    def clear_selection(self) -> None:
        """清除选择状态"""
        self._selection_start = None
        self._selection_end = None
        self.refresh()

    def has_selection(self) -> bool:
        """检查是否有选择"""
        return (
            self._selection_start is not None
            and self._selection_end is not None
            and self._selection_start != self._selection_end
        )


def _slice_text_cells(text: str, start: int, end: int) -> str:
    """Slice text by terminal cell offsets, preserving full-width characters."""
    if end <= start:
        return ""
    parts: list[str] = []
    cell_pos = 0
    for char in text:
        width = get_character_cell_size(char)
        next_pos = cell_pos + width
        if next_pos > start and cell_pos < end:
            parts.append(char)
        if cell_pos >= end:
            break
        cell_pos = next_pos
    return "".join(parts)
