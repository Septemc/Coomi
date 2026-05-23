#!/usr/bin/env python3
"""Coomi Agent 启动脚本 - 瀑布流输出 + 状态栏 + 模型切换"""
import os

from apps.backend.core.engine import AgentLoop, Session, SessionManager
from apps.backend.core.engine.session import build_system_prompt
from apps.backend.core.services.memory import MemoryManager, MemoryRecall, MemoryType
from apps.backend.core.services.memory.extractor import MemoryExtractor
from apps.backend.core.services.memory.types import Memory
from apps.backend.core.services import get_llm_provider
from apps.backend.core.services.llm.config import ConfigManager
from apps.backend.core.services.llm.factory import get_config_manager
from apps.backend.core.tools.registry import create_default_registry
from apps.backend.core.services.context.compressor import _estimate_tokens_from_dicts
from apps.backend.core.ui.stream_renderer import StreamRenderer
from apps.backend.core.ui.status_line import StatusLine, format_token_count
from apps.backend.core.ui.tool_formatter import format_tool_display
from rich.console import Console

console = Console()


def handle_model_command(config_mgr, status_line: StatusLine, ctx: dict, args: str) -> None:
    """处理 /model 命令 — 列出/切换模型

    Args:
        config_mgr: ConfigManager 实例
        status_line: 状态栏实例
        ctx: 可变上下文（含 provider, agent, memory_extractor, memory_recall）
        args: 命令参数（provider ID 或为空）
    """
    if not args:
        # 列出所有可用模型
        providers = config_mgr.list_providers()
        active_id = config_mgr.data.get("active", "")
        if not providers:
            console.print("[dim]没有配置任何模型。[/dim]")
            console.print(f"[dim]请编辑 {config_mgr.get_config_path_str()} 添加配置。[/dim]")
            return

        console.print(f"[bold cyan]可用模型 ({len(providers)} 个):[/bold cyan]")
        for p in providers:
            marker = " [bold green](当前)[/bold green]" if p.id == active_id else ""
            fast_info = f" [dim](fast: {p.fast_model})[/dim]" if p.fast_model else ""
            console.print(f"  [bold]{p.id}[/bold]: {p.display} ({p.type}){fast_info}{marker}")
        console.print("[dim]切换: /model <id>[/dim]")
        return

    # 切换到指定 provider
    provider_id = args.strip()
    if not config_mgr.set_active(provider_id):
        console.print(f"[red]未找到模型: {provider_id}[/red]")
        console.print("[dim]使用 /model 查看可用列表[/dim]")
        return

    # 创建新 provider 并同步更新所有引用
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
    console.print(f"[bold cyan]已切换到:[/bold cyan] {new_provider.get_model_display_name()} ([dim]{new_provider.model}[/dim])")


def handle_context_command(status_line: StatusLine, agent: AgentLoop, args: str) -> None:
    """处理 /context 命令

    Args:
        status_line: 状态栏实例
        agent: AgentLoop 实例
        args: 命令参数（如 "256k", "512k", "128k"）
    """
    if not args:
        # 显示当前上下文窗口大小和可用预设
        current = status_line.get_context_window_size()
        console.print(f"[bold cyan]当前上下文窗口:[/bold cyan] {format_token_count(current)}")
        console.print("[dim]可用预设: /context 128k | /context 256k | /context 512k | /context 1m[/dim]")
        console.print("[dim]也可自定义: /context <数字>[k|m][/dim]")
        return

    # 解析大小参数
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
            console.print("[red]上下文窗口至少 1K tokens[/red]")
            return
        if size > 10_000_000:
            console.print("[red]上下文窗口最大 10M tokens[/red]")
            return
        status_line.set_context_window_size(size)
        agent.context_window_size = size
        from apps.backend.core.ui.status_line import format_token_count
        console.print(f"[bold cyan]上下文窗口已设置为:[/bold cyan] {format_token_count(size)}")
    except ValueError:
        console.print("[red]格式错误，请使用如 /context 256k 或 /context 512k[/red]")


def handle_memory_command(memory_manager: MemoryManager, args: str) -> None:
    """处理 /memory 命令

    Args:
        memory_manager: 记忆管理器实例
        args: 命令参数
    """
    if not args:
        console.print("[bold cyan]记忆系统命令:[/bold cyan]")
        console.print("  /memory list          - 列出所有记忆")
        console.print("  /memory add <content> - 添加新记忆")
        console.print("  /memory delete <name> - 删除记忆")
        console.print("  /memory search <query> - 搜索记忆")
        console.print("  /memory show <name>   - 显示记忆详情")
        console.print("  /memory refresh       - 刷新索引文件")
        return

    parts = args.split(maxsplit=1)
    subcmd = parts[0].lower()
    subargs = parts[1].strip() if len(parts) > 1 else ""

    if subcmd == "list":
        memories = memory_manager.list_memories()
        if not memories:
            console.print("[dim]暂无记忆[/dim]")
            return

        console.print(f"[bold cyan]记忆列表 ({len(memories)} 条):[/bold cyan]")
        for m in memories:
            stale_marker = " [red][stale][/red]" if m.is_stale else ""
            type_color = {
                MemoryType.USER: "green",
                MemoryType.FEEDBACK: "yellow",
                MemoryType.PROJECT: "blue",
                MemoryType.REFERENCE: "magenta",
            }.get(m.memory_type, "white")
            console.print(f"  [{type_color}]{m.memory_type.value:10}[/{type_color}] {m.name}: {m.description}{stale_marker}")

    elif subcmd == "add":
        if not subargs:
            console.print("[red]请提供记忆内容[/red]")
            console.print("[dim]用法: /memory add <content>[/dim]")
            return

        # 生成名称
        import hashlib
        name = hashlib.md5(subargs.encode()).hexdigest()[:8]
        name = f"memory-{name}"

        # 创建记忆
        memory = Memory(
            name=name,
            description=subargs[:50] + ("..." if len(subargs) > 50 else ""),
            memory_type=MemoryType.USER,
            content=subargs,
        )

        if memory_manager.save_memory(memory):
            console.print(f"[bold green]✓ 记忆已保存:[/bold green] {name}")
        else:
            console.print("[red]保存失败[/red]")

    elif subcmd == "delete":
        if not subargs:
            console.print("[red]请提供记忆名称[/red]")
            console.print("[dim]用法: /memory delete <name>[/dim]")
            return

        if memory_manager.delete_memory(subargs):
            console.print(f"[bold green]✓ 记忆已删除:[/bold green] {subargs}")
        else:
            console.print(f"[red]未找到记忆: {subargs}[/red]")

    elif subcmd == "search":
        if not subargs:
            console.print("[red]请提供搜索关键词[/red]")
            console.print("[dim]用法: /memory search <query>[/dim]")
            return

        results = memory_manager.search_memories(subargs)
        if not results:
            console.print("[dim]未找到匹配的记忆[/dim]")
            return

        console.print(f"[bold cyan]搜索结果 ({len(results)} 条):[/bold cyan]")
        for m in results:
            console.print(f"  {m.name}: {m.description}")

    elif subcmd == "show":
        if not subargs:
            console.print("[red]请提供记忆名称[/red]")
            console.print("[dim]用法: /memory show <name>[/dim]")
            return

        memory = memory_manager.get_memory(subargs)
        if not memory:
            console.print(f"[red]未找到记忆: {subargs}[/red]")
            return

        console.print(f"[bold cyan]记忆: {memory.name}[/bold cyan]")
        console.print(f"[dim]类型: {memory.memory_type.value}[/dim]")
        console.print(f"[dim]描述: {memory.description}[/dim]")
        console.print(f"[dim]创建: {memory.created_at}[/dim]")
        console.print(f"[dim]更新: {memory.updated_at}[/dim]")
        console.print(f"\n{memory.content}")

    elif subcmd == "refresh":
        memory_manager.refresh_index()
        console.print("[bold green]✓ 索引文件已刷新[/bold green]")

    else:
        console.print(f"[red]未知子命令: {subcmd}[/red]")
        console.print("[dim]可用子命令: list, add, delete, search, show, refresh[/dim]")


def main():
    # 初始化配置
    config_mgr = get_config_manager()

    # 初始化组件
    provider = get_llm_provider()
    tool_registry = create_default_registry()
    session_manager = SessionManager()
    status_line = StatusLine(console)

    # 初始化记忆系统
    current_dir = os.getcwd()
    memory_manager = MemoryManager(project_path=current_dir)
    memory_extractor = MemoryExtractor(provider, memory_manager)
    memory_recall = MemoryRecall(provider, memory_manager)

    # 设置初始模型
    model_name = provider.model if hasattr(provider, "model") else "unknown"
    display_name = provider.get_model_display_name()
    status_line.set_model(model_name, display_name)

    # 可变上下文（用于 /model 切换时同步更新所有引用）
    ctx = {
        "provider": provider,
        "agent": None,  # 下面创建后回填
        "memory_extractor": memory_extractor,
        "memory_recall": memory_recall,
        "display_name": display_name,
    }

    # 创建 Agent（使用默认上下文窗口）
    context_window_size = status_line.get_context_window_size()
    agent = AgentLoop(provider, tool_registry, context_window_size)
    ctx["agent"] = agent

    # 构建含记忆的 System Prompt
    system_prompt = build_system_prompt(
        memory_manager=memory_manager,
        memory_recall=memory_recall,
        current_context="",
        cwd=current_dir,
        model_display=display_name,
    )
    session = session_manager.create_session(system_prompt=system_prompt)

    # 显示启动信息
    tool_count = len(tool_registry.list_tools())
    console.print(
        f"[bold cyan]Coomi Agent[/bold cyan] "
        f"[dim]({display_name}, {tool_count} tools)[/dim]"
    )
    console.print("[dim]命令: /model [pro|flash] | /context [256k|512k] | /memory | /clear | exit[/dim]\n")

    while True:
        try:
            user_input = console.input("[bold blue]You:[/bold blue] ")
        except (EOFError, KeyboardInterrupt):
            break

        if user_input.lower() in ("exit", "quit"):
            break

        # 处理 /model 命令
        stripped = user_input.strip()
        if stripped == "/model" or stripped.startswith("/model "):
            args = stripped[6:].strip()
            handle_model_command(config_mgr, status_line, ctx, args)
            continue

        # 处理 /context 命令
        if stripped == "/context" or stripped.startswith("/context "):
            args = stripped[8:].strip()
            handle_context_command(status_line, agent, args)
            continue

        # 处理 /memory 命令
        if stripped == "/memory" or stripped.startswith("/memory "):
            args = stripped[7:].strip()
            handle_memory_command(memory_manager, args)
            continue

        # 处理 /clear 命令
        if stripped == "/clear":
            old_id = session.id
            system_prompt = build_system_prompt(
                memory_manager=memory_manager,
                memory_recall=memory_recall,
                current_context="",
                cwd=current_dir,
                model_display=ctx["display_name"],
            )
            session = session_manager.create_session(system_prompt=system_prompt)
            session_manager.delete_session(old_id)
            console.print("[dim]会话已清除[/dim]\n")
            continue

        # 注入最新记忆到 system prompt（每轮刷新，含语义召回）
        session.system_prompt = build_system_prompt(
            memory_manager=memory_manager,
            memory_recall=memory_recall,
            current_context=user_input,
            cwd=current_dir,
            model_display=ctx["display_name"],
        )

        # 流式输出 Agent 响应
        console.print("[bold green]Agent:[/bold green] ", end="")
        renderer = StreamRenderer(console)
        renderer.start()

        try:
            for chunk in agent.run_stream(session, user_input):
                if isinstance(chunk, dict):
                    chunk_type = chunk.get("type")

                    if chunk_type == "tool_start":
                        # 工具执行 - 完成当前渲染，显示详情
                        tool_name = chunk.get("tool_name", "unknown")
                        tool_args = chunk.get("arguments", {})
                        display = format_tool_display(tool_name, tool_args)
                        renderer.finish()
                        console.print(f"\n[dim italic]  {display}[/dim italic]")
                        # 重新开始渲染
                        renderer = StreamRenderer(console)
                        renderer.start()

                    elif chunk_type == "tool_cache_hit":
                        # 缓存命中
                        tool_name = chunk.get("tool_name", "unknown")
                        console.print(f"[dim italic]  ↳ 缓存命中[/dim italic]")

                    elif chunk_type == "compression":
                        # 上下文压缩通知 - 先完成当前渲染再显示压缩信息
                        before = chunk.get("before", 0)
                        after = chunk.get("after", 0)
                        renderer.finish()
                        console.print(
                            f"\n[dim italic]⟳ 上下文已压缩: {before} → {after} 条消息[/dim italic]"
                        )
                        # 重新开始渲染
                        renderer = StreamRenderer(console)
                        renderer.start()

                    elif chunk_type == "usage":
                        status_line.update_usage(chunk["data"])

                elif isinstance(chunk, str):
                    renderer.write(chunk)

            # 完成流式渲染
            renderer.finish()
            console.print()

            # 渲染最终状态栏（只渲染一次，使用当前 prompt 大小估算）
            estimated_prompt = _estimate_tokens_from_dicts(session.get_messages_for_api())
            status_line.update_session_usage(session.token_usage, estimated_prompt)
            status_line.render_final()
            console.print()  # 额外空行分隔

            # 记忆提取（每轮自动分析）
            try:
                extracted = memory_extractor.extract(session.messages)
                if extracted:
                    console.print(f"[dim italic]💾 记忆已自动提取: {extracted.name}[/dim italic]")
                    # 刷新 memory 索引
                    memory_manager.refresh_index()
            except Exception:
                pass  # 记忆提取失败不影响主流程

        except Exception as e:
            if renderer.is_started:
                renderer.finish()
            console.print(f"\n[red]Error: {e}[/red]")

    console.print("[dim]再见！[/dim]")


if __name__ == "__main__":
    main()
