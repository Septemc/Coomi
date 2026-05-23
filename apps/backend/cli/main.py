import typer
from rich.console import Console
from rich.markdown import Markdown

app = typer.Typer()
console = Console()


@app.command()
def chat(message: str = typer.Argument(..., help="发送给Agent的消息")):
    """与Agent对话"""
    from apps.backend.core.services.llm.llm import LLMService

    llm = LLMService()
    messages = [
        {"role": "system", "content": "You are a helpful assistant"},
        {"role": "user", "content": message},
    ]

    console.print(f"[bold blue]You:[/bold blue] {message}")
    console.print("[bold green]Agent:[/bold green] ", end="")

    for chunk in llm.chat_stream(messages):
        console.print(chunk, end="", highlight=False)

    console.print()  # 换行


@app.command()
def interactive():
    """进入交互模式"""
    from apps.backend.core.services.llm.llm import LLMService

    llm = LLMService()
    messages = [{"role": "system", "content": "You are a helpful assistant"}]

    console.print("[bold cyan]Coomi Agent 交互模式[/bold cyan]")
    console.print("[dim]输入 'exit' 或 'quit' 退出[/dim]\n")

    while True:
        try:
            user_input = console.input("[bold blue]You:[/bold blue] ")
        except (EOFError, KeyboardInterrupt):
            break

        if user_input.lower() in ("exit", "quit"):
            break

        messages.append({"role": "user", "content": user_input})
        console.print("[bold green]Agent:[/bold green] ", end="")

        full_response = ""
        for chunk in llm.chat_stream(messages):
            console.print(chunk, end="", highlight=False)
            full_response += chunk

        messages.append({"role": "assistant", "content": full_response})
        console.print()


if __name__ == "__main__":
    app()
