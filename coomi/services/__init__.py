# 服务层 - 基础设施
from .llm.provider import LLMProvider
from .llm.factory import get_llm_provider

# 保留旧接口兼容
from .llm.llm import LLMService

__all__ = ["LLMProvider", "get_llm_provider", "LLMService"]
