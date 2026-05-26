"""引擎层类型定义"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


@dataclass
class ToolCall:
    """工具调用"""
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Message:
    """消息"""
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    reasoning_content: str | None = None
    created_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict[str, Any]:
        """转换为API格式"""
        msg: dict[str, Any] = {"role": self.role}

        if self.content is not None:
            msg["content"] = self.content

        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments),
                    },
                }
                for tc in self.tool_calls
            ]

        if self.tool_call_id:
            msg["tool_call_id"] = self.tool_call_id

        if self.reasoning_content:
            msg["reasoning_content"] = self.reasoning_content

        return msg


@dataclass
class LLMResponse:
    """LLM响应"""
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    usage: dict[str, int] | None = None
    reasoning_content: str | None = None


@dataclass
class TokenUsage:
    """Token 使用量追踪"""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


@dataclass
class Session:
    """会话"""
    id: str
    system_prompt: str = "You are a helpful assistant"
    messages: list[Message] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    current_model: str | None = None
    last_prompt_tokens: int = 0  # 最近一次 API 调用的 prompt_tokens（真实值）

    def get_messages_for_api(self) -> list[dict[str, Any]]:
        """获取API格式的消息列表"""
        result = [{"role": "system", "content": self.system_prompt}]
        result.extend(msg.to_dict() for msg in self.messages)
        return result
