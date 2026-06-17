"""DeepSeek Provider 实现"""
from __future__ import annotations

from typing import Any, AsyncIterator

from .config import ProviderConfig
from .generic import GenericOpenAIProvider


class DeepSeekProvider(GenericOpenAIProvider):
    """DeepSeek LLM Provider — 支持 thinking mode"""

    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        self.is_flash = "flash" in self.model.lower()

    def switch_model(self, model_name: str) -> str:
        self.model = model_name
        self.is_flash = "flash" in self.model.lower()
        return self.model

    def _build_params(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        tool_choice: str = "auto",
    ) -> dict[str, Any]:
        params = super()._build_params(messages, tools, stream, tool_choice)
        if tools:
            params.setdefault("tool_choice", tool_choice)
        # DeepSeek-compatible APIs may require thinking disabled when tools are used.
        params["extra_body"] = {"thinking": {"type": "disabled"}}
        return params

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        **kwargs,
    ) -> AsyncIterator[str]:
        params = self._build_params(messages, stream=True)
        params["reasoning_effort"] = "high"
        response = await self.client.chat.completions.create(**params)
        async for chunk in response:
            if chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
