"""LLM Provider 工厂函数"""
from __future__ import annotations

import os

from .provider import LLMProvider


def get_llm_provider(provider: str | None = None) -> LLMProvider:
    """根据配置返回 LLM Provider

    Args:
        provider: 厂商名称，如果为 None 则从环境变量 LLM_PROVIDER 读取

    Returns:
        LLMProvider: 具体的 Provider 实例

    Raises:
        ValueError: 未知的 provider
    """
    provider = provider or os.getenv("LLM_PROVIDER", "deepseek")
    provider = provider.lower()

    if provider == "deepseek":
        from .deepseek import DeepSeekProvider
        return DeepSeekProvider()

    elif provider == "openai":
        from .openai_provider import OpenAIProvider
        return OpenAIProvider()

    elif provider == "anthropic":
        from .anthropic import AnthropicProvider
        return AnthropicProvider()

    else:
        supported = ["deepseek", "openai", "anthropic"]
        raise ValueError(
            f"Unknown provider: '{provider}'. Supported: {', '.join(supported)}"
        )
