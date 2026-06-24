"""LLM Provider 工厂 — 基于 ConfigManager 的多 Provider 管理"""
from __future__ import annotations

from .config import ConfigManager, ProviderConfig
from .provider import LLMProvider


def get_llm_provider(provider_id: str | None = None) -> LLMProvider:
    """获取 LLM Provider

    Args:
        provider_id: 指定 provider ID，为 None 时使用当前激活的

    Returns:
        LLMProvider 实例
    """
    config_mgr = ConfigManager()
    config = None
    if provider_id:
        config = config_mgr.get_provider(provider_id)
    if not config:
        config = config_mgr.get_active()

    if not config:
        raise RuntimeError(
            "没有可用的 LLM Provider。请编辑 "
            + config_mgr.get_config_path_str()
            + " 添加配置。"
        )

    return _create_from_config(config)


def _create_from_config(config: ProviderConfig) -> LLMProvider:
    """根据 ProviderConfig 创建对应的 Provider 实例"""
    t = config.type.lower()

    if t == "deepseek":
        from .generic import GenericOpenAIProvider
        return GenericOpenAIProvider(config)
    elif t == "openai":
        from .openai import OpenAIProvider
        return OpenAIProvider(config)
    elif t == "anthropic":
        try:
            from .anthropic import AnthropicProvider
            return AnthropicProvider(config)
        except ImportError:
            raise RuntimeError(
                "Anthropic provider 需要安装 anthropic 包：\n"
                "  pip install coomi-agent[anthropic]"
            )
    else:
        from .generic import GenericOpenAIProvider
        return GenericOpenAIProvider(config)


def create_fast_provider(main_provider: LLMProvider) -> LLMProvider | None:
    """为 extractor/recall 创建轻量 Provider

    读取当前 provider 的 config.fast_model，如果不为 null，
    创建同类型、同 api_key/base_url 但 model=fast_model 的新实例。

    Args:
        main_provider: 主 Provider 实例

    Returns:
        轻量 Provider 或 None（无 fast_model 配置时）
    """
    config = getattr(main_provider, "config", None)
    if not config or not config.fast_model:
        return None

    # 创建同类型的轻量 Provider
    fast_config = ProviderConfig(
        id=f"{config.id}-fast",
        type=config.type,
        display=f"{config.display} (Fast)",
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.fast_model,
        tool_protocol=config.tool_protocol,
        fast_model=None,  # 避免递归
    )
    return _create_from_config(fast_config)


def get_config_manager() -> ConfigManager:
    """获取 ConfigManager 单例（供 CLI 命令使用）"""
    return ConfigManager()
