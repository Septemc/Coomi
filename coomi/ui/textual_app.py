"""CoomiApp — Textual App 主类

架构:
  App (Provider Manager)
    └── MainScreen (UI 委托)
          ├── Header (dock: top)
          ├── RichLog (#message-log)
          ├── StreamingPreview (#stream-preview)
          ├── StatusPanel (#status-panel)
          └── PromptTextArea (#prompt-input)
"""
from __future__ import annotations

import asyncio
import os
import re
import shlex
import time
from typing import Any

from rich.markdown import Markdown
from rich.text import Text
from textual import events
from textual import work
from textual.app import App
from textual.binding import Binding
from textual.widgets import Input

from .widgets.selectable_rich_log import SelectableRichLog as RichLog
from .widgets.custom_header import CustomHeader

from ..engine.session import Session, SessionManager, build_system_prompt
from ..services import get_llm_provider
from ..services.session_history import (
    append_session_state,
    delete_session_record,
    list_session_records,
    load_session_from_jsonl,
)
from ..services.llm.factory import get_config_manager
from ..services.update_check import build_update_prompt_suffix, check_for_update
from ..services.memory import MemoryManager, MemoryRecall, MemoryType
from ..services.memory.extractor import MemoryExtractor
from ..services.context.compressor import _estimate_tokens_from_dicts
from ..services.auto_config import (
    INTENT_MCP,
    INTENT_PROVIDER,
    INTENT_SKILL,
    InputIntent,
    InputIntentDetector,
    McpAutoConfigurator,
    ProviderAutoConfigurator,
    SkillAutoInstaller,
    normalize_mcp_config,
    normalize_provider_config,
    redact_secret,
)
from ..services.skills import SkillManager
from ..services.skills.installer import SkillInstallError
from ..services.mcp import McpManager
from ..services.mcp.client import McpError
from ..security import HookSystem, PermissionLevel, PermissionSystem
from ..tools.registry import create_default_registry
from ..ui.events import (
    AgentCancelled,
    AgentError,
    CompressionEvent,
    ConnectionRetry,
    LoopProgress,
    LoopStepStart,
    LoopStepDone,
    LoopIssueCreated,
    ReasoningChunk,
    TextChunk,
    ToolCacheHit,
    ToolDone,
    ToolRunning,
    ToolStart,
    UsageUpdate,
)
from .status_line import StatusLine, format_token_count
from .widgets.status_panel import StatusPanel
from .widgets.streaming_preview import StreamingPreview
from .widgets.tool_call_banner import ToolCallBanner
from .widgets.prompt_text_area import PromptTextArea
from .widgets.pending_queue_panel import PendingQueuePanel, QUEUE_ACTIONS
from .widgets.comm_panel import CommPanel, CommInput
from .screens.main_screen import (
    PROMPT_PLACEHOLDER,
    PROMPT_PLACEHOLDER_RUNNING,
    MainScreen,
    idle_placeholder,
)
from .screens.command_palette import CommandPalette
from .terminal_capabilities import supports_modified_enter

SPINNER_CHARS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SGR_MOUSE_REPORT_RE = re.compile(r"(?:\x1b)?\[<\d+;-?\d+;-?\d+[mM]")


def strip_sgr_mouse_reports(text: str) -> str:
    """Remove terminal mouse reports that leaked into printable input."""
    return SGR_MOUSE_REPORT_RE.sub("", text)


# ============================================================
# 命令处理器
# ============================================================

def _handle_model_command(config_mgr, status_line: StatusLine, ctx: dict, args: str) -> str:
    if not args:
        providers = config_mgr.list_providers()
        active_id = config_mgr.data.get("active", "")
        if not providers:
            return "[dim]No providers configured. Edit ~/.coomi/config/providers.json[/dim]"
        lines = [f"[bold cyan]Available models ({len(providers)}):[/bold cyan]"]
        for p in providers:
            marker = " [bold green](active)[/bold green]" if p.id == active_id else ""
            fast_info = f" [dim](fast: {p.fast_model})[/dim]" if p.fast_model else ""
            lines.append(f"  [bold]{p.id}[/bold]: {p.display} ({p.type_label}){fast_info}{marker}")
        lines.append("[dim]Switch: /model <id>[/dim]")
        return "\n".join(lines)

    provider_id = args.strip()
    if not config_mgr.set_active(provider_id):
        return f"[red]Model not found: {provider_id}[/red]\n\n[dim]Use /model to list[/dim]"

    new_provider = get_llm_provider(provider_id)
    ctx["provider"] = new_provider
    ctx["agent"].llm = new_provider
    ctx["agent"].compressor.llm = new_provider
    if ctx.get("memory_extractor"):
        ctx["memory_extractor"].llm = new_provider
    if ctx.get("memory_recall"):
        ctx["memory_recall"].llm = new_provider
    status_line.set_model(new_provider.model, new_provider.get_model_display_name())
    ctx["display_name"] = new_provider.get_model_display_name()
    return f"[bold cyan]Switched to:[/bold cyan] {new_provider.get_model_display_name()} ([dim]{new_provider.model}[/dim])"


def _handle_context_command(status_line: StatusLine, agent, args: str) -> str:
    if not args:
        current = status_line.get_context_window_size()
        return (
            f"[bold cyan]Context window:[/bold cyan] {format_token_count(current)}\n\n"
            "[dim]Presets: /context 128k | /context 256k | /context 512k | /context 1m\n"
            "Custom: /context <number>[k|m][/dim]"
        )
    size_str = args.strip().lower()
    multiplier = 1
    if size_str.endswith("k"):
        multiplier = 1_000
        size_str = size_str[:-1]
    elif size_str.endswith("m"):
        multiplier = 1_000_000
        size_str = size_str[:-1]
    try:
        size = int(size_str) * multiplier
        if size < 1_000:
            return "[red]Context window must be at least 1K tokens[/red]"
        if size > 10_000_000:
            return "[red]Context window max 10M tokens[/red]"
        status_line.set_context_window_size(size)
        agent.context_window_size = size
        return f"[bold cyan]Context window set to:[/bold cyan] {format_token_count(size)}"
    except ValueError:
        return "[red]Invalid format. Use e.g. /context 256k or /context 512k[/red]"


def _handle_memory_command(memory_manager: MemoryManager, args: str) -> str:
    if not args:
        return (
            "[bold cyan]Memory commands:[/bold cyan]\n\n"
            "  /memory list           - List all memories\n"
            "  /memory add <content>  - Add a memory\n"
            "  /memory delete <name>  - Delete a memory\n"
            "  /memory search <query> - Search memories\n"
            "  /memory show <name>    - Show memory details\n"
            "  /memory refresh        - Refresh index"
        )
    parts = args.split(maxsplit=1)
    subcmd = parts[0].lower()
    subargs = parts[1].strip() if len(parts) > 1 else ""

    if subcmd == "list":
        memories = memory_manager.list_memories()
        if not memories:
            return "[dim]No memories[/dim]"
        lines = [f"[bold cyan]Memories ({len(memories)}):[/bold cyan]"]
        for m in memories:
            stale_marker = " [red][stale][/red]" if m.is_stale else ""
            type_color = {
                MemoryType.USER: "green", MemoryType.FEEDBACK: "yellow",
                MemoryType.PROJECT: "blue", MemoryType.REFERENCE: "magenta",
            }.get(m.memory_type, "white")
            lines.append(f"  [{type_color}]{m.memory_type.value:10}[/{type_color}] {m.name}: {m.description}{stale_marker}")
        return "\n".join(lines)

    if subcmd == "add":
        if not subargs:
            return "[red]Please provide memory content[/red]"
        import hashlib
        name = hashlib.md5(subargs.encode()).hexdigest()[:8]
        from ..services.memory.types import Memory
        memory = Memory(
            name=f"memory-{name}",
            description=subargs[:50] + ("..." if len(subargs) > 50 else ""),
            memory_type=MemoryType.USER,
            content=subargs,
        )
        if memory_manager.save_memory(memory):
            return f"[bold green]+ Memory saved:[/bold green] memory-{name}"
        return "[red]Save failed[/red]"

    if subcmd == "delete":
        if not subargs:
            return "[red]Please provide memory name[/red]"
        if memory_manager.delete_memory(subargs):
            return f"[bold green]- Memory deleted:[/bold green] {subargs}"
        return f"[red]Memory not found: {subargs}[/red]"

    if subcmd == "search":
        if not subargs:
            return "[red]Please provide search query[/red]"
        results = memory_manager.search_memories(subargs)
        if not results:
            return "[dim]No matches[/dim]"
        lines = [f"[bold cyan]Results ({len(results)}):[/bold cyan]"]
        for m in results:
            lines.append(f"  {m.name}: {m.description}")
        return "\n".join(lines)

    if subcmd == "show":
        if not subargs:
            return "[red]Please provide memory name[/red]"
        memory = memory_manager.get_memory(subargs)
        if not memory:
            return f"[red]Memory not found: {subargs}[/red]"
        return (
            f"[bold cyan]Memory: {memory.name}[/bold cyan]\n\n"
            f"[dim]Type: {memory.memory_type.value}\nDescr: {memory.description}\n"
            f"Created: {memory.created_at}\nUpdated: {memory.updated_at}[/dim]\n\n{memory.content}"
        )

    if subcmd == "refresh":
        memory_manager.refresh_index()
        return "[bold green]+ Index refreshed[/bold green]"

    return f"[red]Unknown subcommand: {subcmd}[/red]"


def _option_value(parts: list[str], option: str) -> str | None:
    if option not in parts:
        return None
    idx = parts.index(option)
    if idx + 1 >= len(parts):
        return None
    return parts[idx + 1]


# ============================================================
# CoomiApp
# ============================================================

class CoomiApp(App):
    """Coomi Agent 主应用 — Provider Manager"""

    CSS_PATH = "tcss/coomi.tcss"

    BINDINGS = [
        Binding("ctrl+c", "copy_selected", "Copy", priority=True, show=False),
        Binding("shift+tab", "cycle_permission_mode", "Permission Mode", priority=True),
        Binding("ctrl+p", "command_palette", "Command Palette"),
        Binding("ctrl+g", "enter_queue_mode", "Queue", priority=True),
        Binding("ctrl+t", "toggle_comm_focus", "Talk", priority=True),
        Binding("f2", "go_home", "Home", priority=True),
        Binding("f3", "open_settings", "Setting", priority=True),
        # 问询模式导航 — priority=True 在 TextArea BINDINGS 之前检查
        Binding("up", "question_up", "↑", priority=True),
        Binding("down", "question_down", "↓", priority=True),
        Binding("left", "question_left", "←", priority=True),
        Binding("right", "question_right", "→", priority=True),
        Binding("space", "question_toggle", "Toggle", priority=True),
        Binding("shift+enter", "insert_prompt_newline", "Newline", priority=True, show=False),
        Binding("ctrl+enter", "insert_prompt_newline", "Newline", priority=True, show=False),
        Binding("ctrl+j", "insert_prompt_newline", "Newline", priority=True, show=False),
        Binding("enter", "question_confirm", "Confirm", priority=True),
        Binding("escape", "question_cancel_or_exit", "Cancel/Exit", priority=True),
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.status_line = StatusLine()
        self._provider = None
        self._config_mgr = None
        self._agent = None
        self._session: Session | None = None
        self._session_mgr = SessionManager()
        self._memory_manager: MemoryManager | None = None
        self._memory_extractor: MemoryExtractor | None = None
        self._memory_recall: MemoryRecall | None = None
        self._tool_registry = None
        self._skill_manager: SkillManager | None = None
        self._mcp_manager: McpManager | None = None
        self._display_name: str = ""
        self._ctx: dict = {}

        # Agent execution state
        self._agent_running: bool = False
        self._cancel_requested: bool = False
        self._spinner_idx: int = 0
        self._spinner_timer = None
        self._stream_buffer: str = ""
        self._pending_input: str | None = None
        self._cwd: str = ""
        self._tool_start_time: float = 0.0
        self._pending_tool_name: str = ""
        self._pending_tool_args: str = ""
        self._mcp_called_this_turn: set[str] = set()
        self._input_intent_detector = InputIntentDetector()

        # Exit state
        self._exit_pending: bool = False
        self._exit_timer = None

        # Phase 2: streaming state
        self._reasoning_visible: bool = True
        self._full_reasoning: str = ""
        self._reasoning_start_time: float = 0.0
        self._active_banners: dict[str, ToolCallBanner] = {}

        # Phase 3: plan/question mode
        self._plan_mode: bool = False
        self._question_mode: bool = False
        self._plan_panel = None
        self._question_future: asyncio.Future | None = None

        # Loop mode
        self._loop_mode: bool = False
        self._loop_runner = None
        self._loop_session: Any = None  # LoopSession

        # Slash command inline autocomplete
        self._command_list = None
        self._command_mode: bool = False

        # Unified interactive mode management
        # "none" | "command" | "question" | "model_picker" | "context_picker" | "queue"
        self._interactive_mode: str = "none"

        # Pending execution queue (executes after current run finishes)
        self._pending_queue: list[str] = []
        self._queue_msg_index: int = 0        # ↑↓ 选中第几条
        self._queue_action_index: int = 0     # ←→ 选中动作：0插队 1置顶 2编辑 3删除
        self._queue_drain_gate = asyncio.Event()
        self._queue_drain_gate.set()          # 未在整理时闸门常开

        # 交流队列（独立于主待执行队列）：Ctrl+T 交流窗口的输入进这里，
        # 当前 agent 轮次结束后优先插入执行，仅限本次，不触发主队列。
        self._comm_queue: list[str] = []

        # 并发只读交流（side session）：主任务某个工具/PowerShell 正在阻塞执行时，
        # 交流窗口的输入 / 立即引导会在独立只读旁路会话上并发执行，利用闲置 LLM 资源。
        self._tool_executing: bool = False          # 是否正处于工具阻塞窗口
        self._side_task: asyncio.Task | None = None  # 当前并发交流任务（同一时刻仅一个）
        self._side_pending: list[str] = []           # 阻塞窗口内排队等待的 side 交流内容

        # Model/Context picker state
        self._model_picker = None
        self._context_picker = None

        # Active provider tracking (may differ from providers.json if once_active was used)
        self._active_provider_id: str = ""
        self._permission_system = PermissionSystem()
        self._hook_system = HookSystem()
        self.status_line.set_permission_label(self._permission_system.get_mode_label())

    # -- helpers -----------------------------------------------------------

    def _wl(self, log: RichLog, content: str | object) -> None:
        """Append content to RichLog. Rich markup is rendered natively."""
        try:
            if hasattr(self.screen, "hide_welcome_panel"):
                self.screen.hide_welcome_panel()
        except Exception:
            pass
        log.write(content)

    def _show_welcome_message(self) -> None:
        """显示欢迎消息（在屏幕准备好后调用）"""
        try:
            tool_count = len(self._tool_registry.list_tools())
            sessions = list_session_records(limit=8)
            if hasattr(self.screen, "update_welcome_panel"):
                self.screen.update_welcome_panel(self._display_name, tool_count, sessions)
                self.screen.show_welcome_panel()
                self._focus_prompt_input()
        except Exception:
            pass

    # -- compose / mount ---------------------------------------------------

    def compose(self):
        """不直接 compose widgets，由 MainScreen 管理 UI"""
        return []

    async def on_mount(self) -> None:
        self._cwd = os.getcwd()

        # Push MainScreen as the primary UI
        self.push_screen(MainScreen(status_line=self.status_line))
        self._config_mgr = get_config_manager()
        self._provider = get_llm_provider()
        self._active_provider_id = self._config_mgr.data.get("active", "default")
        self._tool_registry = create_default_registry()
        self._skill_manager = SkillManager()
        self._mcp_manager = McpManager()
        self._register_mcp_tools(show_errors=False)

        self._memory_manager = MemoryManager(project_path=self._cwd)
        self._memory_extractor = MemoryExtractor(self._provider, self._memory_manager)
        self._memory_recall = MemoryRecall(self._provider, self._memory_manager)

        model_name = self._provider.model if hasattr(self._provider, "model") else "unknown"
        self._display_name = self._provider.get_model_display_name()
        self.status_line.set_model(model_name, self._display_name)

        from ..engine.loop import AgentLoop
        ctx_window = self.status_line.get_context_window_size()
        self._agent = AgentLoop(
            self._provider,
            self._tool_registry,
            ctx_window,
            app_context=self,
            permission_system=self._permission_system,
            hook_system=self._hook_system,
            project_path=self._cwd,
        )

        self._ctx = {
            "provider": self._provider,
            "agent": self._agent,
            "memory_extractor": self._memory_extractor,
            "memory_recall": self._memory_recall,
            "display_name": self._display_name,
        }

        system_prompt = await build_system_prompt(
            memory_manager=self._memory_manager,
            memory_recall=self._memory_recall,
            skill_manager=self._skill_manager,
            current_context="",
            cwd=self._cwd,
            model_display=self._display_name,
            permission_mode=self._permission_system.mode.value,
        )
        self._session = self._session_mgr.create_session(
            system_prompt=system_prompt,
            cwd=self._cwd,
            model=self._display_name,
        )

        # Wait for screen to be ready before querying widgets
        self.call_after_refresh(self._show_welcome_message)
        self.call_after_refresh(self._show_multiline_key_hint)
        self.call_after_refresh(self._start_update_check)

    def _show_multiline_key_hint(self) -> None:
        """Warn when Shift+Enter support cannot be established at startup."""
        if not supports_modified_enter():
            self.notify(
                "当前终端可能无法区分 Shift+Enter；请使用 Ctrl+J 换行。",
                title="多行输入",
                severity="warning",
                timeout=8,
            )

    # -- command palette ---------------------------------------------------

    def _start_update_check(self) -> None:
        asyncio.create_task(self._check_for_update_notice())

    async def _check_for_update_notice(self) -> None:
        result = await asyncio.to_thread(check_for_update)
        suffix = build_update_prompt_suffix(result)
        if not suffix:
            return
        try:
            prompt = self.screen.query_one("#prompt-input", PromptTextArea)
            # 更新提示优先占据第二行（覆盖随机使用提示）
            prompt.placeholder = f"{PROMPT_PLACEHOLDER}\n⬆ {suffix}"
        except Exception:
            pass

    def action_command_palette(self) -> None:
        """Ctrl+P: 打开命令面板"""

        def on_palette_result(cmd: str | None) -> None:
            if cmd:
                self._execute_command(cmd)

        self.push_screen(CommandPalette(), on_palette_result)

    def action_open_settings(self) -> None:
        """打开设置面板"""
        from .screens.settings_screen import SettingsScreen

        def on_settings_result(option: str | None) -> None:
            if option == "provider_config":
                self._open_provider_list()
            elif option == "install_skill":
                self._open_skill_marketplace()
            elif option == "install_mcp":
                self._open_mcp_marketplace()

        self.push_screen(SettingsScreen(), on_settings_result)

    def _apply_provider_runtime(self, new_provider, provider_id: str, mode_label: str | None = None) -> None:
        """Attach a provider instance to every live runtime component."""
        self._provider = new_provider
        self._display_name = new_provider.get_model_display_name()
        self._active_provider_id = provider_id

        if self._agent:
            self._agent.llm = new_provider
            self._agent.compressor.llm = new_provider
        if self._memory_extractor:
            self._memory_extractor.llm = new_provider
        if self._memory_recall:
            self._memory_recall.llm = new_provider

        self._ctx["provider"] = new_provider
        if self._agent:
            self._ctx["agent"] = self._agent
        if self._memory_extractor:
            self._ctx["memory_extractor"] = self._memory_extractor
        if self._memory_recall:
            self._ctx["memory_recall"] = self._memory_recall
        self._ctx["display_name"] = self._display_name

        self.status_line.set_model(new_provider.model, self._display_name)
        self._refresh_status_panel()

        if mode_label:
            self._show_command_result(
                f"[bold cyan]Switched to:[/bold cyan] {self._display_name} "
                f"([dim]{new_provider.model}[/dim]) [{mode_label}]"
            )

    async def _reload_active_provider_from_config(self, show_message: bool = False) -> bool:
        """Reload providers.json and refresh the active model in the running app."""
        if not self._config_mgr:
            return False
        self._config_mgr.reload()
        active_id = self._config_mgr.data.get("active", "")
        if not active_id:
            if show_message:
                self._show_command_result("[yellow]Provider saved, but no active provider is configured.[/yellow]")
            return False
        try:
            new_provider = get_llm_provider(active_id)
        except Exception as exc:
            if show_message:
                self._show_command_result(f"[red]Failed to reload provider:[/red] {exc}")
            return False

        self._apply_provider_runtime(new_provider, active_id)
        await self._rebuild_system_prompt()
        if show_message:
            self._show_command_result(
                f"[bold green]Provider saved and active model refreshed:[/bold green] "
                f"{self._display_name} ([dim]{new_provider.model}[/dim])"
            )
        return True

    def action_go_home(self) -> None:
        """Return to the welcome screen without modifying the current session."""
        if self._agent_running:
            self._show_command_result("[dim]Agent 正在运行，完成或取消后可返回 Home。[/dim]")
            return
        self._hide_command_list()
        self._hide_model_picker()
        self._hide_context_picker()
        self._set_interactive_mode("none")
        self._show_welcome_message()

    def _open_provider_list(self) -> None:
        """打开 Provider 列表"""
        from .screens.provider_list_screen import ProviderListScreen

        def on_provider_list_result(result: dict | None) -> None:
            asyncio.create_task(self._reload_active_provider_from_config())
            if result is None:
                return
            if result["action"] == "add":
                self._open_provider_edit(None)
            elif result["action"] == "edit":
                self._open_provider_edit(result["provider"])

        self.push_screen(ProviderListScreen(self._config_mgr), on_provider_list_result)

    def _open_provider_edit(self, provider=None) -> None:
        """打开 Provider 编辑表单"""
        from .screens.provider_edit_screen import ProviderEditScreen

        def on_edit_result(saved: bool) -> None:
            if saved:
                asyncio.create_task(self._reload_active_provider_from_config(show_message=True))

        self.push_screen(ProviderEditScreen(self._config_mgr, provider), on_edit_result)

    def _open_skill_marketplace(self) -> None:
        """Open the curated Skill manager from Settings."""
        if not self._skill_manager:
            self._show_command_result("[red]Skill manager is not initialized[/red]")
            return
        from .screens.skill_marketplace_screen import SkillMarketplaceScreen

        self.push_screen(
            SkillMarketplaceScreen(
                self._skill_manager,
                plan_mode=self._plan_mode,
                on_changed=self._rebuild_system_prompt,
            )
        )

    def _open_mcp_marketplace(self) -> None:
        """Open the curated MCP manager from Settings."""
        if not self._mcp_manager:
            self._show_command_result("[red]MCP manager is not initialized[/red]")
            return
        from .screens.mcp_marketplace_screen import McpMarketplaceScreen

        self.push_screen(
            McpMarketplaceScreen(
                self._mcp_manager,
                plan_mode=self._plan_mode,
                on_registry_refresh=self._refresh_mcp_registry,
            )
        )

    async def _refresh_mcp_registry(self) -> None:
        """Replace all live MCP adapters with the currently enabled set."""
        if not self._tool_registry:
            return
        self._tool_registry.unregister_prefix("mcp__")
        await asyncio.to_thread(self._register_mcp_tools, False)
        await self._rebuild_system_prompt()

    def _execute_command(self, cmd: str) -> None:
        """执行选中的命令"""
        if cmd == "/plan":
            asyncio.create_task(self._handle_plan_command())
        elif cmd == "/exit_plan":
            asyncio.create_task(self._handle_exit_plan_command())
        elif cmd == "/loop":
            asyncio.create_task(self._handle_loop_command(""))
        elif cmd.startswith("/loop "):
            asyncio.create_task(self._handle_loop_command(cmd[5:].strip()))
        elif cmd == "/compact":
            asyncio.create_task(self._handle_compact_command())
        elif cmd == "/clear":
            asyncio.create_task(self._handle_clear())
        elif cmd == "/model":
            asyncio.create_task(self._show_model_picker())
        elif cmd == "/context":
            asyncio.create_task(self._show_context_picker())
        elif cmd == "/permission":
            self._show_permission_mode()
        elif cmd == "/memory":
            result = _handle_memory_command(self._memory_manager, "")
            self._show_command_result(result)
        elif cmd == "/skill":
            asyncio.create_task(self._show_async_command_result(self._handle_skill_command("")))
        elif cmd == "/mcp":
            asyncio.create_task(self._show_async_command_result(self._handle_mcp_command("")))
        elif cmd == "/help":
            self._show_help()

    def _show_command_result(self, result: str) -> None:
        try:
            log = self.screen.query_one("#message-log", RichLog)
            self._wl(log, result)
        except Exception:
            pass

    async def _show_async_command_result(self, awaitable) -> None:
        self._show_command_result(await awaitable)
        self._refresh_status_panel()

    def _register_mcp_tools(self, show_errors: bool = True) -> list[str]:
        if not self._mcp_manager or not self._tool_registry:
            return []
        registered = self._mcp_manager.register_enabled_tools(self._tool_registry)
        if show_errors:
            errors = [
                server
                for server in self._mcp_manager.list(enabled_only=True)
                if server.last_error
            ]
            for server in errors:
                self._show_command_result(
                    f"[red]MCP server failed:[/red] {server.name}\n[dim]{server.last_error}[/dim]"
                )
        return registered

    def open_session_from_history(self, path: str) -> None:
        """Load a JSONL session selected from the welcome screen."""
        try:
            session = load_session_from_jsonl(path)
        except Exception as exc:
            self._show_command_result(f"[red]Failed to load session:[/red] {exc}")
            return

        self._session = session
        self._session_mgr.register_session(session)
        self.status_line.cumulative_usage.input_tokens = 0
        self.status_line.cumulative_usage.output_tokens = 0
        self.status_line.cumulative_usage.total_tokens = 0

        try:
            log = self.screen.query_one("#message-log", RichLog)
            log.clear()
            self._render_loaded_session(log, session)
            self.screen.hide_welcome_panel()
        except Exception:
            pass

    def delete_session_from_history(self, path: str) -> None:
        """Delete the history entry chosen from the welcome screen and refresh the list."""
        try:
            session = load_session_from_jsonl(path)
        except Exception:
            session = None

        if not delete_session_record(path):
            self.notify("会话记录删除失败或已不存在", severity="warning")
            return

        if session is not None:
            self._session_mgr.delete_session(session.id)
        if self._session and self._session.history_path == path:
            self._session.history_path = None
        self.notify("会话记录已删除")
        self._show_welcome_message()

    def _render_loaded_session(self, log: RichLog, session: Session) -> None:
        if not session.messages:
            log.write("[dim]Loaded empty session.[/dim]")
            return

        for message in session.messages:
            if message.role == "user":
                log.write(f"\n[bold cyan]You:[/bold cyan] {message.content or ''}")
            elif message.role == "assistant":
                if message.content:
                    log.write(Markdown(message.content))
                elif message.tool_calls:
                    names = ", ".join(tool_call.name for tool_call in message.tool_calls)
                    log.write(f"[dim]Assistant requested tools: {names}[/dim]")
            elif message.role == "tool":
                preview = (message.content or "").strip().replace("\n", " ")
                if len(preview) > 180:
                    preview = preview[:177] + "..."
                log.write(Text(f"Tool result: {preview}", style="dim"))

    def _refresh_status_panel(self) -> None:
        try:
            status = self.screen.query_one("#status-panel", StatusPanel)
            status.refresh()
        except Exception:
            pass

    def action_copy_selected(self) -> None:
        """Ctrl+C 复制当前选中文本，不触发退出/中断。"""
        try:
            prompt = self.screen.query_one("#prompt-input", PromptTextArea)
            selected_text = prompt.selected_text
            if selected_text:
                self.copy_to_clipboard(selected_text)
                return
        except Exception:
            pass
        try:
            header = self.screen.query_one(CustomHeader)
            selected_text = header.get_selected_text()
            if selected_text:
                self.copy_to_clipboard(selected_text)
                return
        except Exception:
            pass
        try:
            log = self.screen.query_one("#message-log", RichLog)
            selected_text = log.get_selected_text()
            if selected_text:
                self.copy_to_clipboard(selected_text)
                return
        except Exception:
            pass

    async def on_key(self, event: events.Key) -> None:
        if event.key == "ctrl+c":
            event.prevent_default()
            event.stop()
            self.action_copy_selected()
            return
        if self._route_key_to_prompt_if_needed(event):
            return

    def _focus_prompt_input(self) -> PromptTextArea | None:
        try:
            prompt = self.screen.query_one("#prompt-input", PromptTextArea)
            prompt.focus()
            return prompt
        except Exception:
            return None

    def _route_key_to_prompt_if_needed(self, event: events.Key) -> bool:
        """Recover prompt input if focus drifts to log/history widgets."""
        try:
            prompt = self.screen.query_one("#prompt-input", PromptTextArea)
        except Exception:
            return False
        if self.focused is prompt or prompt.disabled:
            return False

        # 焦点在额外交流窗口的输入框时，按键归它自己处理，勿抢到主输入框
        if isinstance(self.focused, CommInput):
            return False

        if self._interactive_mode in ("model_picker", "context_picker", "queue"):
            return False
        if self._interactive_mode == "question":
            if not (self._plan_panel and self._plan_panel._is_other_selected):
                return False

        if event.key == "enter":
            text = prompt.text.strip()
            if not text:
                return False
            event.prevent_default()
            event.stop()
            prompt.focus()
            prompt.post_message(PromptTextArea.Submitted(text))
            return True

        character = getattr(event, "character", None)
        if character is None and event.key == "space":
            character = " "
        if not character or event.key in {"tab", "escape"} or "+" in event.key:
            return False

        event.prevent_default()
        event.stop()
        prompt.focus()
        try:
            prompt._replace_via_keyboard(character, *prompt.selection)
        except Exception:
            prompt.text = f"{prompt.text}{character}"
        return True

    def action_cycle_permission_mode(self) -> None:
        self._permission_system.cycle_mode()
        self.status_line.set_permission_label(self._permission_system.get_mode_label())
        self._refresh_status_panel()
        self._show_command_result(
            f"[bold yellow]Permission mode:[/bold yellow] "
            f"{self._permission_system.get_mode_label()}\n"
            f"[dim]{self._permission_system.get_mode_description()}[/dim]"
        )
        asyncio.create_task(self._rebuild_system_prompt())

    def _show_permission_mode(self) -> None:
        self._show_command_result(
            "[bold cyan]Permission modes[/bold cyan]\n\n"
            f"Current: [bold yellow]{self._permission_system.get_mode_label()}[/bold yellow]\n"
            f"[dim]{self._permission_system.get_mode_description()}[/dim]\n\n"
            "[dim]Press Shift+Tab or run /permission next to cycle.[/dim]"
        )

    async def _handle_skill_command(self, args: str) -> str:
        if not self._skill_manager:
            return "[red]Skill manager is not initialized[/red]"
        parts = shlex.split(args) if args else []
        subcmd = parts[0].lower() if parts else "list"
        readonly = {"list", "info"}
        if self._plan_mode and subcmd not in readonly:
            return "[red]Plan Mode is active: /skill can only list or show info.[/red]"

        try:
            if subcmd == "list":
                skills = self._skill_manager.list()
                if not skills:
                    return "[dim]No skills installed[/dim]"
                lines = [f"[bold cyan]Skills ({len(skills)}):[/bold cyan]"]
                for skill in skills:
                    marker = "[green]enabled[/green]" if skill.enabled else "[dim]disabled[/dim]"
                    desc = f" - {skill.description}" if skill.description else ""
                    lines.append(f"  [bold]{skill.name}[/bold] {marker}{desc}")
                return "\n".join(lines)

            if subcmd == "install":
                if len(parts) < 2:
                    return "[red]Usage: /skill install <local-path|github-url> [--name name][/red]"
                source = parts[1]
                name = _option_value(parts, "--name")
                skill = await asyncio.to_thread(self._skill_manager.install, source, name)
                await self._rebuild_system_prompt()
                return f"[bold green]Skill installed:[/bold green] {skill.name}"

            if subcmd in {"enable", "disable"}:
                if len(parts) < 2:
                    return f"[red]Usage: /skill {subcmd} <name>[/red]"
                skill = self._skill_manager.enable(parts[1], enabled=(subcmd == "enable"))
                await self._rebuild_system_prompt()
                state = "enabled" if skill.enabled else "disabled"
                return f"[bold green]Skill {state}:[/bold green] {skill.name}"

            if subcmd == "remove":
                if len(parts) < 2:
                    return "[red]Usage: /skill remove <name>[/red]"
                skill = await asyncio.to_thread(self._skill_manager.remove, parts[1])
                await self._rebuild_system_prompt()
                return f"[bold green]Skill removed:[/bold green] {skill.name}"

            if subcmd == "update":
                if len(parts) < 2:
                    return "[red]Usage: /skill update <name>[/red]"
                skill = await asyncio.to_thread(self._skill_manager.update, parts[1])
                await self._rebuild_system_prompt()
                return f"[bold green]Skill updated:[/bold green] {skill.name}"

            if subcmd == "info":
                if len(parts) < 2:
                    return "[red]Usage: /skill info <name>[/red]"
                return self._skill_manager.info(parts[1])

            return "[red]Unknown /skill command. Use /skill list|install|enable|disable|remove|update|info[/red]"
        except (SkillInstallError, OSError) as exc:
            return f"[red]Skill error:[/red] {exc}"

    async def _handle_mcp_command(self, args: str) -> str:
        if not self._mcp_manager:
            return "[red]MCP manager is not initialized[/red]"
        parts = shlex.split(args) if args else []
        subcmd = parts[0].lower() if parts else "list"
        readonly = {"list", "info", "tools"}
        if self._plan_mode and subcmd not in readonly:
            return "[red]Plan Mode is active: /mcp can only list, show info, or show tools.[/red]"

        try:
            if subcmd == "list":
                servers = self._mcp_manager.list()
                if not servers:
                    return "[dim]No MCP servers configured[/dim]"
                lines = [f"[bold cyan]MCP servers ({len(servers)}):[/bold cyan]"]
                for server in servers:
                    marker = "[green]enabled[/green]" if server.enabled else "[dim]disabled[/dim]"
                    err = f" [red]error: {server.last_error}[/red]" if server.last_error else ""
                    lines.append(f"  [bold]{server.name}[/bold] {marker} ({server.transport}){err}")
                return "\n".join(lines)

            if subcmd == "add":
                if len(parts) < 4 or parts[2].lower() not in {"stdio", "http", "sse"}:
                    return "[red]Usage: /mcp add <name> stdio <command> [args...] | /mcp add <name> http|sse <url>[/red]"
                transport = parts[2].lower()
                if transport == "stdio":
                    server = self._mcp_manager.add_stdio(parts[1], parts[3], args=parts[4:])
                elif transport == "http":
                    server = self._mcp_manager.add_http(parts[1], parts[3])
                else:
                    server = self._mcp_manager.add_sse(parts[1], parts[3])
                return f"[bold green]MCP server added:[/bold green] {server.name}"

            if subcmd in {"enable", "disable"}:
                if len(parts) < 2:
                    return f"[red]Usage: /mcp {subcmd} <name>[/red]"
                server = self._mcp_manager.enable(parts[1], enabled=(subcmd == "enable"))
                if server.enabled:
                    await asyncio.to_thread(self._register_mcp_tools, False)
                elif self._tool_registry:
                    self._tool_registry.unregister_prefix(f"mcp__{server.name}__")
                state = "enabled" if server.enabled else "disabled"
                return f"[bold green]MCP server {state}:[/bold green] {server.name}"

            if subcmd == "remove":
                if len(parts) < 2:
                    return "[red]Usage: /mcp remove <name>[/red]"
                server = self._mcp_manager.remove(parts[1])
                if self._tool_registry:
                    self._tool_registry.unregister_prefix(f"mcp__{server.name}__")
                return f"[bold green]MCP server removed:[/bold green] {server.name}"

            if subcmd == "test":
                if len(parts) < 2:
                    return "[red]Usage: /mcp test <name>[/red]"
                ok, message = await asyncio.to_thread(self._mcp_manager.test, parts[1])
                if ok:
                    await asyncio.to_thread(self._register_mcp_tools, False)
                    return f"[bold green]MCP connected:[/bold green] {message}"
                return f"[red]MCP test failed:[/red] {message}"

            if subcmd == "tools":
                if len(parts) < 2:
                    return "[red]Usage: /mcp tools <name>[/red]"
                tools = await asyncio.to_thread(self._mcp_manager.list_tools, parts[1])
                if not tools:
                    return "[dim]No tools discovered[/dim]"
                lines = [f"[bold cyan]MCP tools for {parts[1]} ({len(tools)}):[/bold cyan]"]
                for tool in tools:
                    desc = f" - {tool.description}" if tool.description else ""
                    lines.append(f"  [bold]{tool.name}[/bold]{desc}")
                return "\n".join(lines)

            if subcmd == "info":
                if len(parts) < 2:
                    return "[red]Usage: /mcp info <name>[/red]"
                return self._mcp_manager.info(parts[1])

            return "[red]Unknown /mcp command. Use /mcp list|add|enable|disable|remove|test|tools|info[/red]"
        except (McpError, OSError) as exc:
            return f"[red]MCP error:[/red] {exc}"

    async def _handle_auto_config_input(self, text: str) -> str | None:
        """Handle pasted Provider, Skill, or MCP configuration before agent execution."""
        intent = self._input_intent_detector.detect(text)
        if not intent.is_config_intent:
            return None

        if self._plan_mode:
            return self._format_auto_config_plan(intent)

        allowed, denial = await self._confirm_auto_config(intent)
        if not allowed:
            return denial or "[red]Permission denied: auto configuration was not applied.[/red]"

        try:
            if intent.kind == INTENT_PROVIDER:
                if not self._config_mgr:
                    return "[red]Provider config manager is not initialized[/red]"
                result = await asyncio.to_thread(
                    ProviderAutoConfigurator(self._config_mgr).configure,
                    intent.data["config"],
                )
                if result.success:
                    refreshed = await self._reload_active_provider_from_config()
                    if not refreshed:
                        result.message += "\nStatus detail: saved, but runtime refresh failed."
                return result.message

            if intent.kind == INTENT_SKILL:
                if not self._skill_manager:
                    return "[red]Skill manager is not initialized[/red]"
                result = await asyncio.to_thread(
                    SkillAutoInstaller(self._skill_manager).install,
                    intent.data["source"],
                )
                if result.success:
                    await self._rebuild_system_prompt()
                return result.message

            if intent.kind == INTENT_MCP:
                if not self._mcp_manager:
                    return "[red]MCP manager is not initialized[/red]"
                payload = intent.data.get("command_text") or intent.data.get("config")
                result = await asyncio.to_thread(
                    McpAutoConfigurator(self._mcp_manager, self._tool_registry).configure,
                    payload,
                )
                return result.message
        except Exception as exc:
            return f"[red]Auto configuration failed:[/red] {type(exc).__name__}: {exc}"

        return None

    def _format_auto_config_plan(self, intent: InputIntent) -> str:
        lines = [
            "[bold yellow]Plan Mode is active: auto configuration was not applied.[/bold yellow]",
            f"Detected: {intent.detected_as or intent.kind}",
            "Planned action:",
        ]
        if intent.kind == INTENT_PROVIDER:
            try:
                provider = normalize_provider_config(intent.data["config"])
                lines.extend(
                    [
                        "- Add or update the provider in ~/.coomi/config/providers.json.",
                        "- Activate this provider and refresh the running LLM runtime.",
                        f"- Provider: {provider.id}",
                        f"- Model: {provider.model}",
                        f"- Tool protocol: {provider.tool_protocol}",
                        f"- API key: {redact_secret(provider.api_key)}",
                    ]
                )
            except ValueError as exc:
                lines.append(f"- Provider JSON needs repair before it can be applied: {exc}")
        elif intent.kind == INTENT_SKILL:
            lines.extend(
                [
                    "- Install the skill into ~/.coomi/skills/.",
                    "- Repair only the installed copy if metadata or SKILL.md is missing.",
                    "- Enable the skill and rebuild Coomi's skill context.",
                    f"- Source: {intent.data.get('source', '')}",
                ]
            )
        elif intent.kind == INTENT_MCP:
            payload = intent.data.get("command_text") or intent.data.get("config")
            try:
                mcp = normalize_mcp_config(payload)
                lines.extend(
                    [
                        "- Add or update the MCP server in ~/.coomi/config/mcp_servers.json.",
                        "- Enable it, test connectivity, list tools, and register tool adapters.",
                        f"- Name: {mcp['name']}",
                        f"- Transport: {mcp['transport']}",
                    ]
                )
                if mcp["transport"] == "stdio":
                    lines.append(f"- Command: {mcp['command']}")
                    lines.append(f"- Args: {' '.join(mcp.get('args', []))}")
                else:
                    lines.append(f"- URL: {mcp['url']}")
            except ValueError as exc:
                lines.append(f"- MCP config needs repair before it can be applied: {exc}")
        lines.append("Leave Plan Mode with /exit_plan before applying changes.")
        return "\n".join(lines)

    async def _confirm_auto_config(self, intent: InputIntent) -> tuple[bool, str | None]:
        arguments = self._auto_config_permission_arguments(intent)
        level = self._permission_system.check_permission("Config", arguments)
        if level == PermissionLevel.AUTO:
            return True, None
        if level == PermissionLevel.DENY:
            return False, "[red]Permission denied: auto configuration is blocked by policy.[/red]"

        answers = await self._handle_ask_questions(
            [
                {
                    "header": "Permission",
                    "question": self._auto_config_permission_question(intent, arguments),
                    "options": [
                        {
                            "label": "Allow",
                            "value": "allow",
                            "description": (
                                "Coomi will perform the listed write or external actions now. "
                                "Provider secrets are redacted in the prompt, and Skill repairs "
                                "only touch the installed copy or temporary clone."
                            ),
                            "is_recommended": True,
                        },
                        {
                            "label": "Deny",
                            "value": "deny",
                            "description": (
                                "Coomi will leave providers.json, skills.json, MCP settings, "
                                "installed skills, and tool registration unchanged."
                            ),
                        },
                    ],
                }
            ]
        )
        if answers.get("__cancelled__"):
            return False, "[red]Permission request cancelled: auto configuration was not applied.[/red]"
        if answers.get(0, {}).get("option") == "allow":
            return True, None
        return False, "[red]Permission denied: auto configuration was not applied.[/red]"

    def _auto_config_permission_question(self, intent: InputIntent, arguments: dict[str, Any]) -> str:
        lines = [
            f"Allow Coomi to apply detected {intent.detected_as or intent.kind}?",
            f"Action: {arguments.get('action', 'configure')}",
        ]
        for key in ("provider", "model", "api_key", "source", "name", "transport", "url", "command", "args"):
            value = arguments.get(key)
            if value not in (None, "", []):
                rendered = " ".join(value) if isinstance(value, list) else str(value)
                lines.append(f"{key}: {rendered}")
        return "\n".join(lines)

    def _auto_config_permission_arguments(self, intent: InputIntent) -> dict[str, Any]:
        if intent.kind == INTENT_PROVIDER:
            try:
                provider = normalize_provider_config(intent.data["config"])
                return {
                    "action": "write Provider config and activate LLM",
                    "provider": provider.id,
                    "model": provider.model,
                    "tool_protocol": provider.tool_protocol,
                    "api_key": redact_secret(provider.api_key),
                }
            except ValueError:
                return {"action": "attempt Provider config validation", "kind": intent.kind}

        if intent.kind == INTENT_SKILL:
            return {
                "action": "install, repair, and enable Skill",
                "source": intent.data.get("source", ""),
            }

        if intent.kind == INTENT_MCP:
            payload = intent.data.get("command_text") or intent.data.get("config")
            try:
                mcp = normalize_mcp_config(payload)
                return {
                    "action": "write, enable, test, and register MCP tools",
                    "name": mcp["name"],
                    "transport": mcp["transport"],
                    "url": mcp.get("url", ""),
                    "command": mcp.get("command", ""),
                    "args": mcp.get("args", []),
                }
            except ValueError:
                return {"action": "attempt MCP config validation", "kind": intent.kind}

        return {"action": "auto configure", "kind": intent.kind}

    async def _handle_plan_command(self) -> None:
        self._plan_mode = True
        if self._agent:
            self._agent.set_plan_mode(True)
        try:
            status = self.screen.query_one("#status-panel", StatusPanel)
            status.set_plan_mode(True)
        except Exception:
            pass
        self._show_command_result("[bold yellow]⚡ Plan Mode activated[/bold yellow]")
        await self._rebuild_system_prompt()

    async def _handle_exit_plan_command(self) -> None:
        self._plan_mode = False
        if self._agent:
            self._agent.set_plan_mode(False)
        try:
            status = self.screen.query_one("#status-panel", StatusPanel)
            status.set_plan_mode(False)
        except Exception:
            pass
        self._show_command_result("[dim]Plan Mode deactivated[/dim]")
        await self._rebuild_system_prompt()

    async def _handle_loop_command(self, args: str) -> None:
        """处理 /loop 命令"""
        from ..engine.spec_parser import parse_spec_file

        if args in ("status", "pause", "resume", "stop"):
            if args == "status":
                if self._loop_session:
                    ls = self._loop_session
                    self._show_command_result(
                        f"[bold cyan]Loop Status:[/bold cyan] {ls.status.value}\n"
                        f"  Task: {ls.spec.title}\n"
                        f"  Progress: Step {ls.current_step + 1}/{len(ls.spec.steps)}\n"
                        f"  ID: {ls.loop_id}"
                    )
                else:
                    self._show_command_result("[dim]No active loop[/dim]")
            elif args == "pause":
                if self._loop_runner:
                    self._loop_runner.cancel_token.cancel()
                    self._show_command_result("[yellow]Loop paused[/yellow]")
                else:
                    self._show_command_result("[dim]No active loop[/dim]")
            elif args == "resume":
                self._show_command_result("[dim]Loop resume not yet implemented[/dim]")
            elif args == "stop":
                if self._loop_runner:
                    self._loop_runner.cancel_token.cancel()
                    self._loop_mode = False
                    self._loop_session = None
                    self._loop_runner = None
                    try:
                        status = self.screen.query_one("#status-panel", StatusPanel)
                        status.set_loop_mode(False)
                        status.set_plan_mode(self._plan_mode)
                    except Exception:
                        pass
                    self._show_command_result("[red]Loop stopped[/red]")
            return

        # 启动 loop
        spec_path = args if args else None
        spec = None

        if spec_path:
            try:
                spec = parse_spec_file(spec_path)
            except FileNotFoundError:
                self._show_command_result(f"[red]Spec file not found: {spec_path}[/red]")
                return
            except Exception as e:
                self._show_command_result(f"[red]Failed to parse spec: {e}[/red]")
                return
        else:
            # 无 spec — 进入 plan 模式创建
            self._show_command_result(
                "[bold yellow]⚡ Loop Mode — No spec provided[/bold yellow]\n"
                "[dim]Describe your task, and I'll help you create a spec first.[/dim]\n"
                "[dim]Or provide a spec path: /loop path/to/spec.md[/dim]"
            )
            await self._handle_plan_command()
            return

        await self._start_loop(spec)

    async def _start_loop(self, spec) -> None:
        """启动 loop 执行"""
        from ..engine.loop_runner import LoopRunner

        self._loop_runner = LoopRunner(
            llm=self._agent.llm,
            tool_registry=self._tool_registry,
            context_window_size=self._agent.context_window_size,
            app_context=self,
            permission_system=self._permission_system,
            hook_system=self._hook_system,
        )
        self._loop_mode = True
        try:
            status = self.screen.query_one("#status-panel", StatusPanel)
            status.set_loop_mode(True, total_steps=len(spec.steps))
        except Exception:
            pass

        self._show_command_result(
            f"[bold green]🔁 Loop Mode Started[/bold green]\n"
            f"  Task: {spec.title}\n"
            f"  Steps: {len(spec.steps)}\n"
            f"[dim]Use /loop status | /loop pause | /loop stop[/dim]"
        )

        # Run loop in background
        asyncio.create_task(self._run_loop(spec))

    async def _run_loop(self, spec) -> None:
        """后台执行 loop"""
        log = self.screen.query_one("#message-log", RichLog)
        status = self.screen.query_one("#status-panel", StatusPanel)
        preview = self.screen.query_one("#stream-preview", StreamingPreview)

        self._agent_running = True

        def on_state_change(ls):
            self._loop_session = ls

        try:
            async for event in self._loop_runner.start_loop(
                cwd=self._cwd,
                spec=spec,
                memory_manager=self._memory_manager,
                memory_recall=self._memory_recall,
                skill_manager=self._skill_manager,
                display_name=self._display_name,
                on_state_change=on_state_change,
            ):
                if isinstance(event, LoopStepStart):
                    self._wl(log, (
                        f"\n[bold yellow]━━━ Step {event.step_index + 1}/{event.total_steps} "
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━[/bold yellow]\n"
                        f"[bold]{event.step_description}[/bold]"
                    ))

                elif isinstance(event, LoopStepDone):
                    if event.success:
                        self._wl(log, f"[green]✅ Step {event.step_index + 1} complete[/green]")
                    else:
                        self._wl(log, f"[yellow]⚠️ Step {event.step_index + 1} skipped[/yellow]")

                elif isinstance(event, LoopIssueCreated):
                    self._wl(log, (
                        f"[red]⚠️ ISSUE created for Step {event.step_index + 1}[/red] "
                        f"[dim]See .coomi/loops/{self._loop_session.loop_id}/ISSUE.md[/dim]"
                    ))

                elif isinstance(event, LoopProgress):
                    try:
                        status.set_loop_progress(event.current_step, event.total_steps)
                    except Exception:
                        pass

                elif isinstance(event, TextChunk):
                    self._stream_buffer += event.content
                    preview.show_text(self._stream_buffer)

                elif isinstance(event, UsageUpdate):
                    self.status_line.update_usage(event.usage)
                    status.refresh()

                elif isinstance(event, AgentError):
                    self._wl(log, f"\n[red]Loop error: {event.message}[/red]")

                elif isinstance(event, AgentCancelled):
                    self._wl(log, "\n[dim]Loop cancelled[/dim]")
                    break

        except Exception as e:
            self._wl(log, f"\n[red]Loop crashed: {e}[/red]")
        finally:
            self._agent_running = False
            self._loop_mode = False
            self._loop_runner = None
            status.set_loop_mode(False)
            status.set_plan_mode(self._plan_mode)
            status.set_idle()
            preview.clear_preview()
            if self._loop_session and self._loop_session.status.value == "completed":
                self._wl(log, f"\n[bold green]🎉 Loop Complete: {spec.title}[/bold green]")

    async def _rebuild_system_prompt(self) -> None:
        """立即重建 system prompt，使当前 agent 轮次看到最新指令"""
        if not self._session:
            return
        self._session.system_prompt = await build_system_prompt(
            memory_manager=self._memory_manager,
            memory_recall=self._memory_recall,
            skill_manager=self._skill_manager,
            current_context="",
            cwd=self._cwd,
            model_display=self._display_name,
            plan_mode=self._plan_mode,
            permission_mode=self._permission_system.mode.value,
        )

    async def _handle_compact_command(self) -> None:
        if not self._session or not self._agent:
            self._show_command_result("[dim]No active session[/dim]")
            return
        before = len(self._session.messages)
        self._show_command_result("[dim]Compressing context...[/dim]")
        try:
            compressed = await self._agent.compressor.compress(
                self._session,
                self._agent.context_window_size,
                force=True,
            )
            self._session.messages = compressed
            self._show_command_result(
                f"[dim]Context compressed: {before} -> {len(compressed)} messages[/dim]"
            )
        except Exception as e:
            self._show_command_result(f"[red]Compact failed: {e}[/red]")

    def _show_help(self) -> None:
        help_text = (
            "[bold cyan]Coomi Agent Commands[/bold cyan]\n\n"
            "  [bold]/plan[/bold]          进入 Plan Mode\n"
            "  [bold]/exit_plan[/bold]     退出 Plan Mode\n"
            "  [bold]/loop[/bold]          长线任务执行模式\n"
            "  [bold]/model[/bold]         切换 LLM 模型\n"
            "  [bold]/context[/bold]       设置上下文窗口大小\n"
            "  [bold]/permission[/bold]    查看/切换工具权限模式\n"
            "  [bold]/memory[/bold]        记忆管理\n"
            "  [bold]/skill[/bold]         Skill 扩展管理\n"
            "  [bold]/mcp[/bold]           MCP Server 管理\n"
            "  [bold]/compact[/bold]       压缩上下文\n"
            "  [bold]/clear[/bold]         清空会话历史\n"
            "  [bold]/help[/bold]          显示此帮助\n\n"
            "[dim]快捷键: Ctrl+P 命令面板 | F2 Home | F3 Setting | Ctrl+R 切换推理 | "
            "Shift+Tab 权限模式 | 双 Esc 退出[/dim]\n"
            "[dim]执行中: Esc 取消 | Ctrl+G 整理待执行队列（插队/置顶/编辑/删除） | "
            "Ctrl+T 跳转下方交流窗口[/dim]"
        )
        self._show_command_result(help_text)

    # -- Plan Mode: AskUserQuestion ----------------------------------------

    def _set_interactive_mode(self, mode: str) -> None:
        """统一设置交互模式，同步旧布尔标志"""
        self._interactive_mode = mode
        self._command_mode = (mode == "command")
        self._question_mode = (mode == "question")
        # 队列整理期间关闭排空闸门，避免正整理时流结束突然 pop 执行
        if mode == "queue":
            self._queue_drain_gate.clear()
        else:
            self._queue_drain_gate.set()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        """条件路由：统一基于 _interactive_mode 拦截 keys，否则放行给 TextArea"""
        if action == "insert_prompt_newline":
            try:
                prompt = self.screen.query_one("#prompt-input", PromptTextArea)
            except Exception:
                return False
            return self.focused is prompt and not prompt.disabled
        question_actions = {
            "question_up", "question_down", "question_left",
            "question_right", "question_toggle", "question_confirm",
            "question_cancel_or_exit",
        }
        if action not in question_actions:
            return True

        mode = self._interactive_mode

        # 指令模式：拦截 ↑↓ 和 Enter，←→ 放行
        if mode == "command":
            if action in ("question_up", "question_down", "question_confirm"):
                return True
            return None

        # 模型选择器：拦截 ↑↓←→ 和 Enter
        if mode == "model_picker":
            if action in ("question_up", "question_down", "question_left",
                          "question_right", "question_confirm"):
                return True
            if action == "question_cancel_or_exit":
                return True
            return None

        # 上下文选择器：拦截 ↑↓ 和 Enter
        if mode == "context_picker":
            if action in ("question_up", "question_down", "question_confirm"):
                return True
            if action == "question_cancel_or_exit":
                return True
            return None

        # 队列选择模式：拦截 ↑↓←→ / Enter / Esc
        if mode == "queue":
            if action in ("question_up", "question_down", "question_left",
                          "question_right", "question_confirm",
                          "question_cancel_or_exit"):
                return True
            return None

        if mode == "none" and self._welcome_panel_active() and not self._get_prompt_text():
            if action in (
                "question_up",
                "question_down",
                "question_left",
                "question_right",
                "question_confirm",
            ):
                return True

        # 问询模式
        if mode == "question":
            if action == "question_toggle":
                if self._plan_panel and self._plan_panel._is_other_selected:
                    return None
                return True
            # Other 选中且无内容时，阻止左右键切换问题
            if action in ("question_left", "question_right") and self._plan_panel:
                if self._plan_panel._is_other_selected:
                    if not self._plan_panel._other_texts.get(
                        self._plan_panel._active_q, ""
                    ):
                        return False
            return True

        # 无交互模式 → 放行给 TextArea
        return None

    async def action_question_up(self) -> None:
        if self._interactive_mode == "none" and self._welcome_panel_active() and not self._get_prompt_text():
            self.screen.welcome_panel.move_session_selection(-1)
        elif self._interactive_mode == "command" and self._command_list:
            self._command_list.move_up()
        elif self._interactive_mode == "question" and self._plan_panel:
            self._plan_panel.move_up()
        elif self._interactive_mode == "model_picker" and self._model_picker:
            self._model_picker.move_up()
        elif self._interactive_mode == "context_picker" and self._context_picker:
            self._context_picker.move_up()
        elif self._interactive_mode == "queue":
            self._queue_move_selection(-1)

    async def action_question_down(self) -> None:
        if self._interactive_mode == "none" and self._welcome_panel_active() and not self._get_prompt_text():
            self.screen.welcome_panel.move_session_selection(1)
        elif self._interactive_mode == "command" and self._command_list:
            self._command_list.move_down()
        elif self._interactive_mode == "question" and self._plan_panel:
            self._plan_panel.move_down()
        elif self._interactive_mode == "model_picker" and self._model_picker:
            self._model_picker.move_down()
        elif self._interactive_mode == "context_picker" and self._context_picker:
            self._context_picker.move_down()
        elif self._interactive_mode == "queue":
            self._queue_move_selection(1)

    async def action_question_toggle(self) -> None:
        if self._interactive_mode == "question" and self._plan_panel:
            self._plan_panel.toggle_current_option()

    def action_insert_prompt_newline(self) -> None:
        """Insert a newline in the main prompt without submitting it."""
        try:
            prompt = self.screen.query_one("#prompt-input", PromptTextArea)
        except Exception:
            return
        if self.focused is prompt and not prompt.disabled:
            prompt.action_insert_newline()

    async def action_question_left(self) -> None:
        if self._interactive_mode == "none" and self._welcome_panel_active() and not self._get_prompt_text():
            self.screen.welcome_panel.move_session_action(-1)
        elif self._interactive_mode == "question" and self._plan_panel:
            self._plan_panel.prev_question()
        elif self._interactive_mode == "model_picker" and self._model_picker:
            self._model_picker.toggle_mode_left()
        elif self._interactive_mode == "queue":
            self._queue_move_action(-1)

    async def action_question_right(self) -> None:
        if self._interactive_mode == "none" and self._welcome_panel_active() and not self._get_prompt_text():
            self.screen.welcome_panel.move_session_action(1)
        elif self._interactive_mode == "question" and self._plan_panel:
            self._plan_panel.next_question()
        elif self._interactive_mode == "model_picker" and self._model_picker:
            self._model_picker.toggle_mode_right()
        elif self._interactive_mode == "queue":
            self._queue_move_action(1)

    def _get_prompt_text(self) -> str:
        try:
            textarea = self.screen.query_one("#prompt-input", PromptTextArea)
            return textarea.text.strip()
        except Exception:
            return ""

    def _clear_prompt(self) -> None:
        try:
            textarea = self.screen.query_one("#prompt-input", PromptTextArea)
            textarea.clear()
        except Exception:
            pass

    def _welcome_panel_active(self) -> bool:
        try:
            panel = self.screen.welcome_panel
            return bool(panel.display and panel.has_sessions())
        except Exception:
            return False

    def _is_welcome_visible(self) -> bool:
        try:
            return bool(self.screen.welcome_panel.display)
        except Exception:
            return False

    async def action_question_confirm(self) -> None:
        if self._interactive_mode == "queue":
            await self._queue_confirm_action()
            return

        if self._interactive_mode == "none" and self._welcome_panel_active() and not self._get_prompt_text():
            self.screen.welcome_panel.confirm_selected_session()
            return

        # 指令模式 → 执行选中指令
        if self._interactive_mode == "command" and self._command_list:
            current_text = self._get_prompt_text().strip()
            if self._is_complete_extension_command(current_text):
                self._hide_command_list()
                self._clear_prompt()
                self._ensure_new_session_for_welcome_input()
                asyncio.create_task(self._run_agent_async(current_text))
                return
            item = self._command_list.get_selected_item()
            if item:
                cmd, _desc, kind = item
                if kind in {"skill", "mcp", "mcp_action"}:
                    prompt = self.screen.query_one("#prompt-input", PromptTextArea)
                    prompt.text = cmd + " "
                    prompt.move_cursor(prompt.document.end)
                    if kind == "mcp":
                        self._command_list.set_filter(cmd + " ")
                    else:
                        self._hide_command_list()
                    return
                self._hide_command_list()
                self._clear_prompt()
                self._execute_command(cmd)
            return

        # 模型选择器 → 确认选择
        if self._interactive_mode == "model_picker" and self._model_picker:
            provider, mode = self._model_picker.confirm()
            self._hide_model_picker()
            self._apply_model_selection(provider, mode)
            return

        # 上下文选择器 → 确认选择
        if self._interactive_mode == "context_picker" and self._context_picker:
            size = self._context_picker.confirm()
            self._hide_context_picker()
            self._apply_context_selection(size)
            return

        if not (self._interactive_mode == "question" and self._plan_panel):
            return
        panel = self._plan_panel

        # Other 选中 + 输入框有文本 → 采纳为 other_text，留在当前问题
        if panel._is_other_selected:
            input_text = self._get_prompt_text()
            if input_text:
                panel.set_other_text(input_text)
                self._clear_prompt()
                return
            # 输入框空 + other_text 无内容 → 阻止前进
            if not panel._other_texts.get(panel._active_q, ""):
                return

        # 普通确认逻辑
        if panel.is_last_question:
            answers = panel.get_all_answers()
            if self._question_future:
                self._question_future.set_result(answers)
        else:
            panel.next_question()

    def action_question_cancel_or_exit(self) -> None:
        # 队列选择模式 → 退出整理
        if self._interactive_mode == "queue":
            self._exit_queue_mode()
            return
        # 模型选择器 → 关闭 picker
        if self._interactive_mode == "model_picker":
            self._hide_model_picker()
            return
        # 上下文选择器 → 关闭 picker
        if self._interactive_mode == "context_picker":
            self._hide_context_picker()
            return
        # 指令列表 → 关闭
        if self._interactive_mode == "command":
            self._hide_command_list()
            self._clear_prompt()
            return
        if self._interactive_mode == "question" and self._plan_panel:
            if self._exit_pending:
                if self._question_future:
                    self._question_future.set_result({"__cancelled__": True})
            else:
                self._exit_pending = True
                self._show_command_result("[dim]再按一次 Esc 取消问询[/dim]")
                self._exit_timer = self.set_timer(2.0, self._reset_exit_pending)
        else:
            self.action_cancel_or_exit()

    # -- Pending queue (待执行队列) -----------------------------------------

    def _refresh_queue_panel(self) -> None:
        """同步队列面板显示。"""
        try:
            panel = self.screen.query_one("#pending-queue", PendingQueuePanel)
        except Exception:
            return
        panel.update_state(
            self._pending_queue,
            selecting=(self._interactive_mode == "queue"),
            msg_index=self._queue_msg_index,
            action_index=self._queue_action_index,
        )

    def _enqueue_pending(self, text: str) -> None:
        """执行过程中追加的普通文本进入待执行队列。"""
        self._pending_queue.append(text)
        self._refresh_queue_panel()

    def action_enter_queue_mode(self) -> None:
        """Ctrl+G：进入队列选择模式。"""
        # 队列为空 → 提示且不进入
        if not self._pending_queue:
            self._show_command_result("[dim]待执行队列为空[/dim]")
            return
        # 仅在无其他交互模式时进入
        if self._interactive_mode not in ("none",):
            return
        self._queue_msg_index = 0
        self._queue_action_index = 0
        self._queue_drain_gate.clear()      # 整理期间暂停排空
        self._set_interactive_mode("queue")
        self._refresh_queue_panel()
        # 右上角亮起 QUEUE 徽章
        try:
            self.screen.query_one("#status-panel", StatusPanel).set_queue_mode(True)
        except Exception:
            pass
        # 输入框失焦，交由面板接管方向键（避免草稿被污染）
        try:
            self.screen.query_one("#pending-queue", PendingQueuePanel).focus()
        except Exception:
            pass

    def _exit_queue_mode(self) -> None:
        """退出队列选择模式，焦点回输入框。"""
        self._set_interactive_mode("none")
        self._refresh_queue_panel()
        # 熄灭 QUEUE 徽章
        try:
            self.screen.query_one("#status-panel", StatusPanel).set_queue_mode(False)
        except Exception:
            pass
        try:
            self.screen.query_one("#prompt-input", PromptTextArea).focus()
        except Exception:
            pass

    def action_toggle_comm_focus(self) -> None:
        """Ctrl+T：开关额外交流窗口。

        窗口默认隐藏。按 Ctrl+T 打开并聚焦交流输入框；再按 Ctrl+T 关闭
        并把焦点交回主输入框。与 agent 是否执行无关。打开后鼠标点击可在
        主输入框 / 交流区之间切换焦点。
        """
        # 队列/问询等交互模式下不抢焦点
        if self._interactive_mode != "none":
            return
        try:
            comm = self.screen.query_one("#comm-panel", CommPanel)
        except Exception:
            return
        if comm.is_open:
            # 关闭 → 焦点交回主输入框
            comm.close_panel()
            try:
                self.screen.query_one("#prompt-input", PromptTextArea).focus()
            except Exception:
                pass
        else:
            # 打开 → 聚焦交流输入框
            comm.open_panel()

    def _queue_move_selection(self, delta: int) -> None:
        """↑↓ 切换选中的消息（钳位）。"""
        if not self._pending_queue:
            return
        n = len(self._pending_queue)
        self._queue_msg_index = max(0, min(n - 1, self._queue_msg_index + delta))
        self._refresh_queue_panel()

    def _queue_move_action(self, delta: int) -> None:
        """←→ 切换动作（循环）。"""
        n = len(QUEUE_ACTIONS)
        self._queue_action_index = (self._queue_action_index + delta) % n
        self._refresh_queue_panel()

    async def _queue_confirm_action(self) -> None:
        """Enter：对选中消息执行选中动作。"""
        if not self._pending_queue:
            self._exit_queue_mode()
            return
        idx = max(0, min(len(self._pending_queue) - 1, self._queue_msg_index))
        action = QUEUE_ACTIONS[self._queue_action_index]

        if action == "插队":
            # 移除该条 → 立即引导（复用 cancel_token input_buffer）
            text = self._pending_queue.pop(idx)
            self._queue_msg_index = 0
            self._exit_queue_mode()
            self._queue_jump_in(text)
            return

        if action == "置顶":
            # 移到队首，留在队列模式继续整理
            text = self._pending_queue.pop(idx)
            self._pending_queue.insert(0, text)
            self._queue_msg_index = 0
            self._refresh_queue_panel()
            return

        if action == "编辑":
            # 取出文本放回输入框，退出队列模式
            text = self._pending_queue.pop(idx)
            self._queue_msg_index = 0
            self._exit_queue_mode()
            self._queue_edit(text)
            return

        if action == "删除":
            self._pending_queue.pop(idx)
            # 索引钳位
            if self._queue_msg_index >= len(self._pending_queue):
                self._queue_msg_index = max(0, len(self._pending_queue) - 1)
            if not self._pending_queue:
                self._exit_queue_mode()
            else:
                self._refresh_queue_panel()
            return

    def _queue_jump_in(self, text: str) -> None:
        """立即引导：强制中断当前流并让下一轮执行 text。

        特例：主任务正处在工具/PowerShell 阻塞窗口时，LLM 闲置，不中断主流，
        改为在独立只读旁路会话上并发执行这条引导内容——主任务工具返回后照常接续。
        """
        if self._agent_running and self._tool_executing:
            self._start_side_conversation(text)
            return
        if self._agent_running:
            # 运行中 → 塞 buffer + cancel，循环取消后捡起它继续
            self._cancel_requested = True
            self._agent.cancel_token.set_input_buffer(text)
            self._agent.cancel_token.cancel()
        else:
            # 空闲 → 直接起 run
            import asyncio
            asyncio.create_task(self._show_auto_config_or_run_agent(text))

    # -- 并发只读交流（工具阻塞窗口内利用闲置 LLM）--------------------------

    def _start_side_conversation(self, text: str) -> None:
        """在工具阻塞窗口内，起一个独立只读旁路会话并发执行 text。

        同一时刻只允许一个 side task 在跑；若已有在跑，则把新内容排入
        `_side_pending`，当前 side 完成后接着跑（仍在阻塞窗口内则继续，
        窗口已关闭则转入 `_comm_queue` 走轮末）。
        """
        if self._side_task is not None and not self._side_task.done():
            self._side_pending.append(text)
            return
        self._side_task = asyncio.create_task(self._run_side_conversation(text))

    async def _run_side_conversation(self, text: str) -> None:
        """执行一次 side 交流，并在结束后消化 `_side_pending` 队列。"""
        from ..engine.side_session import run_side_conversation

        try:
            comm = self.screen.query_one("#comm-panel", CommPanel)
        except Exception:
            return
        try:
            ctx_window = self.status_line.get_context_window_size()
        except Exception:
            ctx_window = 256_000
        try:
            await run_side_conversation(
                comm,
                self._session,
                self._provider,
                self._tool_registry,
                ctx_window,
                text,
                app_context=self,
                permission_system=self._permission_system,
                hook_system=self._hook_system,
                project_path=self._cwd,
            )
        except Exception:
            pass

        # 消化后续排队内容（仍在阻塞窗口内则继续并发；否则转轮末队列）
        if self._side_pending:
            nxt = self._side_pending.pop(0)
            if self._tool_executing:
                self._side_task = asyncio.create_task(self._run_side_conversation(nxt))
                return
            self._comm_queue.append(nxt)
            self._refresh_comm_title()

    async def _await_side_task(self) -> None:
        """等待正在跑的 side task 收尾（工具已返回，避免两路流同时写 UI）。"""
        task = self._side_task
        if task is not None and not task.done():
            try:
                await task
            except Exception:
                pass

    def _queue_edit(self, text: str) -> None:
        """编辑取回：草稿保留（追加回队尾），取回文本进输入框。"""
        try:
            prompt = self.screen.query_one("#prompt-input", PromptTextArea)
        except Exception:
            return
        draft = prompt.text.strip()
        if draft:
            # 已有草稿 → 追加回队尾，不丢
            self._pending_queue.append(draft)
        prompt.text = text
        try:
            prompt.move_cursor(prompt.document.end)
        except Exception:
            pass
        prompt.focus()
        self._refresh_queue_panel()

    async def _handle_ask_questions(self, questions: list[dict]) -> dict:
        """处理 Agent 发起的多问题问询

        questions: [
            {
                "header": "用户群体",
                "question": "完整问题描述...",
                "recommendation": "推荐: 选项A",
                "options": [{"label": "选项A", "value": "a"}, ...]
            },
        ]

        返回: {0: {"option": "a", "label": "选项A", "other_text": None}, ...}
        """
        from .widgets.plan_panel import PlanPanel

        # 1. 创建面板并 mount 为真实 widget（不是 RichLog 静态渲染）
        panel = PlanPanel(questions)
        try:
            log = self.screen.query_one("#message-log", RichLog)
            await self.screen.mount(panel, before=log)
        except Exception:
            return {"__cancelled__": True}

        # 2. 进入问询模式 + 转移焦点到面板（接收 Other 文本输入）
        self._set_interactive_mode("question")
        self._plan_panel = panel
        panel.focus()

        # 3. 更新状态栏
        try:
            status = self.screen.query_one("#status-panel", StatusPanel)
            status.set_question_mode()
        except Exception:
            pass

        # 4. 阻塞等待用户（无限等待）
        self._question_future = asyncio.get_event_loop().create_future()
        result = await self._question_future

        # 5. 清理：移除面板
        self._set_interactive_mode("none")
        self._plan_panel = None
        try:
            panel.remove()
        except Exception:
            pass
        self._focus_prompt_input()
        try:
            status = self.screen.query_one("#status-panel", StatusPanel)
            status.set_idle()
        except Exception:
            pass
        return result

    # -- slash command inline autocomplete -----------------------------------

    def _on_text_area_changed(self, event) -> None:
        """输入框内容变化 — 斜杠指令筛选"""
        if self._question_mode or self._agent_running:
            return
        if hasattr(event, "text_area") and event.text_area.id != "prompt-input":
            return
        text = event.text_area.text if hasattr(event, "text_area") else ""
        clean_text = strip_sgr_mouse_reports(text)
        if clean_text != text:
            event.text_area.text = clean_text
            event.text_area.move_cursor(event.text_area.document.end)
            text = clean_text
        if text.startswith("/"):
            if not self._command_mode:
                asyncio.create_task(self._show_command_list())
            if self._command_list:
                self._command_list.set_filter(text)
        else:
            if self._command_mode:
                self._hide_command_list()

    async def _show_command_list(self) -> None:
        from .widgets.command_list import CommandList
        commands = self._extension_commands()
        self._command_list = CommandList(commands=commands)
        try:
            log = self.screen.query_one("#message-log", RichLog)
            await self.screen.mount(self._command_list, before=log)
            self._set_interactive_mode("command")
            self._focus_prompt_input()
        except Exception:
            pass

    def _extension_commands(self) -> list[tuple[str, str, str]]:
        from .widgets.command_list import COMMANDS
        commands = [(cmd, desc, "command") for cmd, desc in COMMANDS]
        if self._skill_manager:
            for skill in self._skill_manager.list(enabled_only=True):
                commands.append((f"/skill {skill.name}", skill.description or "激活专业工作方法", "skill"))
        if self._mcp_manager:
            for server in self._mcp_manager.list(enabled_only=True):
                if server.last_error:
                    continue
                base = f"/mcp {server.name}"
                commands.append((base, f"选择 MCP（{server.tools_count} 个工具）", "mcp"))
                for action, desc in self._mcp_actions(server.name):
                    commands.append((f"{base} {action}", desc, "mcp_action"))
        return commands

    @staticmethod
    def _mcp_actions(server_name: str) -> list[tuple[str, str]]:
        if server_name.casefold() == "memory":
            return [("保存信息", "保存长期信息"), ("查询信息", "查询已保存的信息"), ("查看已有信息", "列出已有信息")]
        return [("执行任务", "描述希望该 MCP 完成的具体任务")]

    @staticmethod
    def _is_complete_extension_command(text: str) -> bool:
        parts = text.split(maxsplit=2)
        if len(parts) < 3 or parts[0].casefold() not in {"/skill", "/mcp"}:
            return False
        return bool(parts[1].strip() and parts[2].strip())

    def _hide_command_list(self) -> None:
        if self._command_list:
            try:
                self._command_list.remove()
            except Exception:
                pass
            self._command_list = None
        self._set_interactive_mode("none")
        self._focus_prompt_input()

    # -- model picker -------------------------------------------------------

    async def _show_model_picker(self) -> None:
        from .widgets.model_picker import ModelPicker
        self._config_mgr.reload()
        providers = self._config_mgr.list_providers()
        if not providers:
            self._show_command_result("[dim]No providers configured. Edit ~/.coomi/config/providers.json[/dim]")
            return
        self._model_picker = ModelPicker(
            providers,
            active_id=self._active_provider_id,
        )
        try:
            log = self.screen.query_one("#message-log", RichLog)
            await self.screen.mount(self._model_picker, before=log)
            self._set_interactive_mode("model_picker")
            self._clear_prompt()
        except Exception:
            pass

    def _hide_model_picker(self) -> None:
        if self._model_picker:
            try:
                self._model_picker.remove()
            except Exception:
                pass
            self._model_picker = None
        self._set_interactive_mode("none")
        self._focus_prompt_input()

    def _apply_model_selection(self, provider, mode: str) -> None:
        """应用模型选择结果"""
        mode_label = "active (持久)" if mode == "active" else "once_active (仅本次)"
        self._switch_model(provider.id, persist=(mode == "active"), mode_label=mode_label)

    def _switch_model(self, provider_id: str, persist: bool = True, mode_label: str = "active") -> None:
        """切换当前会话模型，并立即刷新状态栏。"""
        if persist and not self._config_mgr.set_active(provider_id):
            self._show_command_result(f"[red]Model not found: {provider_id}[/red]")
            return

        new_provider = get_llm_provider(provider_id)
        self._apply_provider_runtime(new_provider, provider_id, mode_label=mode_label)
        asyncio.create_task(self._rebuild_system_prompt())

    # -- context picker -----------------------------------------------------

    async def _show_context_picker(self) -> None:
        from .widgets.context_picker import ContextPicker
        current_size = self.status_line.get_context_window_size()
        self._context_picker = ContextPicker(current_size=current_size)
        try:
            log = self.screen.query_one("#message-log", RichLog)
            await self.screen.mount(self._context_picker, before=log)
            self._set_interactive_mode("context_picker")
            self._clear_prompt()
        except Exception:
            pass

    def _hide_context_picker(self) -> None:
        if self._context_picker:
            try:
                self._context_picker.remove()
            except Exception:
                pass
            self._context_picker = None
        self._set_interactive_mode("none")
        self._focus_prompt_input()

    def _apply_context_selection(self, size: int) -> None:
        """应用上下文窗口选择结果"""
        self.status_line.set_context_window_size(size)
        if self._agent:
            self._agent.context_window_size = size
        from .status_line import format_token_count
        self._show_command_result(
            f"[bold cyan]Context window set to:[/bold cyan] {format_token_count(size)}"
        )

    # -- input -------------------------------------------------------------

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        user_input = event.value.strip()
        if not user_input:
            return

        event.input.clear()

        if user_input.lower() in ("exit", "quit"):
            self.exit()
            return

        stripped = user_input.strip()

        # --- command dispatch ---
        command_result: str | None = None
        if stripped == "/model":
            import asyncio
            asyncio.create_task(self._show_model_picker())
            return
        elif stripped.startswith("/model "):
            self._switch_model(stripped[6:].strip(), persist=True, mode_label="active")
            return
        elif stripped == "/context":
            import asyncio
            asyncio.create_task(self._show_context_picker())
            return
        elif stripped.startswith("/context "):
            command_result = _handle_context_command(
                self.status_line, self._agent, stripped[8:].strip()
            )
        elif stripped == "/permission":
            self._show_permission_mode()
            return
        elif stripped == "/permission next":
            self.action_cycle_permission_mode()
            return
        elif stripped == "/memory" or stripped.startswith("/memory "):
            command_result = _handle_memory_command(
                self._memory_manager, stripped[7:].strip()
            )
        elif stripped == "/skill" or stripped.startswith("/skill "):
            command_result = await self._handle_skill_command(stripped[6:].strip())
        elif stripped == "/mcp" or stripped.startswith("/mcp "):
            command_result = await self._handle_mcp_command(stripped[4:].strip())
        elif stripped == "/clear":
            await self._handle_clear()
            return
        elif stripped == "/plan":
            await self._handle_plan_command()
            return
        elif stripped == "/exit_plan":
            await self._handle_exit_plan_command()
            return
        elif stripped == "/loop" or stripped.startswith("/loop "):
            await self._handle_loop_command(stripped[5:].strip())
            return
        elif stripped == "/compact":
            asyncio.create_task(self._handle_compact_command())
            return
        elif stripped == "/help":
            self._show_help()
            return

        if command_result is not None:
            self._show_command_result(command_result)
            self._refresh_status_panel()
            return

        auto_config_result = await self._handle_auto_config_input(user_input)
        if auto_config_result is not None:
            self._show_command_result(auto_config_result)
            self._refresh_status_panel()
            return

        # --- agent execution ---
        self._ensure_new_session_for_welcome_input()
        asyncio.create_task(self._run_agent_async(user_input))

    # -- PromptTextArea handling (Enter 发送, Shift+Enter 换行) -------------

    def on_prompt_text_area_submitted(self, event: PromptTextArea.Submitted) -> None:
        """PromptTextArea 提交事件处理"""
        text = event.text
        try:
            textarea = self.screen.query_one("#prompt-input", PromptTextArea)
            textarea.clear()
        except Exception:
            pass
        self._on_text_submit(text)

    def on_comm_input_submitted(self, event: CommInput.Submitted) -> None:
        """交流窗口输入提交。

        分三种情形：
          · 主任务正处在工具/PowerShell 阻塞窗口（LLM 闲置）—— 立即在独立只读
            旁路会话上并发执行，回复直接显示在交流窗回复区，不进队列、不污染主线。
          · 主任务运行中但非阻塞窗口 —— 进入「交流队列」，当前轮次结束后优先插入执行。
          · agent 未运行 —— 无「当前任务」可插入，兜底走正常提交。
        """
        stripped = event.text.strip()
        if not stripped:
            return
        if self._agent_running and self._tool_executing:
            self._start_side_conversation(stripped)
        elif self._agent_running:
            self._comm_queue.append(stripped)
            self._refresh_comm_title()
        else:
            self._on_text_submit(stripped)

    def _refresh_comm_title(self) -> None:
        """同步交流窗口标题里的队列计数。"""
        try:
            comm = self.screen.query_one("#comm-panel", CommPanel)
            comm.set_queue_count(len(self._comm_queue))
        except Exception:
            pass

    def _on_text_submit(self, text: str) -> None:
        """TextArea 提交处理"""
        if text.lower() in ("exit", "quit"):
            self.exit()
            return

        stripped = text.strip()

        # --- command dispatch ---
        command_result: str | None = None
        if stripped == "/model":
            import asyncio
            asyncio.create_task(self._show_model_picker())
            return
        elif stripped.startswith("/model "):
            self._switch_model(stripped[6:].strip(), persist=True, mode_label="active")
            return
        elif stripped == "/context":
            import asyncio
            asyncio.create_task(self._show_context_picker())
            return
        elif stripped.startswith("/context "):
            command_result = _handle_context_command(
                self.status_line, self._agent, stripped[8:].strip()
            )
        elif stripped == "/permission":
            self._show_permission_mode()
            return
        elif stripped == "/permission next":
            self.action_cycle_permission_mode()
            return
        elif stripped == "/memory" or stripped.startswith("/memory "):
            command_result = _handle_memory_command(
                self._memory_manager, stripped[7:].strip()
            )
        elif stripped == "/skill" or stripped.startswith("/skill "):
            import asyncio
            asyncio.create_task(self._show_async_command_result(
                self._handle_skill_command(stripped[6:].strip())
            ))
            return
        elif stripped == "/mcp" or stripped.startswith("/mcp "):
            import asyncio
            asyncio.create_task(self._show_async_command_result(
                self._handle_mcp_command(stripped[4:].strip())
            ))
            return
        elif stripped == "/clear":
            import asyncio
            asyncio.create_task(self._handle_clear())
            return
        elif stripped == "/plan":
            import asyncio
            asyncio.create_task(self._handle_plan_command())
            return
        elif stripped == "/exit_plan":
            import asyncio
            asyncio.create_task(self._handle_exit_plan_command())
            return
        elif stripped == "/loop" or stripped.startswith("/loop "):
            import asyncio
            asyncio.create_task(self._handle_loop_command(stripped[5:].strip()))
            return
        elif stripped == "/compact":
            asyncio.create_task(self._handle_compact_command())
            return
        elif stripped == "/help":
            self._show_help()
            return

        if command_result is not None:
            self._show_command_result(command_result)
            self._refresh_status_panel()
            return

        # 执行中普通文本 → 进入待执行队列，不立即发送
        if self._agent_running:
            self._enqueue_pending(text)
            return

        import asyncio
        asyncio.create_task(self._show_auto_config_or_run_agent(text))

    async def _show_auto_config_or_run_agent(self, text: str) -> None:
        try:
            auto_config_result = await self._handle_auto_config_input(text)
            if auto_config_result is not None:
                self._show_command_result(auto_config_result)
                self._refresh_status_panel()
                return

            # --- agent execution ---
            self._ensure_new_session_for_welcome_input()
            await self._run_agent_async(text)
        except Exception as exc:
            self._restore_prompt_after_submit_failure(text)
            self._show_command_result(
                f"[red]输入提交失败，原文已恢复：[/red] {type(exc).__name__}: {exc}"
            )

    def _restore_prompt_after_submit_failure(self, text: str) -> None:
        try:
            prompt = self.screen.query_one("#prompt-input", PromptTextArea)
            if not prompt.text.strip():
                prompt.text = text
                prompt.move_cursor(prompt.document.end)
                prompt.focus()
        except Exception:
            pass

    async def _run_agent_async(self, user_input: str) -> None:
        """异步执行 agent"""
        prepared = self._prepare_extension_request(user_input)
        if prepared is None:
            return
        self._run_agent(prepared)

    def _prepare_extension_request(self, user_input: str) -> str | None:
        """Parse user-facing extension commands and update conversation state."""
        text = user_input.strip()
        if not self._session:
            return text
        parts = text.split(maxsplit=2)
        if parts and parts[0].casefold() == "/skill" and len(parts) >= 2:
            name = parts[1]
            if name.casefold() == "deactivate":
                target = parts[2].strip() if len(parts) == 3 else ""
                if not target:
                    self._show_command_result("[yellow]用法：/skill deactivate <name|all>[/yellow]")
                    return None
                self._session.active_skills = [] if target.casefold() == "all" else [
                    item for item in self._session.active_skills if item.casefold() != target.casefold()
                ]
                append_session_state(self._session)
                self._show_command_result(f"[green]Skill {target} 已取消激活[/green]")
                return None
            if name.casefold() not in {"list", "install", "enable", "disable", "remove", "update", "info"}:
                skill = self._skill_manager.get(name) if self._skill_manager else None
                if not skill or not skill.enabled:
                    self._show_command_result(f"[red]Skill {name} 未安装或未启用，请前往 Skill 管理界面处理。[/red]")
                    return None
                if len(parts) < 3 or not parts[2].strip():
                    self._show_command_result(
                        f"[yellow]Skill 指令后还需要输入具体任务。[/yellow]\n\n"
                        f"示例：/skill {skill.name} 帮我完成这个任务"
                    )
                    return None
                if skill.name not in self._session.active_skills:
                    self._session.active_skills.append(skill.name)
                    append_session_state(self._session)
                return parts[2].strip()

        if parts and parts[0].casefold() == "/mcp" and len(parts) >= 2:
            name = parts[1]
            if name.casefold() == "deactivate":
                target = parts[2].strip() if len(parts) == 3 else ""
                if not target:
                    self._show_command_result("[yellow]用法：/mcp deactivate <name|all>[/yellow]")
                    return None
                self._session.selected_mcps = [] if target.casefold() == "all" else [
                    item for item in self._session.selected_mcps if item.casefold() != target.casefold()
                ]
                append_session_state(self._session)
                self._show_command_result(f"[green]MCP {target} 已取消选择[/green]")
                return None
            if name.casefold() not in {"list", "add", "enable", "disable", "remove", "test", "tools", "info"}:
                server = self._mcp_manager.get(name) if self._mcp_manager else None
                if not server or not server.enabled or server.last_error:
                    self._show_command_result(f"[red]MCP {name} 不可用，请前往 MCP 管理界面测试连接。[/red]")
                    return None
                if len(parts) < 3 or not parts[2].strip():
                    examples = "\n".join(f"/mcp {server.name} {action}" for action, _ in self._mcp_actions(server.name))
                    self._show_command_result(f"[yellow]MCP {server.name} 还需要选择具体操作。[/yellow]\n\n{examples}")
                    return None
                action_text = parts[2].strip()
                if server.name.casefold() == "memory" and action_text == "保存信息":
                    self._show_command_result(
                        "[yellow]“保存信息”后还需要输入需要保存的内容。[/yellow]\n\n"
                        "/mcp memory 保存信息 我的项目名称是 Coomi"
                    )
                    return None
                if server.name not in self._session.selected_mcps:
                    self._session.selected_mcps.append(server.name)
                    append_session_state(self._session)
                return f"请使用 {server.name} MCP 完成以下操作：{action_text}"
        return text

    def _ensure_new_session_for_welcome_input(self) -> None:
        """Typing on the welcome screen starts a fresh conversation."""
        if not self._is_welcome_visible():
            return
        if self._session and not self._session.messages:
            return
        system_prompt = self._session.system_prompt if self._session else "You are Coomi Agent."
        self._session = self._session_mgr.create_session(
            system_prompt=system_prompt,
            cwd=self._cwd,
            model=self._display_name,
        )
        self.status_line.cumulative_usage.input_tokens = 0
        self.status_line.cumulative_usage.output_tokens = 0
        self.status_line.cumulative_usage.total_tokens = 0
        try:
            log = self.screen.query_one("#message-log", RichLog)
            log.clear()
        except Exception:
            pass

    async def _handle_clear(self) -> None:
        old_id = self._session.id
        system_prompt = await build_system_prompt(
            memory_manager=self._memory_manager,
            memory_recall=self._memory_recall,
            skill_manager=self._skill_manager,
            current_context="",
            cwd=self._cwd,
            model_display=self._display_name,
            permission_mode=self._permission_system.mode.value,
        )
        self._session = self._session_mgr.create_session(
            system_prompt=system_prompt,
            cwd=self._cwd,
            model=self._display_name,
        )
        self._session_mgr.delete_session(old_id)
        self.status_line.cumulative_usage.input_tokens = 0
        self.status_line.cumulative_usage.output_tokens = 0
        self.status_line.cumulative_usage.total_tokens = 0

        log = self.screen.query_one("#message-log", RichLog)
        log.clear()
        try:
            self._show_welcome_message()
        except Exception:
            pass

    # -- agent execution ---------------------------------------------------

    def action_cancel_or_exit(self) -> None:
        """Esc: cancel agent if running, otherwise double-press to exit."""
        if self._agent_running:
            # 纯中断当前轮：不自动重跑、不排空队列、保留输入框草稿。
            # 「插队」才使用 set_input_buffer；此处不触碰它。
            self._cancel_requested = True
            self._agent.cancel_token.cancel()
            return

        # Idle — double-press Esc to exit
        if self._exit_pending:
            self.exit()
        else:
            self._exit_pending = True
            try:
                status = self.screen.query_one("#status-panel", StatusPanel)
                status.set_exit_pending()
            except Exception:
                pass
            # 更新输入框 placeholder 提示用户再按一次
            try:
                prompt = self.screen.query_one("#prompt-input", PromptTextArea)
                prompt.placeholder = '再按一次 ESC 退出，或继续输入...'
            except Exception:
                pass
            self._exit_timer = self.set_timer(2.0, self._reset_exit_pending)

    def action_toggle_reasoning(self) -> None:
        """Ctrl+R: 切换推理内容可见性。"""
        self._reasoning_visible = not self._reasoning_visible

    def _flush_reasoning(self, log: RichLog) -> bool:
        """Commit one model reasoning phase and reset it before tool execution."""
        reasoning = self._full_reasoning
        self._full_reasoning = ""
        started_at = self._reasoning_start_time
        self._reasoning_start_time = 0.0
        if not reasoning or not self._reasoning_visible:
            return False

        now = time.time()
        elapsed = max(0.0, now - started_at) if isinstance(started_at, (int, float)) else 0.0
        reasoning_text = reasoning.replace("\n", "\n│ ")
        reasoning_block = (
            f"[dim]┌─ [Thinking ({elapsed:.1f}s)][/dim]\n"
            f"[dim]│ {reasoning_text}[/dim]\n"
            f"[dim]└─[/dim]"
        )
        log.write(reasoning_block)
        return True

    def _reset_exit_pending(self) -> None:
        """Reset exit-pending state after timeout."""
        self._exit_pending = False
        self._exit_timer = None
        try:
            status = self.screen.query_one("#status-panel", StatusPanel)
            status.reset_exit_pending()
        except Exception:
            pass

    @work(exclusive=True)
    async def _run_agent(self, user_input: str) -> None:
        """Launch agent execution as a background async worker."""
        self._agent_running = True
        self._cancel_requested = False
        self._stream_buffer = ""
        self._full_reasoning = ""
        self._reasoning_start_time = 0.0
        self._active_banners.clear()

        log = self.screen.query_one("#message-log", RichLog)
        status = self.screen.query_one("#status-panel", StatusPanel)
        preview = self.screen.query_one("#stream-preview", StreamingPreview)
        prompt = self.screen.query_one("#prompt-input", PromptTextArea)

        self._wl(log, f"\n[bold cyan]You:[/bold cyan] {user_input}")

        prompt.disabled = False
        prompt.placeholder = PROMPT_PLACEHOLDER_RUNNING
        status.set_executing()
        preview.show_status("Preparing context")

        self._start_spinner()

        try:
            while True:
                if self._stream_buffer:
                    self._wl(log, "")

                self._stream_buffer = ""
                self._full_reasoning = ""

                preview.show_status("Preparing context")
                self._session.system_prompt = await build_system_prompt(
                    memory_manager=self._memory_manager,
                    memory_recall=self._memory_recall,
                    skill_manager=self._skill_manager,
                    current_context=user_input,
                    cwd=self._cwd,
                    model_display=self._display_name,
                    plan_mode=self._plan_mode,
                    active_skills=self._session.active_skills,
                    selected_mcps=self._session.selected_mcps,
                    permission_mode=self._permission_system.mode.value,
                )
                for skill_name in self._session.active_skills:
                    skill = self._skill_manager.get(skill_name) if self._skill_manager else None
                    if skill and skill.enabled:
                        self._wl(log, f"[bold green]Skill {skill.name} 已触发[/bold green]")
                for server_name in self._session.selected_mcps:
                    self._wl(log, f"[bold cyan]MCP {server_name} 已选择[/bold cyan]")
                self._mcp_called_this_turn.clear()
                preview.show_status("Waiting for model")

                async for event in self._agent.run_stream(self._session, user_input):
                    if self._cancel_requested:
                        self._cleanup_banners_on_cancel(log)
                        self._wl(log, "\n[dim]Cancelled.[/dim]")
                        break

                    # --- 文本 chunk ---
                    if isinstance(event, TextChunk):
                        # 首个文本 chunk 到达时，先渲染已累积的推理内容
                        if self._full_reasoning and self._reasoning_visible and not self._stream_buffer:
                            self._flush_reasoning(log)

                        self._stream_buffer += event.content
                        preview.show_text(self._stream_buffer)

                    # --- 推理 chunk ---
                    elif isinstance(event, ReasoningChunk):
                        if not self._full_reasoning:
                            self._reasoning_start_time = time.time()
                        self._full_reasoning += event.content
                        if self._reasoning_visible:
                            preview.show_reasoning(self._full_reasoning)

                    # --- 工具开始（双重 yield） ---
                    elif isinstance(event, ToolStart):
                        # Reasoning belongs to the model phase before this tool. Flush it
                        # now so later tool rounds cannot accumulate in the same box.
                        self._flush_reasoning(log)
                        if self._stream_buffer.strip():
                            preview.flush_pending()
                            log.write(Markdown(self._stream_buffer))
                            self._stream_buffer = ""
                        tool_name = event.tool_name
                        if tool_name.startswith("mcp__"):
                            pieces = tool_name.split("__", 2)
                            if len(pieces) == 3:
                                self._mcp_called_this_turn.add(pieces[1])
                                self._wl(log, f"[cyan]MCP {pieces[1]} · {pieces[2]} 正在调用[/cyan]")
                        if tool_name in self._active_banners:
                            banner = self._active_banners[tool_name]
                            if event.arguments:
                                banner.set_arguments(event.arguments)
                        else:
                            banner = ToolCallBanner(tool_name=tool_name)
                            self._active_banners[tool_name] = banner
                            if event.arguments:
                                banner.set_arguments(event.arguments)
                        preview.show_tool(tool_name)

                    # --- 工具执行中 ---
                    elif isinstance(event, ToolRunning):
                        banner = self._active_banners.get(event.tool_name)
                        if banner:
                            banner.set_running()
                        # 进入工具阻塞窗口：此刻 LLM 闲置，允许并发只读交流。
                        self._tool_executing = True

                    # --- 工具完成 ---
                    elif isinstance(event, ToolDone):
                        # 退出阻塞窗口，并等待可能正在跑的并发交流收尾，
                        # 避免主流与 side 流同时写 UI。
                        self._tool_executing = False
                        await self._await_side_task()
                        banner = self._active_banners.pop(event.tool_name, None)
                        if banner:
                            banner.set_done(
                                result_preview=event.result_preview or "",
                                cache_hit=False,
                                is_error=event.is_error,
                            )
                            log.write(banner.build())
                        preview.show_thinking()
                        if event.tool_name.startswith("mcp__"):
                            pieces = event.tool_name.split("__", 2)
                            if len(pieces) == 3:
                                state = "调用失败" if event.is_error else "调用成功"
                                color = "red" if event.is_error else "green"
                                self._wl(log, f"[{color}]MCP {pieces[1]} · {pieces[2]} {state}[/{color}]")

                    # --- 缓存命中 ---
                    elif isinstance(event, ToolCacheHit):
                        self._tool_executing = False
                        await self._await_side_task()
                        banner = self._active_banners.pop(event.tool_name, None)
                        if banner:
                            banner.set_done(cache_hit=True)
                            log.write(banner.build())
                        preview.show_thinking()

                    # --- Token 用量 ---
                    elif isinstance(event, UsageUpdate):
                        self.status_line.update_usage(event.usage)
                        status.refresh()

                    elif isinstance(event, ConnectionRetry):
                        # The failed attempt never committed content/tool calls, so its
                        # preview can be safely replaced by the retried response.
                        self._stream_buffer = ""
                        self._full_reasoning = ""
                        self._reasoning_start_time = 0.0
                        preview.show_status(
                            f"Reconnecting ({event.attempt}/{event.max_attempts})"
                        )
                        self._wl(
                            log,
                            f"[yellow]连接中断，{event.delay:.0f}s 后自动重试 "
                            f"({event.attempt}/{event.max_attempts})…[/yellow]",
                        )

                    # --- 压缩事件 ---
                    elif isinstance(event, CompressionEvent):
                        status.set_compressing(event.before, event.after)
                        self._wl(
                            log,
                            f"[dim]Context compressed: {event.before} -> {event.after} messages[/dim]",
                        )

                    # --- 取消/错误 ---
                    elif isinstance(event, AgentCancelled):
                        self._cleanup_banners_on_cancel(log)
                        self._wl(log, "\n[dim]Cancelled.[/dim]")
                        break
                    elif isinstance(event, AgentError):
                        if event.is_fatal:
                            # 致命错误 — 步骤确实失败，但用户仍可继续对话
                            self._wl(log, f"\n[red]❌ Agent 错误:[/red] {event.message}")
                        else:
                            # 非致命警告 — LLM 降级/迭代上限等，可继续
                            self._wl(log, f"\n[yellow]⚠️ Agent 警告:[/yellow] {event.message}")
                        self._wl(log, "[dim]你可以继续输入来恢复工作。[/dim]")
                        break  # 退出当前 run，但 finally 块会正常恢复 UI

                # --- 流结束：推理内容若未渲染（无 TextChunk 跟随），在此兜底 ---
                self._flush_reasoning(log)

                # --- 流结束：flush 文本缓冲区 ---
                if self._stream_buffer.strip():
                    preview.flush_pending()
                    log.write(Markdown(self._stream_buffer))
                preview.clear_preview()

                # --- 若正在整理队列则先等待闸门（放行后再判定 buffer/队列）---
                await self._queue_drain_gate.wait()

                # --- 检查 buffered input（立即引导 / 插队，最高优先）---
                buffered = self._agent.cancel_token.get_input_buffer()
                if buffered:
                    user_input = buffered
                    self._agent.cancel_token.reset()
                    self._cancel_requested = False
                    self._wl(log, f"\n[bold cyan]You:[/bold cyan] {user_input}")
                    preview.show_thinking()
                    continue

                # --- 纯 Esc 中断 → 停止，不排空队列（草稿与队列保留）---
                if self._cancel_requested:
                    self._cancel_requested = False
                    break

                # --- 交流队列优先排空（仅限本次，不触发主队列）---
                if self._comm_queue:
                    user_input = self._comm_queue.pop(0)
                    self._refresh_comm_title()
                    self._agent.cancel_token.reset()
                    self._wl(log, f"\n[bold #58d0e8]临时交流:[/bold #58d0e8] {user_input}")
                    preview.show_thinking()
                    continue

                # --- 主队列排空 ---
                if self._pending_queue:
                    user_input = self._pending_queue.pop(0)
                    self._refresh_queue_panel()
                    self._agent.cancel_token.reset()
                    self._wl(log, f"\n[bold cyan]You:[/bold cyan] {user_input}")
                    preview.show_thinking()
                    continue

                for server_name in self._session.selected_mcps:
                    if server_name not in self._mcp_called_this_turn:
                        self._wl(log, f"[dim]MCP {server_name} 本轮未调用：模型判断当前回复无需工具[/dim]")
                break

        except Exception as e:
            error_type = type(e).__name__
            if "Connection" in error_type or "Connect" in error_type:
                self._wl(log, f"\n[red]连接错误: {e}[/red]")
                self._wl(log, "[yellow]请检查：[/yellow]")
                self._wl(log, "  1. 网络连接是否正常")
                self._wl(log, "  2. API 端点 URL 是否正确")
                self._wl(log, "  3. 防火墙/代理是否拦截")
            elif "Timeout" in error_type:
                self._wl(log, f"\n[red]请求超时: {e}[/red]")
                self._wl(log, "[yellow]请检查网络稳定性或增加超时配置[/yellow]")
            elif "Authentication" in error_type or "401" in str(e):
                self._wl(log, f"\n[red]认证失败: {e}[/red]")
                self._wl(log, "[yellow]请检查 API Key 是否有效[/yellow]")
            else:
                self._wl(log, f"\n[red]Agent error: {e}[/red]")
        finally:
            self._stop_spinner()
            self._agent_running = False
            self._cancel_requested = False
            self._tool_executing = False
            await self._await_side_task()
            self._active_banners.clear()
            status.set_idle()
            preview.clear_preview()
            self._refresh_comm_title()
            prompt.disabled = False
            prompt.placeholder = idle_placeholder()
            prompt.focus()

            # Post-run token accounting is synchronous; memory extraction is best-effort.
            try:
                estimated = _estimate_tokens_from_dicts(
                    self._session.get_messages_for_api()
                )
                self.status_line.update_session_usage(
                    self._session.token_usage, estimated
                )
                status.refresh()
                if self._memory_extractor and self._memory_manager:
                    asyncio.create_task(
                        self._extract_memory_background(list(self._session.messages))
                    )
            except Exception:
                pass

    async def _extract_memory_background(self, messages) -> None:
        """Best-effort memory extraction that never holds the chat worker open."""
        if not self._memory_extractor or not self._memory_manager:
            return
        try:
            timeout = float(os.environ.get("COOMI_MEMORY_EXTRACT_TIMEOUT", "3.0"))
            extracted = await asyncio.wait_for(
                self._memory_extractor.extract(messages),
                timeout=timeout,
            )
            if extracted:
                self._memory_manager.refresh_index()
        except Exception:
            pass

    def _cleanup_banners_on_cancel(self, log: RichLog) -> None:
        """取消时清理所有活跃的 banner。"""
        for banner in self._active_banners.values():
            banner.set_done(result_preview="[cancelled]")
            log.write(banner.build())
        self._active_banners.clear()

    # -- spinner -----------------------------------------------------------

    def _start_spinner(self) -> None:
        self._spinner_idx = 0
        self._spinner_timer = self.set_interval(0.08, self._tick_spinner)

    def _stop_spinner(self) -> None:
        if self._spinner_timer:
            self._spinner_timer.stop()
            self._spinner_timer = None

    def _tick_spinner(self) -> None:
        self._spinner_idx = (self._spinner_idx + 1) % len(SPINNER_CHARS)
        spinner_char = SPINNER_CHARS[self._spinner_idx]
        try:
            status = self.screen.query_one("#status-panel", StatusPanel)
            status.set_spinner(spinner_char)
        except Exception:
            pass

        # Animate stream preview dots during thinking
        try:
            preview = self.screen.query_one("#stream-preview", StreamingPreview)
            dots_cycle = ["   ", ".  ", ".. ", "..."]
            dots = dots_cycle[self._spinner_idx % 4]
            preview.tick_status(spinner_char, dots)
        except Exception:
            pass

    # -- cleanup -----------------------------------------------------------
