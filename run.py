#!/usr/bin/env python3
"""Coomi Agent 启动脚本 - 瀑布流输出 + 状态栏 + 模型切换"""
from apps.backend.core.engine import AgentLoop, Session, SessionManager
from apps.backend.core.services import get_llm_provider
from apps.backend.core.tools.registry import create_default_registry
from apps.backend.core.ui.stream_renderer import StreamRenderer
from apps.backend.core.ui.status_line import StatusLine
from rich.console import Console

console = Console()


def handle_model_command(provider, status_line: StatusLine, args: str) -> None:
    """处理 /model 命令

    Args:
        provider: LLM Provider 实例
        status_line: 状态栏实例
        args: 命令参数（模型名称或别名）
    """
    if not args:
        # 显示当前模型
        name = provider.model if hasattr(provider, "model") else "unknown"
        display = provider.get_model_display_name()
        console.print(f"[bold cyan]当前模型:[/bold cyan] {display} ([dim]{name}[/dim])")
        console.print("[dim]可用别名: pro, flash, dsv4pro, dsv4flash[/dim]")
        return

    new_model = provider.switch_model(args)
    display = provider.get_model_display_name()
    status_line.set_model(new_model, display)
    console.print(f"[bold cyan]已切换到:[/bold cyan] {display} ([dim]{new_model}[/dim])")


def main():
    # 初始化组件
    provider = get_llm_provider()
    tool_registry = create_default_registry()
    session_manager = SessionManager()
    agent = AgentLoop(provider, tool_registry)
    status_line = StatusLine(console)

    # 设置初始模型
    model_name = provider.model if hasattr(provider, "model") else "unknown"
    display_name = provider.get_model_display_name()
    status_line.set_model(model_name, display_name)

    # 创建会话
    session = session_manager.create_session()

    # 显示启动信息
    tool_count = len(tool_registry.list_tools())
    console.print(
        f"[bold cyan]Coomi Agent[/bold cyan] "
        f"[dim]({display_name}, {tool_count} tools)[/dim]"
    )
    console.print("[dim]命令: /model [pro|flash] | /clear | exit[/dim]\n")

    while True:
        try:
            user_input = console.input("[bold blue]You:[/bold blue] ")
        except (EOFError, KeyboardInterrupt):
            break

        if user_input.lower() in ("exit", "quit"):
            break

        # 处理 /model 命令
        stripped = user_input.strip()
        if stripped.startswith("/model"):
            args = stripped[6:].strip()
            handle_model_command(provider, status_line, args)
            continue

        # 处理 /clear 命令
        if stripped == "/clear":
            session = session_manager.create_session()
            console.print("[dim]会话已清除[/dim]\n")
            continue

        # 流式输出 Agent 响应
        console.print("[bold green]Agent:[/bold green] ", end="")
        renderer = StreamRenderer(console)
        renderer.start()

        try:
            for chunk in agent.run_stream(session, user_input):
                if isinstance(chunk, dict):
                    chunk_type = chunk.get("type")

                    if chunk_type == "tool_start":
                        # 工具执行 - 完成当前渲染
                        renderer.finish()
                        console.print("\n[dim italic]⚙ 执行工具中...[/dim italic]")
                        # 重新开始渲染
                        renderer = StreamRenderer(console)
                        renderer.start()

                    elif chunk_type == "usage":
                        # 更新状态栏（不立即渲染，等最后统一渲染）
                        pass

                elif isinstance(chunk, str):
                    renderer.write(chunk)

            # 完成流式渲染
            renderer.finish()
            console.print()

            # 渲染最终状态栏（只渲染一次）
            status_line.update_session_usage(session.token_usage)
            status_line.render_final()
            console.print()  # 额外空行分隔

        except Exception as e:
            if renderer.is_started:
                renderer.finish()
            console.print(f"\n[red]Error: {e}[/red]")

    console.print("[dim]再见！[/dim]")


if __name__ == "__main__":
    main()
