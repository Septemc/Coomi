"""AgentEvent 类型系统 — AgentLoop 与 UI 之间的类型安全通信协议"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentEvent:
    """所有 Agent 事件的基类"""


@dataclass
class TextChunk(AgentEvent):
    """流式文本内容"""
    content: str


@dataclass
class ReasoningChunk(AgentEvent):
    """推理/思考内容片段 (DeepSeek thinking mode)"""
    content: str


@dataclass
class ToolStart(AgentEvent):
    """工具即将开始执行"""
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolRunning(AgentEvent):
    """工具执行中"""
    tool_name: str


@dataclass
class ToolDone(AgentEvent):
    """工具执行完成"""
    tool_name: str
    elapsed: float = 0.0
    result_preview: str | None = None


@dataclass
class ToolCacheHit(AgentEvent):
    """工具结果来自缓存"""
    tool_name: str


@dataclass
class UsageUpdate(AgentEvent):
    """Token 用量更新"""
    usage: dict[str, int] = field(default_factory=dict)


@dataclass
class CompressionEvent(AgentEvent):
    """上下文压缩完成"""
    before: int = 0
    after: int = 0


@dataclass
class AgentError(AgentEvent):
    """Agent 执行错误"""
    message: str = ""


@dataclass
class AgentCancelled(AgentEvent):
    """用户取消了 Agent 执行"""
