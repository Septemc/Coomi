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
    is_error: bool = False


@dataclass
class ToolCacheHit(AgentEvent):
    """工具结果来自缓存"""
    tool_name: str


@dataclass
class UsageUpdate(AgentEvent):
    """Token 用量更新"""
    usage: dict[str, int] = field(default_factory=dict)


@dataclass
class ConnectionRetry(AgentEvent):
    """流式连接在产生有效回复前中断，正在自动重试。"""
    attempt: int = 1
    max_attempts: int = 1
    delay: float = 0.0
    message: str = ""


@dataclass
class CompressionEvent(AgentEvent):
    """上下文压缩完成"""
    before: int = 0
    after: int = 0


@dataclass
class AgentError(AgentEvent):
    """Agent 执行错误
    
    is_fatal: True 表示致命错误（步骤确实失败），False 表示警告（可继续）
    - LLM API 降级错误: is_fatal=False
    - 迭代上限: is_fatal=False（会话可继续）
    - Loop 步骤失败: is_fatal=True（步骤确实执行失败）
    """
    message: str = ""
    is_fatal: bool = False


@dataclass
class AgentCancelled(AgentEvent):
    """用户取消了 Agent 执行"""


# ============================================================
# Loop 模式事件
# ============================================================

@dataclass
class LoopStepStart(AgentEvent):
    """Loop 步骤开始"""
    step_index: int = 0
    step_description: str = ""
    total_steps: int = 0


@dataclass
class LoopStepDone(AgentEvent):
    """Loop 步骤完成"""
    step_index: int = 0
    success: bool = True


@dataclass
class LoopProgress(AgentEvent):
    """Loop 进度更新"""
    current_step: int = 0
    total_steps: int = 0
    status: Any = None  # LoopStatus


@dataclass
class LoopIssueCreated(AgentEvent):
    """Loop 创建了一个 issue（跳过步骤）"""
    step_index: int = 0
    step_description: str = ""
