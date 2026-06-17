"""引擎层类型定义"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal


@dataclass
class ToolCall:
    """工具调用"""
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str | None = None
    parse_error: str | None = None


@dataclass
class Message:
    """消息"""
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    reasoning_content: str | None = None
    created_at: datetime = field(default_factory=datetime.now)

    def to_dict(self, include_reasoning: bool = False) -> dict[str, Any]:
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

        if include_reasoning and self.reasoning_content:
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
        from .services.context.message_guard import prepare_messages_for_api

        return prepare_messages_for_api(self, repair=True)


# ============================================================
# Loop 模式类型
# ============================================================

class LoopStatus(str, Enum):
    """Loop 执行状态"""
    RUNNING = "running"
    PAUSED_ISSUE = "paused_issue"       # 遇到需要人工处理的问题
    PAUSED_NETWORK = "paused_network"   # 网络断开等待重连
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Spec:
    """解析后的任务规格"""
    title: str
    goal: str
    steps: list[str]
    constraints: list[str]
    acceptance_criteria: list[str]
    resources: dict[str, str]           # 可用资源描述
    tools_allowed: list[str]            # 允许使用的工具
    tools_forbidden: list[str]          # 禁止使用的工具


@dataclass
class Checkpoint:
    """步骤检查点"""
    step_index: int
    step_summary: str                   # LLM 生成的步骤完成摘要
    files_changed: list[str]            # 已修改的文件路径列表
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class LoopSession:
    """Loop 模式会话"""
    loop_id: str
    spec: Spec
    status: LoopStatus = LoopStatus.RUNNING
    current_step: int = 0                # 当前执行到第几步（0-based）
    checkpoints: list[Checkpoint] = field(default_factory=list)
    retry_counts: dict[int, int] = field(default_factory=dict)  # step_index -> retry count
    started_at: datetime = field(default_factory=datetime.now)
    last_active_at: datetime = field(default_factory=datetime.now)
    loop_dir: Path | None = None         # .coomi/loops/{loop_id}/ 目录


class StepResult(str, Enum):
    """步骤执行结果"""
    SUCCESS = "success"
    RETRY = "retry"                      # 需要重试
    SKIP = "skip"                        # 跳过（写入 ISSUE.md 后）
    FAILED = "failed"                    # 最终失败
