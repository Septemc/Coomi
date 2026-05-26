"""Agent主循环 - 核心引擎"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

from ..services.context.cache import ToolResultCache
from ..services.context.compressor import ContextCompressor
from ..services.llm.provider import LLMProvider
from ..tools.registry import ToolRegistry
from ..types import ToolCall
from ..ui.events import (
    AgentCancelled,
    AgentError,
    AgentEvent,
    CompressionEvent,
    ReasoningChunk,
    TextChunk,
    ToolCacheHit,
    ToolDone,
    ToolRunning,
    ToolStart,
    UsageUpdate,
)
from .session import Session, add_assistant_message, add_tool_result, add_user_message, update_token_usage

MAX_ITERATIONS = 50
MAX_RETRIES = 3


class CancelToken:
    """异步取消令牌"""

    def __init__(self):
        self._event = asyncio.Event()
        self._input_buffer: str | None = None

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def reset(self) -> None:
        self._event.clear()
        self._input_buffer = None

    def set_input_buffer(self, text: str) -> None:
        self._input_buffer = text

    def get_input_buffer(self) -> str | None:
        return self._input_buffer


class AgentLoop:
    """Agent主循环 - 协调LLM调用和工具执行

    核心流程：
    1. 拼接消息（系统提示 + 历史 + 用户输入）
    2. 调用LLM
    3. 如果有工具调用 → 执行工具 → 递归继续
    4. 如果无工具调用 → 返回结果

    交互式工具（is_interactive=True）走 run_async 路径，可 await UI 回调。
    """

    def __init__(self, llm: LLMProvider, tool_registry: ToolRegistry,
                 context_window_size: int = 256_000, app_context: Any = None):
        self.llm = llm
        self.tool_registry = tool_registry
        self.context_window_size = context_window_size
        self.compressor = ContextCompressor(llm)
        self.cache = ToolResultCache()
        self._cancel_token = CancelToken()
        self.app_context = app_context  # CoomiApp 实例，交互式工具需要
        self._plan_mode: bool = False

    @property
    def plan_mode(self) -> bool:
        return self._plan_mode

    def set_plan_mode(self, active: bool) -> None:
        self._plan_mode = active

    @property
    def cancel_token(self) -> CancelToken:
        return self._cancel_token

    async def _execute_tool_async(self, session: Session, tool_call: ToolCall) -> str:
        """异步执行工具调用

        交互式工具（is_interactive=True）走 run_async 路径，可 await UI 回调。
        普通工具在线程池中运行（asyncio.to_thread）。
        """
        cached = self.cache.get(tool_call.name, tool_call.arguments)
        if cached:
            return cached

        try:
            tool = self.tool_registry.get(tool_call.name)
            if tool and tool.is_interactive:
                # 交互式工具走 async 路径（阻塞等待用户输入）
                result = await tool.run_async(tool_call.arguments, self.app_context)
            else:
                # 普通工具在线程池中执行
                result = await asyncio.to_thread(self.tool_registry.execute_sync, tool_call)
            result_text = (result.output if result.success else f"Error: {result.error}") or ""
        except Exception as e:
            result_text = f"Tool execution crashed: {e}"

        self.cache.put(tool_call.name, tool_call.arguments, result_text)
        return result_text

    async def _check_compress(self, session: Session) -> CompressionEvent | None:
        """检查是否需要压缩，需要时执行并返回事件"""
        if self.compressor.should_compress(session, self.context_window_size):
            before_count = len(session.messages)
            compressed = await self.compressor.compress(session, self.context_window_size)
            session.messages = compressed
            return CompressionEvent(before=before_count, after=len(compressed))
        return None

    async def _chat_with_retry(self, messages, tools=None):
        """带重试的 LLM 调用"""
        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                return await self.llm.chat(messages, tools=tools)
            except Exception as e:
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2 ** attempt)
        raise last_error  # type: ignore[misc]

    async def run_stream(self, session: Session, user_input: str) -> AsyncIterator[AgentEvent]:
        """执行Agent主循环（异步流式输出）

        Yields:
            AgentEvent 子类: 结构化事件（文本、工具调用、用量更新、压缩、错误等）
        """
        add_user_message(session, user_input)
        self._cancel_token.reset()

        compress_event = await self._check_compress(session)
        if compress_event:
            yield compress_event

        iteration = 0
        while iteration < MAX_ITERATIONS:
            # 取消检查点
            if self._cancel_token.is_cancelled:
                yield AgentCancelled()
                return

            iteration += 1
            messages = session.get_messages_for_api()
            tools = self.tool_registry.get_tool_definitions() or None

            full_content = ""
            full_reasoning = ""
            tool_calls_data = []

            async for chunk in self.llm.chat_stream_with_tools(messages, tools=tools):
                # 流式过程中也检查取消
                if self._cancel_token.is_cancelled:
                    yield AgentCancelled()
                    return

                if chunk["type"] == "reasoning_content":
                    full_reasoning += chunk["content"]
                    yield ReasoningChunk(content=chunk["content"])
                elif chunk["type"] == "tool_call_start":
                    yield ToolStart(tool_name=chunk["tool_name"], arguments={})
                elif chunk["type"] == "content":
                    full_content += chunk["content"]
                    yield TextChunk(content=chunk["content"])
                elif chunk["type"] == "tool_call":
                    tool_calls_data.append(chunk["data"])
                elif chunk["type"] == "usage":
                    update_token_usage(session, chunk["data"])
                    yield UsageUpdate(usage=chunk["data"])

            if tool_calls_data:
                tool_calls = [
                    ToolCall(
                        id=tc["id"] or f"call_{i}_{id(tc)}",
                        name=tc["name"],
                        arguments=tc["arguments"],
                    )
                    for i, tc in enumerate(tool_calls_data)
                ]
                reasoning = full_reasoning or None
                add_assistant_message(session, full_content or None, tool_calls, reasoning)

                for tool_call in tool_calls:
                    yield ToolStart(tool_name=tool_call.name, arguments=tool_call.arguments)

                    cached = self.cache.get(tool_call.name, tool_call.arguments)
                    if cached:
                        result_text = cached
                        yield ToolCacheHit(tool_name=tool_call.name)
                    else:
                        yield ToolRunning(tool_name=tool_call.name)
                        result_text = await self._execute_tool_async(session, tool_call)
                        yield ToolDone(
                            tool_name=tool_call.name,
                            result_preview=result_text[:500] if result_text else None,
                        )

                    add_tool_result(session, tool_call.id, result_text)

                # 工具执行后取消检查
                if self._cancel_token.is_cancelled:
                    yield AgentCancelled()
                    return

                compress_event = await self._check_compress(session)
                if compress_event:
                    yield compress_event

                continue
            else:
                add_assistant_message(session, full_content, reasoning_content=full_reasoning or None)
                return
        else:
            yield AgentError(message="达到最大迭代次数上限")
