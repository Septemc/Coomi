"""SelectableRichLog — 支持鼠标拖拽选中 + 复制的 RichLog

绕过 Textual 的选择系统，自己管理选择状态。
支持部分选择、高亮保持、复制功能。
"""
from __future__ import annotations

from rich.segment import Segment
from rich.style import Style
from textual import events
from textual.strip import Strip
from textual.widgets import RichLog


class SelectableRichLog(RichLog):
    """支持文本选择的 RichLog"""

    ALLOW_SELECT = True

    # 选择状态
    _selection_start: tuple[int, int] | None = None  # (line, col)
    _selection_end: tuple[int, int] | None = None  # (line, col)
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
        # 计算点击位置对应的 (line, col)
        line, col = self._get_position(event.x, event.y)
        self._selection_start = (line, col)
        self._selection_end = (line, col)
        self._is_selecting = True
        self.refresh()  # 触发重绘

    def on_mouse_move(self, event: events.MouseMove) -> None:
        """鼠标移动：更新选择范围"""
        if self._is_selecting:
            line, col = self._get_position(event.x, event.y)
            self._selection_end = (line, col)
            self.refresh()  # 触发重绘

    def on_mouse_up(self, event: events.MouseUp) -> None:
        """鼠标松开：保持选择状态"""
        self._is_selecting = False
        # 保持选择状态，不清除

    def _get_position(self, x: int, y: int) -> tuple[int, int]:
        """将屏幕坐标转换为 (line, col)"""
        scroll_x, scroll_y = self.scroll_offset
        line = scroll_y + y

        # 获取该行的 Strip
        try:
            strip = self.render_line(y)
        except Exception:
            return line, 0

        # 遍历 Strip 的 Segments 计算 col
        col = 0
        current_x = 0
        for segment in strip._segments:
            seg_len = len(segment.text)
            if current_x + seg_len > x:
                col = x - current_x
                break
            current_x += seg_len
            col += seg_len

        return line, col

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
        if line_index == start_line and line_index == end_line:
            col_start, col_end = start_col, end_col
        elif line_index == start_line:
            col_start, col_end = start_col, strip.cell_length
        elif line_index == end_line:
            col_start, col_end = 0, end_col
        else:
            col_start, col_end = 0, strip.cell_length

        # 应用高亮
        return self._apply_highlight(strip, col_start, col_end, self.HIGHLIGHT_STYLE)

    def _apply_highlight(self, strip: Strip, start_x: int, end_x: int, style: Style) -> Strip:
        """在指定范围内应用高亮样式"""
        new_segments = []
        col = 0

        for segment in strip._segments:
            seg_text = segment.text
            seg_len = len(seg_text)
            seg_end = col + seg_len

            if seg_end <= start_x or col >= end_x:
                # 完全不在选择范围内
                new_segments.append(segment)
            elif col >= start_x and seg_end <= end_x:
                # 完全在选择范围内
                new_segments.append(Segment(
                    seg_text,
                    segment.style + style if segment.style else style,
                    segment.control,
                ))
            else:
                # 部分在选择范围内 — 分割
                for i, char in enumerate(seg_text):
                    pos = col + i
                    if start_x <= pos < end_x:
                        new_segments.append(Segment(
                            char,
                            segment.style + style if segment.style else style,
                            segment.control,
                        ))
                    else:
                        new_segments.append(Segment(char, segment.style, segment.control))
            col = seg_end

        return Strip(new_segments)

    def get_selected_text(self) -> str | None:
        """获取选中的文本"""
        if self._selection_start is None or self._selection_end is None:
            return None

        start_line, start_col = self._selection_start
        end_line, end_col = self._selection_end

        # 确保 start 在 end 之前
        if (start_line, start_col) > (end_line, end_col):
            start_line, start_col, end_line, end_col = end_line, end_col, start_line, start_col

        lines = self.text.split("\n")
        selected_lines = []

        for i in range(start_line, min(end_line + 1, len(lines))):
            line = lines[i]
            if i == start_line and i == end_line:
                selected_lines.append(line[start_col:end_col])
            elif i == start_line:
                selected_lines.append(line[start_col:])
            elif i == end_line:
                selected_lines.append(line[:end_col])
            else:
                selected_lines.append(line)

        return "\n".join(selected_lines)

    def clear_selection(self) -> None:
        """清除选择状态"""
        self._selection_start = None
        self._selection_end = None
        self.refresh()

    def has_selection(self) -> bool:
        """检查是否有选择"""
        return self._selection_start is not None and self._selection_end is not None
