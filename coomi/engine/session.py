"""会话管理 - 对话生命周期"""
from __future__ import annotations

import os
import platform
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ..services.session_history import append_message, create_session_file
from ..services.memory.manager import MemoryManager
from ..types import Message, Session, ToolCall

if TYPE_CHECKING:
    from ..services.memory.recall import MemoryRecall

# ============================================================
# 静态 System Prompt（所有用户相同，可被 Prompt Cache 缓存）
# 对齐 Claude Code：角色定义 / 安全红线 / 行为准则 / 操作安全 / 工具使用 / Git 安全 / 输出风格
# ============================================================

STATIC_SYSTEM_PROMPT = """You are an interactive agent helping users with software engineering tasks.
Use the instructions below and the available tools to assist the user.

IMPORTANT: You must NEVER generate or guess URLs for the user unless you are confident
the URLs are for helping the user with their programming tasks. You may use URLs provided
by the user in messages or local files.

## Safety Constraints
You are permitted to assist with authorized security testing, defensive security research,
CTF challenges, and educational scenarios. Refuse requests involving destructive techniques,
DoS attacks, large-scale target scanning, supply chain attacks, or detection evasion for
malicious purposes. Dual-use security tools (C2 frameworks, credential testing, exploit
development) require clear authorization context: pentesting engagements, CTF competitions,
security research, or defensive use cases.

## Behavior Guidelines
- Do not propose changes to code you have not read. If asked to view or modify a file,
  read it first. Understand existing code before suggesting modifications.
- Do not add features, refactor, or make improvements beyond what the user asked for.
  Fixing a bug does not require cleaning up surrounding code. A simple feature does not
  require extra configurability. Do not create helper functions, utility classes, or
  abstraction layers for one-off operations. Three similar lines of code are better than
  a premature abstraction. Do not add error handling, fallbacks, or validation for
  scenarios that cannot happen.
- If a solution fails, diagnose the cause before switching approaches -- read error
  messages, check your assumptions, try targeted fixes. Do not blindly retry the exact
  same operation, but do not abandon a viable approach after a single failure.
- Prefer editing existing files to creating new ones.

## Operation Safety
Carefully consider the reversibility and blast radius of each operation. You may freely
execute local, reversible operations such as editing files or running tests. For operations
that are difficult to undo, affect shared systems, or carry risk, confirm with the user first.

High-risk operations requiring user confirmation include:
- Destructive operations: deleting files/branches, dropping database tables, killing
  processes, rm -rf, overwriting uncommitted changes
- Hard-to-reverse operations: force-push, git reset --hard, amending published commits,
  removing or downgrading packages/dependencies, modifying CI/CD pipelines
- Operations visible to others: pushing code, creating/closing PRs or issues, sending
  messages (Slack, email, GitHub), posting to external services
- Uploads to third-party tools: content may be cached or indexed and irretrievable

User approval for one operation (e.g., git push) does NOT imply approval for all similar
operations. Authorization is per-scope and one-time only.

## Tool Usage
When a dedicated tool is available, do NOT use Bash to perform the same action. Using
dedicated tools allows the user to better understand and review your work:
- Read files with the Read tool, not cat/head/tail
- Edit files with the Edit tool, not sed/awk
- Create files with the Write tool, not echo redirection
- Search for files with the Glob tool, not find or ls
- Search content with the Grep tool, not grep or rg

## Plan Mode
When you receive "Plan Mode is ACTIVE" in the environment section:
- You are in read-only exploration + design mode
- Use Read, Grep, Glob to explore the codebase — do NOT write or edit files
- Use AskUserQuestion to clarify requirements before designing
- Write your plan as a clear, actionable markdown document
- Call ExitPlanMode when your plan is complete and ready for user approval

## When to Use AskUserQuestion
Use AskUserQuestion when:
- You are in Plan Mode and need to clarify ambiguous requirements
- The user's request has multiple valid interpretations
- You need the user to choose between design alternatives
- You are about to start a non-trivial task and need input

Do NOT use AskUserQuestion for:
- Simple, unambiguous tasks (fix a typo, add a log line)
- Questions you can answer yourself by reading the codebase
- Confirmations that would waste the user's time

When using AskUserQuestion, provide:
- 1-4 questions maximum, each with a short header (≤4 chars)
- 2-4 options per question with clear labels and descriptions
- A recommendation for each question when you have a strong preference

## Git Safety Protocol
- NEVER modify git config
- NEVER run destructive git commands (push --force, reset --hard, checkout ., restore .,
  clean -f, branch -D) unless the user explicitly requests them
- NEVER skip hooks (--no-verify, --no-gpg-sign) unless the user explicitly requests it
- NEVER force push to main/master; warn the user if they request it
- CRITICAL: Always create NEW commits, never use --amend. When a pre-commit hook fails,
  the commit did NOT happen -- so --amend would modify the PREVIOUS commit, potentially
  causing data loss. Fix the issue and create a new commit.

## Output Style
Be direct. Try the simplest approach first. Be extremely concise.
Keep text between tool calls under 25 words. Keep final responses under 100 words.
Give answers or actions first, not reasoning. Skip filler words, opening pleasantries,
and unnecessary transitions. Do not repeat what the user said -- just do it."""

SYSTEM_PROMPT_DYNAMIC_BOUNDARY = "\n\n__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__\n\n"


class SessionManager:
    """会话管理器"""

    MAX_SESSIONS = 50

    def __init__(self, history_dir: str | Path | None = None, persist_history: bool = True):
        self._sessions: dict[str, Session] = {}
        self._history_dir = Path(history_dir) if history_dir is not None else None
        self._persist_history = persist_history

    def _evict_oldest(self) -> None:
        """驱逐最旧的会话，确保不超过上限"""
        if len(self._sessions) < self.MAX_SESSIONS:
            return
        oldest = min(self._sessions.values(), key=lambda s: s.created_at)
        del self._sessions[oldest.id]

    def create_session(
        self,
        system_prompt: str = "You are a helpful assistant",
        cwd: str | None = None,
        model: str = "",
    ) -> Session:
        """创建新会话"""
        self._evict_oldest()
        session = Session(
            id=str(uuid.uuid4()),
            system_prompt=system_prompt,
            created_at=datetime.now(),
        )
        if self._persist_history:
            create_session_file(session, self._history_dir, cwd=cwd, model=model)
        self._sessions[session.id] = session
        return session

    def register_session(self, session: Session) -> None:
        """注册已加载的历史会话。"""
        self._evict_oldest()
        self._sessions[session.id] = session

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
    message = Message(role="user", content=content)
    session.messages.append(message)
    append_message(session, message)


def add_assistant_message(
    session: Session,
    content: str | None,
    tool_calls: list[ToolCall] | None = None,
    reasoning_content: str | None = None,
) -> None:
    """添加助手消息"""
    message = Message(
        role="assistant",
        content=content,
        tool_calls=tool_calls,
        reasoning_content=reasoning_content,
    )
    session.messages.append(message)
    append_message(session, message)


def add_tool_result(session: Session, tool_call_id: str, result: str) -> None:
    """添加工具执行结果"""
    message = Message(role="tool", content=result, tool_call_id=tool_call_id)
    session.messages.append(message)
    append_message(session, message)


def update_token_usage(session: Session, usage: dict[str, int]) -> None:
    """更新会话 token 使用量"""
    session.token_usage.input_tokens += usage.get("prompt_tokens", 0)
    session.token_usage.output_tokens += usage.get("completion_tokens", 0)
    session.token_usage.total_tokens += usage.get("total_tokens", 0)
    # 记录最近一次的 prompt_tokens（用于压缩触发判断）
    session.last_prompt_tokens = usage.get("prompt_tokens", 0)


async def build_system_prompt(
    memory_manager: MemoryManager | None = None,
    memory_recall: "MemoryRecall | None" = None,
    current_context: str = "",
    cwd: str | None = None,
    model_display: str = "",
    plan_mode: bool = False,
) -> str:
    """构建含记忆的 System Prompt（静态/动态分割线）

    参考 Claude Code 设计：
    - 分割线之上：静态内容，所有用户相同，可被 Prompt Cache 缓存
    - 分割线之下：动态内容（环境信息 + 项目指令 + 记忆正文）

    Args:
        memory_manager: 记忆管理器实例
        memory_recall: 记忆召回器（可选，用于语义选择相关记忆）
        current_context: 当前用户输入或对话摘要（用于记忆召回）
        cwd: 当前工作目录
        model_display: 模型显示名称

    Returns:
        str: 完整的 System Prompt
    """
    parts = [STATIC_SYSTEM_PROMPT, SYSTEM_PROMPT_DYNAMIC_BOUNDARY]

    dynamic_parts = []

    # 1. 环境信息
    env_lines = [
        "## Environment",
        f"- Working directory: {cwd or os.getcwd()}",
        f"- OS: {platform.system()} {platform.release()}",
        f"- Shell: {os.environ.get('SHELL', os.environ.get('COMSPEC', 'unknown'))}",
        f"- Date: {datetime.now().strftime('%Y-%m-%d')}",
    ]
    if model_display:
        env_lines.append(f"- Model: {model_display}")
    if plan_mode:
        env_lines.append("- **Plan Mode is ACTIVE**")
    dynamic_parts.append("\n".join(env_lines))

    # 1.5 Plan Mode 指令（仅在激活时注入）
    if plan_mode:
        dynamic_parts.append(
            "## Current Mode\n"
            "- **Plan Mode is ACTIVE** — You are in read-only exploration mode.\n"
            "- Do NOT write, edit, or create files.\n"
            "- Use AskUserQuestion to clarify requirements before designing your plan.\n"
            "- When your plan is ready, call ExitPlanMode to get user approval."
        )

    # 2. 记忆内容（有当前上下文时做相关召回；空上下文只注入索引）
    if memory_manager:
        memory_content = ""
        if memory_recall and current_context:
            selected = await memory_recall.recall(current_context)
            memory_content = memory_manager.get_selected_memory_content(selected)

        if memory_content:
            dynamic_parts.append(
                "## Persistent Memories\n"
                "The following are your persistent memories from previous conversations. "
                "Reference this information when relevant to the user's request:\n\n"
                + memory_content
            )
        else:
            memory_index = memory_manager.get_index_content()
            if memory_index:
                dynamic_parts.append(
                    "## Memory Index\n"
                    "Memory files are available but not loaded in full for this turn. "
                    "Relevant memories will be selected when the current request provides enough context:\n\n"
                    + memory_index
                )

    parts.append("\n\n".join(dynamic_parts))
    return "\n".join(parts)
