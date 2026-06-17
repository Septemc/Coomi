"""Welcome panel for the initial empty transcript state."""
from __future__ import annotations

import struct
import zlib
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from rich.align import Align
from rich.console import Group
from rich.panel import Panel
from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual.widget import Widget


class WelcomePanel(Widget):
    """Initial guide panel with a terminal-rendered pixel mascot."""

    DEFAULT_CSS = """
    WelcomePanel {
        height: 1fr;
        background: #000000;
        padding: 1 2;
    }
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._model_display = ""
        self._tool_count = 0

    def set_context(self, model_display: str, tool_count: int) -> None:
        self._model_display = model_display
        self._tool_count = tool_count
        self.refresh()

    def render(self):
        width = max(42, self.size.width or 80)
        height = max(14, self.size.height or 24)
        bubble_width = min(70, max(36, width - 8))
        bubble = self._render_bubble(bubble_width)
        mascot = render_pixel_mascot(width=12, height=13)

        top = Table.grid(expand=True)
        top.add_column(ratio=1)
        top.add_column(width=bubble_width)
        top.add_column(ratio=1)
        top.add_row("", bubble, "")

        mascot_lines = len(mascot.plain.splitlines())
        bubble_lines = 9 if width >= 58 else 7
        spacer_lines = max(1, height - bubble_lines - mascot_lines - 3)
        left_pad = 2 if width < 80 else 4

        bottom = Table.grid(expand=True)
        bottom.add_column(width=left_pad)
        bottom.add_column(width=12)
        bottom.add_column(ratio=1)
        bottom.add_row("", Align.left(mascot), "")

        return Group(top, Text("\n" * spacer_lines), bottom)

    def _render_bubble(self, width: int) -> Panel:
        model = self._model_display or "model pending"
        tools = f"{self._tool_count} tools" if self._tool_count else "tools loading"
        guide = Text()
        guide.append("准备就绪\n", style="bold cyan")
        guide.append(f"{model} · {tools}\n\n", style="dim")
        if width < 50:
            guide.append("Enter 发送，Shift+Enter 换行。\n")
            guide.append("/model 模型，/context 上下文。\n")
            guide.append("Shift+Tab 权限，Ctrl+P 命令。\n")
            guide.append("双击 Esc 退出。", style="dim")
        else:
            guide.append("Enter 发送消息，Shift+Enter 换行。\n")
            guide.append("/model 切换模型，/context 调整上下文窗口。\n")
            guide.append("Shift+Tab 切换工具权限模式。\n")
            guide.append("Ctrl+P 打开命令面板，Ctrl+C 复制选中文本。\n")
            guide.append("双击 Esc 退出应用。", style="dim")
        return Panel(
            guide,
            width=width,
            padding=(1, 2),
            border_style="#00a8df",
            title="操作指南",
            title_align="left",
        )


def render_pixel_mascot(width: int = 12, height: int = 13) -> Text:
    """Render assets/mascot/coomi.png as block pixels."""
    image_path = _find_mascot_path()
    if image_path is None:
        fallback = Text()
        fallback.append("[ mascot ]", style="bold cyan")
        return fallback

    try:
        pixels, src_width, src_height = _load_png_rgba(str(image_path))
        sampled = _sample_image(pixels, src_width, src_height, width, height)
        return _pixels_to_half_blocks(sampled, width, height)
    except Exception:
        fallback = Text()
        fallback.append("[ mascot ]", style="bold cyan")
        return fallback


def _find_mascot_path() -> Path | None:
    candidates = [
        Path.cwd() / "assets" / "mascot" / "coomi.png",
        Path(__file__).resolve().parents[3] / "assets" / "mascot" / "coomi.png",
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


@lru_cache(maxsize=4)
def _load_png_rgba(path: str) -> tuple[list[tuple[int, int, int, int]], int, int]:
    data = Path(path).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG file")

    pos = 8
    width = height = bit_depth = color_type = None
    idat = bytearray()

    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        chunk_type = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, compression, filter_method, interlace = (
                struct.unpack(">IIBBBBB", chunk)
            )
            if bit_depth != 8 or compression != 0 or filter_method != 0 or interlace != 0:
                raise ValueError("unsupported PNG format")
            if color_type not in (2, 6):
                raise ValueError("unsupported PNG color type")
        elif chunk_type == b"IDAT":
            idat.extend(chunk)
        elif chunk_type == b"IEND":
            break

    if width is None or height is None or bit_depth is None or color_type is None:
        raise ValueError("missing PNG header")

    channels = 4 if color_type == 6 else 3
    stride = width * channels
    raw = zlib.decompress(bytes(idat))
    rows: list[bytearray] = []
    offset = 0
    prev = bytearray(stride)

    for _ in range(height):
        filter_type = raw[offset]
        offset += 1
        row = bytearray(raw[offset:offset + stride])
        offset += stride
        _unfilter_row(row, prev, filter_type, channels)
        rows.append(row)
        prev = row

    pixels: list[tuple[int, int, int, int]] = []
    for row in rows:
        for x in range(width):
            base = x * channels
            r, g, b = row[base], row[base + 1], row[base + 2]
            a = row[base + 3] if channels == 4 else 255
            pixels.append((r, g, b, a))

    return pixels, width, height


def _unfilter_row(row: bytearray, prev: bytearray, filter_type: int, bpp: int) -> None:
    for i in range(len(row)):
        left = row[i - bpp] if i >= bpp else 0
        up = prev[i]
        up_left = prev[i - bpp] if i >= bpp else 0
        if filter_type == 1:
            row[i] = (row[i] + left) & 0xFF
        elif filter_type == 2:
            row[i] = (row[i] + up) & 0xFF
        elif filter_type == 3:
            row[i] = (row[i] + ((left + up) // 2)) & 0xFF
        elif filter_type == 4:
            row[i] = (row[i] + _paeth(left, up, up_left)) & 0xFF
        elif filter_type != 0:
            raise ValueError("unsupported PNG filter")


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _sample_image(
    pixels: list[tuple[int, int, int, int]],
    src_width: int,
    src_height: int,
    out_width: int,
    out_height: int,
) -> list[tuple[int, int, int, int]]:
    sampled: list[tuple[int, int, int, int]] = []
    for y in range(out_height):
        y0 = y * src_height // out_height
        y1 = max(y0 + 1, (y + 1) * src_height // out_height)
        for x in range(out_width):
            x0 = x * src_width // out_width
            x1 = max(x0 + 1, (x + 1) * src_width // out_width)
            sampled.append(_average_pixels(_iter_region(pixels, src_width, x0, y0, x1, y1)))
    return sampled


def _iter_region(
    pixels: list[tuple[int, int, int, int]],
    width: int,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
) -> Iterable[tuple[int, int, int, int]]:
    for y in range(y0, y1):
        start = y * width
        for x in range(x0, x1):
            yield pixels[start + x]


def _average_pixels(region: Iterable[tuple[int, int, int, int]]) -> tuple[int, int, int, int]:
    r = g = b = a = count = 0
    for pr, pg, pb, pa in region:
        r += pr * pa
        g += pg * pa
        b += pb * pa
        a += pa
        count += 1
    if count == 0 or a == 0:
        return (0, 0, 0, 0)
    return (r // a, g // a, b // a, a // count)


def _pixels_to_half_blocks(
    pixels: list[tuple[int, int, int, int]],
    width: int,
    height: int,
) -> Text:
    text = Text()
    for y in range(0, height, 2):
        for x in range(width):
            top = pixels[y * width + x]
            bottom = pixels[(y + 1) * width + x] if y + 1 < height else (0, 0, 0, 0)
            text.append(_pixel_char(top, bottom), style=_pixel_style(top, bottom))
        if y + 2 < height:
            text.append("\n")
    return text


def _pixel_char(
    top: tuple[int, int, int, int],
    bottom: tuple[int, int, int, int],
) -> str:
    top_visible = top[3] > 32
    bottom_visible = bottom[3] > 32
    if top_visible and bottom_visible:
        return "▀"
    if top_visible:
        return "▀"
    if bottom_visible:
        return "▄"
    return " "


def _pixel_style(
    top: tuple[int, int, int, int],
    bottom: tuple[int, int, int, int],
) -> Style:
    top_visible = top[3] > 32
    bottom_visible = bottom[3] > 32
    color = _rgb_hex(top) if top_visible else None
    bgcolor = _rgb_hex(bottom) if bottom_visible and top_visible else None
    if bottom_visible and not top_visible:
        color = _rgb_hex(bottom)
    return Style(color=color, bgcolor=bgcolor)


def _rgb_hex(pixel: tuple[int, int, int, int]) -> str:
    return f"#{pixel[0]:02x}{pixel[1]:02x}{pixel[2]:02x}"
