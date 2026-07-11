"""首次运行配置引导 — 支持环境变量、.env 文件、交互式配置"""
from __future__ import annotations

import json
import os
from pathlib import Path
from rich.console import Console
from rich.prompt import Prompt

console = Console()

# 环境变量映射表
ENV_MAPPINGS = {
    "deepseek": {
        "api_key": "DEEPSEEK_API_KEY",
        "base_url": "DEEPSEEK_BASE_URL",
        "model": "DEEPSEEK_MODEL",
    },
    "openai": {
        "api_key": "OPENAI_API_KEY",
        "model": "OPENAI_MODEL",
    },
    "anthropic": {
        "api_key": "ANTHROPIC_API_KEY",
        "model": "ANTHROPIC_MODEL",
    },
}

PROVIDER_TEMPLATES = {
    "deepseek": {
        "type": "generic",
        "display": "DeepSeek V4",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-pro",
        "fast_model": "deepseek-v4-flash",
    },
    "openai": {
        "type": "openai",
        "display": "GPT-4o",
        "model": "gpt-4o",
    },
    "anthropic": {
        "type": "anthropic",
        "display": "Claude Sonnet 4",
        "model": "claude-sonnet-4-20250514",
        "fast_model": "claude-haiku-4-5-20251001",
    },
    "generic": {
        "type": "generic",
        "display": "Custom Provider",
        "model": "",
        "base_url": "",
    },
}


def _load_dotenv() -> dict[str, str]:
    """加载 .env 文件（如果存在）"""
    env_path = Path.cwd() / ".env"
    env_vars: dict[str, str] = {}
    if env_path.exists():
        try:
            from dotenv import dotenv_values
            env_vars = {
                str(key): str(value)
                for key, value in dotenv_values(env_path).items()
                if value is not None
            }
        except ImportError:
            pass
    return env_vars


def _detect_provider_from_env(env_vars: dict[str, str]) -> str | None:
    """从环境变量检测 Provider 类型"""
    # 优先检查 LLM_PROVIDER 环境变量
    provider = env_vars.get("LLM_PROVIDER") or os.getenv("LLM_PROVIDER")
    if provider:
        return provider.lower()

    # 按优先级检测
    if env_vars.get("DEEPSEEK_API_KEY") or os.getenv("DEEPSEEK_API_KEY"):
        return "deepseek"
    if env_vars.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY"):
        return "openai"
    if env_vars.get("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"

    return None


def _build_config_from_env(provider_type: str, env_vars: dict[str, str]) -> dict:
    """从环境变量构建配置"""
    template = PROVIDER_TEMPLATES.get(provider_type, PROVIDER_TEMPLATES["generic"]).copy()
    mappings = ENV_MAPPINGS.get(provider_type, {})

    # 从环境变量填充配置
    for config_key, env_key in mappings.items():
        value = env_vars.get(env_key) or os.getenv(env_key)
        if value:
            template[config_key] = value

    # 设置 API Key
    api_key = template.get("api_key", "")
    if not api_key:
        return {}

    return {
        "version": 1,
        "active": "default",
        "providers": {
            "default": template
        }
    }


def run_first_time_setup() -> bool:
    """首次运行配置引导

    配置优先级：
    1. 环境变量（DEEPSEEK_API_KEY 等）
    2. .env 文件
    3. 交互式输入

    Returns:
        bool: True 表示配置成功，False 表示用户取消
    """
    console.print("\n[bold cyan]欢迎使用 Coomi Agent![/bold cyan]\n")

    # Step 1: 尝试从环境变量自动配置
    env_vars = _load_dotenv()
    detected_type = _detect_provider_from_env(env_vars)

    if detected_type and detected_type in PROVIDER_TEMPLATES:
        config = _build_config_from_env(detected_type, env_vars)
        if config:
            console.print(f"[green]检测到 {detected_type.upper()} 环境变量，自动配置中...[/green]")
            _save_config(config)
            provider_name = config["providers"]["default"]["display"]
            console.print(f"[green]已自动配置: {provider_name}[/green]\n")
            return True

    # Step 2: 交互式配置
    console.print("首次运行需要配置 LLM Provider。\n")

    # 选择 Provider 类型
    console.print("[bold]支持的 Provider 类型：[/bold]")
    console.print("  1. [cyan]Generic[/cyan] — 任意兼容 OpenAI API 的服务（DeepSeek/MiMo/MiniMax 等）")
    console.print("  2. [cyan]OpenAI[/cyan] — GPT-4o 等")
    console.print("  3. [cyan]Anthropic[/cyan] — Claude 系列")
    console.print()

    choice = Prompt.ask(
        "请选择 Provider 类型",
        choices=["1", "2", "3"],
        default="1"
    )

    provider_map = {"1": "generic", "2": "openai", "3": "anthropic"}
    provider_type = provider_map[choice]
    template = PROVIDER_TEMPLATES[provider_type].copy()

    # 获取 API Key
    console.print()
    api_key = Prompt.ask(
        "[bold]请输入 API Key[/bold]",
        password=True,
    )

    if not api_key.strip():
        console.print("[red]API Key 不能为空[/red]")
        return False

    # Generic 需要额外配置
    if provider_type == "generic":
        base_url = Prompt.ask("[bold]请输入 Base URL[/bold]")
        model = Prompt.ask("[bold]请输入模型名[/bold]")
        template["base_url"] = base_url
        template["model"] = model
        template["display"] = model

    # 构建配置
    template["api_key"] = api_key.strip()

    config = {
        "version": 1,
        "active": "default",
        "providers": {
            "default": template
        }
    }

    _save_config(config)
    console.print("[dim]后续可通过 /model 命令或设置界面修改配置[/dim]\n")

    return True


def _save_config(config: dict) -> None:
    """保存配置到 providers.json"""
    config_dir = Path.home() / ".coomi" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "providers.json"

    config_path.write_text(
        json.dumps(config, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

    console.print(f"\n[green]配置已保存到 {config_path}[/green]")
