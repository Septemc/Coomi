"""会话管理 - 对话生命周期"""
from __future__ import annotations

import uuid
from datetime import datetime

from ..types import Message, Session, ToolCall


class SessionManager:
    """会话管理器"""

    def __init__(self):
        self._sessions: dict[str, Session] = {}

    def create_session(self, system_prompt: str = "You are a helpful assistant") -> Session:
        """创建新会话"""
        session = Session(
            id=str(uuid.uuid4()),
            system_prompt=system_prompt,
            created_at=datetime.now(),
        )
        self._sessions[session.id] = session
        return session

    def get_session(self, session_id: str) -> Session | None:
        """获取会话"""
        return self._sessions.get(session_id)

    def delete_session(self, session_id: str) -> bool:
        """删除会话"""
        if session_id in self._sessions:
            del self._sessions[session_id]
            return True
        return False

    def list_sessions(self) -> list[Session]:
        """列出所有会话"""
        return list(self._sessions.values())


def add_user_message(session: Session, content: str) -> None:
    """添加用户消息"""
    session.messages.append(Message(role="user", content=content))


def add_assistant_message(
    session: Session,
    content: str | None,
    tool_calls: list[ToolCall] | None = None,
) -> None:
    """添加助手消息"""
    session.messages.append(
        Message(role="assistant", content=content, tool_calls=tool_calls)
    )


def add_tool_result(session: Session, tool_call_id: str, result: str) -> None:
    """添加工具执行结果"""
    session.messages.append(
        Message(role="tool", content=result, tool_call_id=tool_call_id)
    )


def update_token_usage(session: Session, usage: dict[str, int]) -> None:
    """更新会话 token 使用量"""
    session.token_usage.input_tokens += usage.get("prompt_tokens", 0)
    session.token_usage.output_tokens += usage.get("completion_tokens", 0)
    session.token_usage.total_tokens += usage.get("total_tokens", 0)
