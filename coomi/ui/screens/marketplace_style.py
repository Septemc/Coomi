"""Skill / MCP 精选管理器共用的渲染样式辅助。

两个 marketplace 屏（Skill / MCP）列表行、来源标记、动作胶囊的渲染完全对称，
集中在此处，保证视觉统一、改一处即两处生效。纯渲染层，不含任何交互逻辑。
"""
from __future__ import annotations

from rich.markup import escape

# 状态语义 → 颜色。key 为状态文案里的关键词（子串匹配），命中即取对应色。
# 顺序敏感：先匹配"操作中"类（蓝），再失败（红），再可更新（黄），再正常（绿），兜底灰。
_STATE_COLORS: list[tuple[tuple[str, ...], str]] = [
    (("安装中", "更新中", "检查中", "卸载中", "移除中", "配置中", "测试中"), "#58a6ff"),
    (("失败", "错误"), "#f85149"),
    (("可更新", "可刷新"), "#d4a72c"),
    (("已停用", "未安装", "未配置"), "#8b949e"),
    (("已连接", "已安装", "已配置", "已是最新"), "#3fb950"),
]


def _state_color(state: str) -> str:
    for keywords, color in _STATE_COLORS:
        if any(kw in state for kw in keywords):
            return color
    return "#8b949e"


def state_badge(state: str) -> str:
    """把状态文案渲染成带背景色的小徽章。"""
    color = _state_color(state)
    return f"[bold #0d1117 on {color}] {escape(state)} [/bold #0d1117 on {color}]"


def render_source_marker(is_curated: bool) -> str:
    """来源标记：精选（青色）/ 手动（灰色）。"""
    if is_curated:
        return "[#39c5cf]精选[/#39c5cf]"
    return "[#8b949e]手动[/#8b949e]"


def render_actions(actions: list[str], action_index: int) -> str:
    """动作列表：选中项高亮胶囊，其余暗色，用 · 分隔。"""
    parts: list[str] = []
    for i, action in enumerate(actions):
        text = escape(action)
        if i == action_index:
            parts.append(f"[bold #0d1117 on #58a6ff] {text} [/bold #0d1117 on #58a6ff]")
        else:
            parts.append(f"[#8b949e]{text}[/#8b949e]")
    return " [dim]·[/dim] ".join(parts)


def detail_field(label: str, value: str) -> str:
    """详情面板里的一行字段：标签（灰）+ 值。value 需已 escape。"""
    return f"[#8b949e]{label}[/#8b949e]  {value}"


def detail_message(message: str) -> str:
    """详情面板底部状态行，按语义（失败/成功/提示）上色。value 需已 escape。"""
    if any(kw in message for kw in ("失败", "错误", "不能", "只能")):
        color = "#f85149"
    elif any(kw in message for kw in ("已", "成功", "最新")):
        color = "#3fb950"
    else:
        color = "#8b949e"
    return f"[{color}]{message}[/{color}]"


def render_footer(hint: str, plan_mode: bool) -> str:
    """底部快捷键提示 + Plan Mode 只读徽章（保留 'Plan Mode' 字样）。"""
    badge = (
        "   [bold #0d1117 on #d4a72c] Plan Mode 只读 [/bold #0d1117 on #d4a72c]"
        if plan_mode
        else ""
    )
    return f"[dim]{hint}[/dim]{badge}"
