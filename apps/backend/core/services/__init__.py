# 服务层 - 基础设施
from .provider import LLMProvider
from .factory import get_llm_provider

# 保留旧接口兼容
from .llm import LLMService

__all__ = ["LLMProvider", "get_llm_provider", "LLMService"]
