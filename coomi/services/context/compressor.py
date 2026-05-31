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

    async def compress(self, session: Session, context_window_size: int) -> list[Message]:
        """执行三层压缩

        Args:
            session: 当前会话
            context_window_size: 上下文窗口大小

        Returns:
            list[Message]: 压缩后的消息列表
        """
        messages = session.messages.copy()

        threshold = int(context_window_size * COMPRESS_THRESHOLD)

        # Layer 1: Microcompact - 清理老工具结果
        messages = self._microcompact(messages)

        # 检查是否还需要进一步压缩
        if _estimate_tokens_from_dicts([m.to_dict() for m in messages]) < threshold:
            session.messages = messages
            return messages

        # Layer 2: 消息裁剪 - 删除远古消息
        messages = self._trim_old_messages(messages)

        # 检查是否还需要进一步压缩
        if _estimate_tokens_from_dicts([m.to_dict() for m in messages]) < threshold:
            session.messages = messages
            return messages

        # Layer 3: LLM 摘要 - 全量压缩
        messages = await self._llm_summarize(messages, context_window_size)
        session.messages = messages
        return messages

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

        # 保留第一条 + 最近 N 条
        return [messages[0]] + messages[-(KEEP_RECENT_MESSAGES):]

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

            # 恢复最近的工具结果（模拟 Claude Code 的 "restore recently accessed files"）
            recent_tool_results = self._get_recent_tool_results(messages, limit=5)
            for msg in recent_tool_results:
                msg.tool_call_id = None  # 清除悬空引用，避免 API 校验失败
            restored_msgs = [summary_msg] + recent_tool_results

            return restored_msgs

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

    def _get_recent_tool_results(self, messages: list[Message], limit: int = 5) -> list[Message]:
        """获取最近的工具结果消息"""
        tool_results = [msg for msg in messages if msg.role == "tool"]
        return tool_results[-limit:] if tool_results else []

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
