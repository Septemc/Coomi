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
from ..services.session_history import list_session_records, load_session_from_jsonl
from ..services.llm.factory import get_config_manager
from ..services.memory import MemoryManager, MemoryRecall, MemoryType
from ..services.memory.extractor import MemoryExtractor
from ..services.context.compressor import _estimate_tokens_from_dicts
from ..security import HookSystem, PermissionSystem
from ..tools.registry import create_default_registry
from ..ui.events import (
    AgentCancelled,
    AgentError,
    CompressionEvent,
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
from .screens.main_screen import MainScreen
from .screens.command_palette import CommandPalette

SPINNER_CHARS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


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
            lines.append(f"  [bold]{p.id}[/bold]: {p.display} ({p.type}){fast_info}{marker}")
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
        # 问询模式导航 — priority=True 在 TextArea BINDINGS 之前检查
        Binding("up", "question_up", "↑", priority=True),
        Binding("down", "question_down", "↓", priority=True),
        Binding("left", "question_left", "←", priority=True),
        Binding("right", "question_right", "→", priority=True),
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
        # "none" | "command" | "question" | "model_picker" | "context_picker"
        self._interactive_mode: str = "none"

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
            current_context="",
            cwd=self._cwd,
            model_display=self._display_name,
        )
        self._session = self._session_mgr.create_session(
            system_prompt=system_prompt,
            cwd=self._cwd,
            model=self._display_name,
        )

        # Wait for screen to be ready before querying widgets
        self.call_after_refresh(self._show_welcome_message)

    # -- command palette ---------------------------------------------------

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
                self._show_command_result("[dim]SKILL 安装功能即将推出...[/dim]")
            elif option == "install_mcp":
                self._show_command_result("[dim]MCP 安装功能即将推出...[/dim]")

        self.push_screen(SettingsScreen(), on_settings_result)

    def _open_provider_list(self) -> None:
        """打开 Provider 列表"""
        from .screens.provider_list_screen import ProviderListScreen

        def on_provider_list_result(result: dict | None) -> None:
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
                self._show_command_result("[bold green]Provider 已保存[/bold green]")

        self.push_screen(ProviderEditScreen(self._config_mgr, provider), on_edit_result)

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
        elif cmd == "/help":
            self._show_help()

    def _show_command_result(self, result: str) -> None:
        try:
            log = self.screen.query_one("#message-log", RichLog)
            self._wl(log, result)
        except Exception:
            pass

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
        """Ctrl+C 只复制消息区选中文本，不触发退出/中断。"""
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
        except Exception:
            pass

    async def on_key(self, event: events.Key) -> None:
        if event.key == "ctrl+c":
            event.prevent_default()
            event.stop()
            self.action_copy_selected()

    def action_cycle_permission_mode(self) -> None:
        self._permission_system.cycle_mode()
        self.status_line.set_permission_label(self._permission_system.get_mode_label())
        self._refresh_status_panel()
        self._show_command_result(
            f"[bold yellow]Permission mode:[/bold yellow] "
            f"{self._permission_system.get_mode_label()}\n"
            f"[dim]{self._permission_system.get_mode_description()}[/dim]"
        )

    def _show_permission_mode(self) -> None:
        self._show_command_result(
            "[bold cyan]Permission modes[/bold cyan]\n\n"
            f"Current: [bold yellow]{self._permission_system.get_mode_label()}[/bold yellow]\n"
            f"[dim]{self._permission_system.get_mode_description()}[/dim]\n\n"
            "[dim]Press Shift+Tab or run /permission next to cycle.[/dim]"
        )

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
            current_context="",
            cwd=self._cwd,
            model_display=self._display_name,
            plan_mode=self._plan_mode,
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
            "  [bold]/compact[/bold]       压缩上下文\n"
            "  [bold]/clear[/bold]         清空会话历史\n"
            "  [bold]/help[/bold]          显示此帮助\n\n"
            "[dim]快捷键: Ctrl+P 命令面板 | Ctrl+R 切换推理 | Shift+Tab 权限模式 | 双 Esc 退出[/dim]"
        )
        self._show_command_result(help_text)

    # -- Plan Mode: AskUserQuestion ----------------------------------------

    def _set_interactive_mode(self, mode: str) -> None:
        """统一设置交互模式，同步旧布尔标志"""
        self._interactive_mode = mode
        self._command_mode = (mode == "command")
        self._question_mode = (mode == "question")

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        """条件路由：统一基于 _interactive_mode 拦截 keys，否则放行给 TextArea"""
        question_actions = {
            "question_up", "question_down", "question_left",
            "question_right", "question_confirm", "question_cancel_or_exit",
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

        if mode == "none" and self._welcome_panel_active() and not self._get_prompt_text():
            if action in ("question_up", "question_down", "question_confirm"):
                return True

        # 问询模式
        if mode == "question":
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

    async def action_question_left(self) -> None:
        if self._interactive_mode == "question" and self._plan_panel:
            self._plan_panel.prev_question()
        elif self._interactive_mode == "model_picker" and self._model_picker:
            self._model_picker.toggle_mode_left()

    async def action_question_right(self) -> None:
        if self._interactive_mode == "question" and self._plan_panel:
            self._plan_panel.next_question()
        elif self._interactive_mode == "model_picker" and self._model_picker:
            self._model_picker.toggle_mode_right()

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

    async def action_question_confirm(self) -> None:
        if self._interactive_mode == "none" and self._welcome_panel_active() and not self._get_prompt_text():
            self.screen.welcome_panel.open_selected_session()
            return

        # 指令模式 → 执行选中指令
        if self._interactive_mode == "command" and self._command_list:
            cmd = self._command_list.get_selected_command()
            self._hide_command_list()
            self._clear_prompt()
            if cmd:
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
        self._command_list = CommandList()
        try:
            log = self.screen.query_one("#message-log", RichLog)
            await self.screen.mount(self._command_list, before=log)
            self._set_interactive_mode("command")
        except Exception:
            pass

    def _hide_command_list(self) -> None:
        if self._command_list:
            try:
                self._command_list.remove()
            except Exception:
                pass
            self._command_list = None
        self._set_interactive_mode("none")

    # -- model picker -------------------------------------------------------

    async def _show_model_picker(self) -> None:
        from .widgets.model_picker import ModelPicker
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
        self._provider = new_provider
        self._ctx["provider"] = new_provider
        self._ctx["agent"].llm = new_provider
        self._ctx["agent"].compressor.llm = new_provider
        if self._ctx.get("memory_extractor"):
            self._ctx["memory_extractor"].llm = new_provider
        if self._ctx.get("memory_recall"):
            self._ctx["memory_recall"].llm = new_provider

        self._display_name = new_provider.get_model_display_name()
        self.status_line.set_model(new_provider.model, self._display_name)
        self._ctx["display_name"] = self._display_name
        self._active_provider_id = provider_id
        self._refresh_status_panel()
        self._show_command_result(
            f"[bold cyan]Switched to:[/bold cyan] {self._display_name} "
            f"([dim]{new_provider.model}[/dim]) [{mode_label}]"
        )

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

        # --- agent execution ---
        self._session.system_prompt = await build_system_prompt(
            memory_manager=self._memory_manager,
            memory_recall=self._memory_recall,
            current_context=user_input,
            cwd=self._cwd,
            model_display=self._display_name,
            plan_mode=self._plan_mode,
        )

        self._run_agent(user_input)

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

        # --- agent execution ---
        import asyncio
        asyncio.create_task(self._run_agent_async(text))

    async def _run_agent_async(self, user_input: str) -> None:
        """异步执行 agent"""
        self._session.system_prompt = await build_system_prompt(
            memory_manager=self._memory_manager,
            memory_recall=self._memory_recall,
            current_context=user_input,
            cwd=self._cwd,
            model_display=self._display_name,
            plan_mode=self._plan_mode,
        )
        self._run_agent(user_input)

    async def _handle_clear(self) -> None:
        old_id = self._session.id
        system_prompt = await build_system_prompt(
            memory_manager=self._memory_manager,
            memory_recall=self._memory_recall,
            current_context="",
            cwd=self._cwd,
            model_display=self._display_name,
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
            self._cancel_requested = True
            self._agent.cancel_token.cancel()
            try:
                textarea = self.screen.query_one("#prompt-input", PromptTextArea)
                current_text = textarea.text
                if current_text.strip():
                    self._agent.cancel_token.set_input_buffer(current_text.strip())
                    textarea.clear()
            except Exception:
                pass
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
        status.set_executing()
        preview.show_thinking()

        self._start_spinner()

        try:
            while True:
                if self._stream_buffer:
                    self._wl(log, "")

                self._stream_buffer = ""
                self._full_reasoning = ""

                async for event in self._agent.run_stream(self._session, user_input):
                    if self._cancel_requested:
                        self._cleanup_banners_on_cancel(log)
                        self._wl(log, "\n[dim]Cancelled.[/dim]")
                        break

                    # --- 文本 chunk ---
                    if isinstance(event, TextChunk):
                        # 首个文本 chunk 到达时，先渲染已累积的推理内容
                        if self._full_reasoning and self._reasoning_visible and not self._stream_buffer:
                            elapsed = time.time() - self._reasoning_start_time
                            reasoning_text = self._full_reasoning.replace("\n", "\n│ ")
                            reasoning_block = (
                                f"[dim]┌─ [Thinking ({elapsed:.1f}s)][/dim]\n"
                                f"[dim]│ {reasoning_text}[/dim]\n"
                                f"[dim]└─[/dim]"
                            )
                            log.write(reasoning_block)
                            self._full_reasoning = ""

                        self._stream_buffer += event.content
                        preview.show_text(self._stream_buffer)

                    # --- 推理 chunk ---
                    elif isinstance(event, ReasoningChunk):
                        if not self._full_reasoning:
                            self._reasoning_start_time = time.time()
                        self._full_reasoning += event.content

                    # --- 工具开始（双重 yield） ---
                    elif isinstance(event, ToolStart):
                        if self._stream_buffer.strip():
                            preview.flush_pending()
                            log.write(Markdown(self._stream_buffer))
                            self._stream_buffer = ""
                        tool_name = event.tool_name
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

                    # --- 工具完成 ---
                    elif isinstance(event, ToolDone):
                        banner = self._active_banners.pop(event.tool_name, None)
                        if banner:
                            banner.set_done(
                                result_preview=event.result_preview or "",
                                cache_hit=False,
                                is_error=event.is_error,
                            )
                            log.write(banner.build())
                        preview.show_thinking()

                    # --- 缓存命中 ---
                    elif isinstance(event, ToolCacheHit):
                        banner = self._active_banners.pop(event.tool_name, None)
                        if banner:
                            banner.set_done(cache_hit=True)
                            log.write(banner.build())
                        preview.show_thinking()

                    # --- Token 用量 ---
                    elif isinstance(event, UsageUpdate):
                        self.status_line.update_usage(event.usage)
                        status.refresh()

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
                if self._full_reasoning and self._reasoning_visible:
                    elapsed = time.time() - self._reasoning_start_time
                    reasoning_text = self._full_reasoning.replace("\n", "\n│ ")
                    reasoning_block = (
                        f"[dim]┌─ [Thinking ({elapsed:.1f}s)][/dim]\n"
                        f"[dim]│ {reasoning_text}[/dim]\n"
                        f"[dim]└─[/dim]"
                    )
                    log.write(reasoning_block)
                    self._full_reasoning = ""

                # --- 流结束：flush 文本缓冲区 ---
                if self._stream_buffer.strip():
                    preview.flush_pending()
                    log.write(Markdown(self._stream_buffer))
                preview.clear_preview()

                # --- 检查 buffered input（取消 + append 模式）---
                buffered = self._agent.cancel_token.get_input_buffer()
                if buffered:
                    user_input = buffered
                    self._agent.cancel_token.reset()
                    self._cancel_requested = False
                    self._wl(log, f"\n[bold cyan]You:[/bold cyan] {user_input}")
                    preview.show_thinking()
                    continue
                else:
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
            self._active_banners.clear()
            status.set_idle()
            preview.clear_preview()
            prompt.disabled = False

            # Post-run memory extraction + token accounting
            try:
                estimated = _estimate_tokens_from_dicts(
                    self._session.get_messages_for_api()
                )
                self.status_line.update_session_usage(
                    self._session.token_usage, estimated
                )
                status.refresh()

                extracted = await self._memory_extractor.extract(self._session.messages)
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
            current = preview.renderable
            if current and "Thinking" in str(current):
                dots_cycle = ["·  ", "·· ", "···", "·· "]
                dots = dots_cycle[self._spinner_idx % 4]
                preview.update(f"[bold yellow]{spinner_char} Thinking{dots}[/bold yellow]")
        except Exception:
            pass

    # -- cleanup -----------------------------------------------------------
