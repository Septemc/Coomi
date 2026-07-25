"""上下文压缩器 - 三层压缩策略（对齐 Claude Code）

三层：
1. Microcompact：清理老工具结果（零 API 开销）
2. 消息裁剪：删除远古消息（零 API 开销）
3. LLM 摘要：全量压缩（用当前模型生成 9 段结构化摘要）
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ...types import Message, Session

# 压缩配置（对齐 Claude Code）
COMPRESS_THRESHOLD = 0.9     # 超过窗口 90% 即触发压缩
KEEP_RECENT_TOOL_RESULTS = 6  # 保留最近 N 个工具结果
KEEP_RECENT_MESSAGES = 8     # 裁剪后保留最近 N 条消息
COMPACT_MAX_OUTPUT_TOKENS = 16_000  # 摘要最大输出

# 可压缩的工具角色
COMPACTABLE_ROLES = {"tool"}

# 摘要 prompt（学 Claude Code 的 9 段结构）
SUMMARIZE_PROMPT = """请将以下对话历史压缩为结构化摘要。这是一个自动压缩过程，目的是在上下文窗口满时保留关键信息。

要求：
1. 必须保留所有用户的原始消息（原文保留，不要改写）
2. 保留关键的技术决策和代码变更
3. 保留未完成的任务
4. 使用中文

请按以下 9 个部分组织摘要：

## 1. Primary Request
用户的核心需求和意图

## 2. Key Technical Concepts
涉及的关键技术概念、架构决策

## 3. Files and Code Sections
涉及的重要文件路径和关键代码片段

## 4. Errors and Fixes
遇到的错误和对应的修复方案

## 5. Problem Solving
问题解决的过程和方法

## 6. All User Messages
所有用户的原始输入（逐条保留，标记为 user）

## 7. Pending Tasks
尚未完成的任务

## 8. Current Work
当前正在进行的工作状态

## 9. Optional Next Step
建议的下一步操作"""

# 压缩后能力提醒（三模式 + 核心工具）
# 压缩会丢失早期上下文，模型容易"忘记"自己具备的内置能力，
# 每次压缩后在消息流尾部补一条紧凑提醒，把这些能力重新拉回注意力。
CAPABILITY_REMINDER_MARKER = "[能力提醒]"
CAPABILITY_REMINDER = (
    f"{CAPABILITY_REMINDER_MARKER} 上下文刚被压缩，重申你可用的内置能力：\n"
    "- Plan 模式：只读探索+设计，先规划再动手；Loop 模式：拆解长线任务分步推进；"
    "遇到需求歧义或多种可选方案时用 AskUserQuestion 让用户拍板。\n"
    "- 核心工具：Read/Glob/Grep 读查、Edit/Write 改写、PowerShell/Bash 执行命令、"
    "WebSearch 查实时信息、Skill/MCP 扩展能力。有专用工具时不要用命令行替代。"
)


class ContextCompressor:
    """上下文压缩器

    触发条件：实时估算当前消息 token 数 > context_window * 90%
    始终从 session.get_messages_for_api() 估算，不含滞后数据。
    """

    def __init__(self, llm_provider=None):
        """
        Args:
            llm_provider: LLM Provider（用于 Layer 3 全量摘要）
        """
        self.llm = llm_provider

    def should_compress(self, session: Session, context_window_size: int) -> bool:
        """检查是否需要压缩

        始终用当前消息列表实时估算 token 数（反映即将发送的真实大小，
        包含新追加的用户消息 + 工具结果），阈值 = 窗口 * 90%。

        Args:
            session: 当前会话
            context_window_size: 上下文窗口大小

        Returns:
            bool: 是否需要压缩
        """
        threshold = int(context_window_size * COMPRESS_THRESHOLD)
        if session.last_prompt_tokens > 0:
            return session.last_prompt_tokens > threshold
        estimated = _estimate_tokens_from_dicts(session.get_messages_for_api())
        return estimated > threshold

    async def compress(
        self,
        session: Session,
        context_window_size: int,
        force: bool = False,
    ) -> list[Message]:
        """执行三层压缩

        Args:
            session: 当前会话
            context_window_size: 上下文窗口大小
            force: 是否强制压缩（用于 /compact）

        Returns:
            list[Message]: 压缩后的消息列表
        """
        messages = session.messages.copy()

        threshold = int(context_window_size * COMPRESS_THRESHOLD)

        # Layer 1: Microcompact - 清理老工具结果
        messages = self._microcompact(messages)

        # 检查是否还需要进一步压缩
        if not force and _estimate_tokens_from_messages(messages) < threshold:
            messages = self._inject_capability_reminder(messages)
            session.messages = messages
            return messages

        # Layer 2: 消息裁剪 - 删除远古消息
        messages = self._trim_old_messages(messages)

        # 检查是否还需要进一步压缩
        if not force and _estimate_tokens_from_messages(messages) < threshold:
            messages = self._inject_capability_reminder(messages)
            session.messages = messages
            return messages

        # Layer 3: LLM 摘要 - 全量压缩
        if force or _estimate_tokens_from_messages(messages) >= threshold:
            messages = await self._llm_summarize(messages, context_window_size)
        messages = self._inject_capability_reminder(messages)
        session.messages = messages
        return messages

    def _inject_capability_reminder(self, messages: list[Message]) -> list[Message]:
        """压缩后在消息流尾部补一条能力提醒（去重，避免多轮压缩累积）。

        先移除历史里已存在的提醒，再在末尾追加最新一条，保证任意时刻
        只有一条提醒且位于最后，紧贴即将发送的请求。
        """
        cleaned = [
            msg for msg in messages
            if not (msg.role == "user" and (msg.content or "").startswith(CAPABILITY_REMINDER_MARKER))
        ]
        cleaned.append(Message(
            role="user",
            content=CAPABILITY_REMINDER,
            created_at=datetime.now(),
        ))
        return cleaned

    def _microcompact(self, messages: list[Message]) -> list[Message]:
        """Layer 1: Microcompact - 清理老工具结果

        将超过 N 条的旧 tool message 内容替换为 [已清理]。
        """
        tool_count = 0
        result = []

        for msg in reversed(messages):
            if msg.role == "tool":
                tool_count += 1
                if tool_count > KEEP_RECENT_TOOL_RESULTS:
                    # 替换为清理标记
                    result.append(Message(
                        role="tool",
                        content="[cleared]",
                        tool_call_id=msg.tool_call_id,
                        created_at=msg.created_at,
                    ))
                else:
                    result.append(msg)
            else:
                result.append(msg)

        result.reverse()
        return result

    def _trim_old_messages(self, messages: list[Message]) -> list[Message]:
        """Layer 2: 消息裁剪 - 保留最近 N 条消息

        始终保留第一条（通常是系统消息或第一轮对话），
        然后保留最近 KEEP_RECENT_MESSAGES 条。
        """
        if len(messages) <= KEEP_RECENT_MESSAGES + 1:
            return messages

        groups = self._group_messages_for_trimming(messages)
        if len(groups) <= 2:
            return [msg for group in groups for msg in group]

        first_group = groups[0]
        tail_groups: list[list[Message]] = []
        kept_count = 0
        for group in reversed(groups[1:]):
            tail_groups.insert(0, group)
            kept_count += len(group)
            if kept_count >= KEEP_RECENT_MESSAGES:
                break

        return [msg for group in [first_group, *tail_groups] for msg in group]

    def _group_messages_for_trimming(self, messages: list[Message]) -> list[list[Message]]:
        """将 assistant 工具调用和紧随其后的 tool result 作为不可切分单元。"""
        groups: list[list[Message]] = []
        i = 0
        while i < len(messages):
            msg = messages[i]
            if msg.role == "assistant" and msg.tool_calls:
                expected = {tool_call.id for tool_call in msg.tool_calls}
                group = [msg]
                j = i + 1
                while j < len(messages) and messages[j].role == "tool":
                    if messages[j].tool_call_id in expected:
                        group.append(messages[j])
                    j += 1
                groups.append(group)
                i = j
                continue

            if msg.role == "tool":
                i += 1
                continue

            groups.append([msg])
            i += 1

        return groups

    async def _llm_summarize(self, messages: list[Message], context_window_size: int) -> list[Message]:
        """Layer 3: LLM 摘要 - 用当前模型生成 9 段结构化摘要

        模仿 Claude Code 的 compactConversation 行为：
        1. 把对话发给 LLM 生成摘要
        2. 摘要后恢复最近的工具结果
        """
        if not self.llm:
            # 无 LLM 时降级为简单裁剪
            return self._trim_old_messages(messages)

        # 构建对话文本
        conversation = self._format_conversation(messages)

        try:
            # 调用 LLM 生成摘要
            response = await self.llm.chat(
                messages=[
                    {"role": "user", "content": f"{SUMMARIZE_PROMPT}\n\n---\n\n{conversation}"}
                ],
                tools=None,
            )

            summary_content = response.content or "对话历史摘要"

            # 创建摘要消息
            summary_msg = Message(
                role="user",
                content=f"[上下文已自动压缩]\n\n{summary_content}",
                created_at=datetime.now(),
            )

            recent_context = self._get_recent_plain_messages(messages, limit=4)
            return [summary_msg] + recent_context

        except Exception:
            # 摘要失败，降级为消息裁剪
            return self._trim_old_messages(messages)

    def _format_conversation(self, messages: list[Message]) -> str:
        """将消息列表格式化为对话文本"""
        lines = []
        for msg in messages:
            if msg.role == "system":
                continue
            if msg.role == "tool":
                # 工具结果只保留前 500 字符
                content = (msg.content or "")[:500]
                lines.append(f"[tool result] {content}")
            elif msg.role == "assistant" and msg.tool_calls:
                tool_names = [tc.name for tc in msg.tool_calls]
                lines.append(f"[assistant] (调用工具: {', '.join(tool_names)})")
                if msg.content:
                    lines.append(f"  {msg.content[:200]}")
            else:
                content = (msg.content or "")[:1000]
                lines.append(f"[{msg.role}] {content}")
        return "\n".join(lines)

    def _get_recent_plain_messages(self, messages: list[Message], limit: int = 4) -> list[Message]:
        """获取最近的普通消息，摘要后避免恢复裸 tool 消息。"""
        plain: list[Message] = []
        for msg in messages:
            if msg.role in {"system", "tool"}:
                continue
            if msg.role == "assistant" and msg.tool_calls:
                continue
            plain.append(msg)
        return plain[-limit:]

def _estimate_tokens_from_dicts(messages: list[dict[str, Any]]) -> int:
    """从 API 格式的消息字典估算 token 数（模块级函数，供 Provider 层共享）

    英文约 4 chars/token，中文约 1.5，取 3 作为安全中间值，宁可提前触发。
    """
    total_chars = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        for tc in msg.get("tool_calls", []):
            args = tc.get("function", {}).get("arguments", "")
            total_chars += len(args)
    return max(1, total_chars // 3)


def _estimate_tokens_from_messages(messages: list[Message]) -> int:
    """Estimate tokens from the same repaired payload shape providers receive."""
    temp = Session(id="estimate", system_prompt="", messages=messages)
    return _estimate_tokens_from_dicts(temp.get_messages_for_api())
