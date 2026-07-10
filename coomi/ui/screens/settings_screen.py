"""SettingsScreen — 设置面板模态屏。"""
from __future__ import annotations

from typing import Optional
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.screen import ModalScreen
from textual.widgets import Static

SETTINGS_OPTIONS = [
    ("provider_config", "新增/修改模型API配置", "管理 LLM Provider"),
    ("install_skill", "管理 Skill", "精选目录、安装、更新、卸载"),
    ("install_mcp", "管理 MCP", "精选目录、配置、测试、移除"),
]

SETTINGS_GUIDE = {
    "provider_config": (
        "[bold cyan]当前选中：LLM Provider[/bold cyan]\n"
        "Enter 打开 Provider 管理；新增或编辑 base_url、model、api_key、tool_protocol。保存后配置会自动生效，通常不需要再手动 /model。\n\n"
        "[bold]完整流程[/bold]\n"
        "1. 进入“新增/修改模型API配置”。\n"
        "2. 选择新增 Provider 或编辑已有 Provider。\n"
        "3. 填写显示名称、接口类型、Base URL、模型名和 API Key。\n"
        "4. tool_protocol 可选 auto/native/structured/mimo/disabled；不确定时用 auto。\n"
        "5. 保存后返回主界面，直接继续对话；需要切换多个 Provider 时再使用 /model。\n"
    ),
    "install_skill": (
        "[bold cyan]当前选中：Skill[/bold cyan]\n"
        "Skill 是可安装、可启用/停用的能力说明包。Coomi 会在相关任务中读取已启用 Skill 的 SKILL.md，把专业流程注入上下文。\n\n"
        "按 Enter 打开精选管理中心：↑↓ 选择；Enter 安装、检查更新或应用更新；Delete 确认卸载；Esc 返回。\n\n"
        "[bold]常用命令[/bold]\n"
        "/skill list 查看已安装 Skill\n"
        "/skill install C:\\path\\my-skill 安装本地 Skill 目录，目录内需要 SKILL.md\n"
        "/skill install https://github.com/owner/repo/tree/main/skills/name 从 GitHub 安装\n"
        "/skill enable name 启用；/skill disable name 停用\n"
        "/skill update name 更新 GitHub 来源 Skill；/skill info name 查看详情\n"
        "使用时直接提到任务领域或写 $SkillName，Coomi 会在匹配时加载说明。\n"
    ),
    "install_mcp": (
        "[bold cyan]当前选中：MCP[/bold cyan]\n"
        "MCP 用来接入外部工具服务器。启用成功后，服务器暴露的工具会注册成 mcp__server__tool 格式，Agent 可以像普通工具一样调用。\n\n"
        "按 Enter 打开精选管理中心：↑↓ 选择；Enter 配置或测试并刷新工具；Delete 确认移除；Esc 返回。需要 Token、目录或数据库地址的项目会先显示配置表单。\n\n"
        "[bold]常用命令[/bold]\n"
        "/mcp list 查看服务器\n"
        "/mcp add files stdio npx -y @modelcontextprotocol/server-filesystem F:\\Work 添加 stdio 服务器\n"
        "/mcp add docs http https://example.com/mcp 添加 HTTP 服务器\n"
        "/mcp add stream sse https://example.com/sse 添加 SSE 服务器\n"
        "/mcp test name 测试连接；/mcp tools name 查看工具\n"
        "/mcp enable name 启用并注册工具；/mcp disable name 停用并卸载工具\n"
    ),
}


class SettingsScreen(ModalScreen[Optional[str]]):
    """设置面板"""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("up", "move_up", "Up", priority=True),
        Binding("down", "move_down", "Down", priority=True),
        Binding("enter", "confirm", "Confirm", priority=True),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._selected: int = 0

    def compose(self) -> ComposeResult:
        with Container(id="settings-container"):
            yield Static("  Settings", id="settings-title")
            yield Static(self._render_options(), id="settings-options")
        yield Static(self._render_guide(), id="settings-guide")

    def _render_options(self) -> str:
        lines = []
        for i, (key, label, desc) in enumerate(SETTINGS_OPTIONS):
            if i == self._selected:
                lines.append(f"[bold reverse] ● {label} [/bold reverse]  [dim]{desc}[/dim]")
            else:
                lines.append(f"  [cyan]○[/cyan] {label}  [dim]{desc}[/dim]")
        lines.append("")
        lines.append("[dim]↑↓ 导航  Enter 选择  Esc 返回[/dim]")
        return "\n".join(lines)

    def _refresh_display(self) -> None:
        try:
            options = self.query_one("#settings-options", Static)
            options.update(self._render_options())
            guide = self.query_one("#settings-guide", Static)
            guide.update(self._render_guide())
        except Exception:
            pass

    def _render_guide(self) -> str:
        key = SETTINGS_OPTIONS[self._selected][0]
        selected_guide = SETTINGS_GUIDE[key]
        quick_reference = (
            "\n[bold]配置文件位置[/bold]\n"
            "LLM: ~/.coomi/config/providers.json\n"
            "Skill: ~/.coomi/config/skills.json 和 ~/.coomi/skills/\n"
            "MCP: ~/.coomi/config/mcp_servers.json\n\n"
            "[dim]提示：Plan Mode 下只能查看或规划，安装 Skill、添加 MCP、修改 Provider 需要退出 Plan Mode 后执行。[/dim]"
        )
        direct_paste = (
            "\n\n[bold]输入框直接配置[/bold]\n"
            "Provider JSON：把 Provider JSON 直接粘贴到输入框发送，Coomi 会自动添加、激活并刷新 LLM 运行配置。\n"
            "Skill 链接/路径：把 GitHub Skill 链接或本地 Skill 路径粘贴到输入框发送，Coomi 会自动安装、修复、启用并刷新 Skill 上下文。\n"
            "MCP JSON/URL/stdio：把 MCP JSON、MCP URL 或 MCP stdio 命令粘贴到输入框发送，Coomi 会自动配置、测试并注册工具。\n"
            "Plan Mode：直接粘贴只会解析并给出执行计划，不会写文件、安装 Skill 或启用 MCP。\n"
        )
        return selected_guide + quick_reference + direct_paste

    def action_move_up(self) -> None:
        self._selected = (self._selected - 1) % len(SETTINGS_OPTIONS)
        self._refresh_display()

    def action_move_down(self) -> None:
        self._selected = (self._selected + 1) % len(SETTINGS_OPTIONS)
        self._refresh_display()

    def action_confirm(self) -> None:
        key = SETTINGS_OPTIONS[self._selected][0]
        if key == "provider_config":
            self.dismiss("provider_config")
        elif key == "install_skill":
            self.dismiss("install_skill")
        elif key == "install_mcp":
            self.dismiss("install_mcp")

    def action_cancel(self) -> None:
        self.dismiss(None)
