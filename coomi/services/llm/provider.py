"""Base interface for LLM providers."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

from ...types import LLMResponse


class LLMProvider(ABC):
    """Common interface implemented by all model providers."""

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs,
    ) -> LLMResponse:
        """Run a non-streaming model request."""
        pass

    @abstractmethod
    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        **kwargs,
    ) -> AsyncIterator[str]:
        """Stream plain text from the model."""
        pass

    @abstractmethod
    async def chat_stream_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream text, tool-call events, and usage events."""
        pass

    @abstractmethod
    def switch_model(self, model_name: str) -> str:
        """Switch the active model in memory."""
        pass

    @abstractmethod
    def get_model_display_name(self) -> str:
        """Return the human-readable model/provider display name."""
        pass

    def get_tool_protocol(self) -> str:
        """Return the resolved provider tool protocol."""
        config = getattr(self, "config", None)
        if config and hasattr(config, "resolved_tool_protocol"):
            return config.resolved_tool_protocol()
        return "native"

    def get_text_tool_mode(self) -> str:
        """Return disabled/structured/mimo text-tool parsing mode for this provider."""
        config = getattr(self, "config", None)
        if config and hasattr(config, "text_tool_mode"):
            return config.text_tool_mode()
        return "disabled"
