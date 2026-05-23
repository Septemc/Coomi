"""Agent主循环 - 核心引擎"""
from __future__ import annotations

import time
from typing import Any, Iterator

from ..services.context.cache import ToolResultCache
from ..services.context.compressor import ContextCompressor
from ..services.llm.provider import LLMProvider
from ..tools.registry import ToolRegistry
from ..types import ToolCall
from .session import Session, add_assistant_message, add_tool_result, add_user_message, update_token_usage

MAX_ITERATIONS = 50
MAX_RETRIES = 3


class AgentLoop:
    """Agent主循环 - 协调LLM调用和工具执行

    核心流程：
    1. 拼接消息（系统提示 + 历史 + 用户输入）
    2. 调用LLM
    3. 如果有工具调用 → 执行工具 → 递归继续
    4. 如果无工具调用 → 返回结果
    """

    def __init__(self, llm: LLMProvider, tool_registry: ToolRegistry, context_window_size: int = 256_000):
        self.llm = llm
        self.tool_registry = tool_registry
        self.context_window_size = context_window_size
        self.compressor = ContextCompressor(llm)
        self.cache = ToolResultCache()

    def _execute_tool(self, session: Session, tool_call: ToolCall) -> str:
        """执行单个工具调用，含缓存检查和错误隔离"""
        cached = self.cache.get(tool_call.name, tool_call.arguments)
        if cached:
            return cached

        try:
            result = self.tool_registry.execute_sync(tool_call)
            result_text = result.output if result.success else f"Error: {result.error}"
        except Exception as e:
            result_text = f"Tool execution crashed: {e}"

        self.cache.put(tool_call.name, tool_call.arguments, result_text)
        return result_text

    def _check_compress(self, session: Session) -> dict | None:
        """检查是否需要压缩，需要时执行并返回压缩信息"""
        if self.compressor.should_compress(session, self.context_window_size):
            before_count = len(session.messages)
            compressed = self.compressor.compress(session, self.context_window_size)
            session.messages = compressed
            return {"type": "compression", "before": before_count, "after": len(compressed)}
        return None

    def _chat_with_retry(self, messages, tools=None):
        """带重试的 LLM 调用"""
        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                return self.llm.chat(messages, tools=tools)
            except Exception as e:
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)
        raise last_error  # type: ignore[misc]

    def _chat_stream_with_retry(self, messages, tools=None):
        """带重试的流式 LLM 调用"""
        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                yield from self.llm.chat_stream_with_tools(messages, tools=tools)
                return
            except Exception as e:
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)
        raise last_error  # type: ignore[misc]

    def run(self, session: Session, user_input: str) -> str:
        """执行Agent主循环（非流式）"""
        add_user_message(session, user_input)

        self._check_compress(session)

        iteration = 0
        while iteration < MAX_ITERATIONS:
            iteration += 1
            messages = session.get_messages_for_api()
            tools = self.tool_registry.get_tool_definitions() or None
            response = self._chat_with_retry(messages, tools=tools)

            if response.usage:
                update_token_usage(session, response.usage)

            if response.tool_calls:
                add_assistant_message(session, response.content, response.tool_calls, response.reasoning_content)
                for tool_call in response.tool_calls:
                    result_text = self._execute_tool(session, tool_call)
                    add_tool_result(session, tool_call.id, result_text)

                self._check_compress(session)
                continue
            else:
                add_assistant_message(session, response.content, reasoning_content=response.reasoning_content)
                return response.content or ""

        return "已达到最大迭代次数上限"

    def run_stream(self, session: Session, user_input: str) -> Iterator[str | dict]:
        """执行Agent主循环（流式输出）"""
        add_user_message(session, user_input)

        compress_info = self._check_compress(session)
        if compress_info:
            yield compress_info

        iteration = 0
        while iteration < MAX_ITERATIONS:
            iteration += 1
            messages = session.get_messages_for_api()
            tools = self.tool_registry.get_tool_definitions() or None

            full_content = ""
            full_reasoning = ""
            tool_calls_data = []

            for chunk in self._chat_stream_with_retry(messages, tools=tools):
                if chunk["type"] == "reasoning_content":
                    full_reasoning += chunk["content"]
                elif chunk["type"] == "content":
                    full_content += chunk["content"]
                    yield chunk["content"]
                elif chunk["type"] == "tool_call":
                    tool_calls_data.append(chunk["data"])
                elif chunk["type"] == "usage":
                    update_token_usage(session, chunk["data"])
                    yield {"type": "usage", "data": chunk["data"]}

            if tool_calls_data:
                tool_calls = [
                    ToolCall(id=tc["id"], name=tc["name"], arguments=tc["arguments"])
                    for tc in tool_calls_data
                ]
                reasoning = full_reasoning or None
                add_assistant_message(session, full_content or None, tool_calls, reasoning)

                for tool_call in tool_calls:
                    yield {"type": "tool_start", "tool_name": tool_call.name, "arguments": tool_call.arguments}

                    cached = self.cache.get(tool_call.name, tool_call.arguments)
                    if cached:
                        result_text = cached
                        yield {"type": "tool_cache_hit", "tool_name": tool_call.name}
                    else:
                        result_text = self._execute_tool(session, tool_call)

                    add_tool_result(session, tool_call.id, result_text)

                compress_info = self._check_compress(session)
                if compress_info:
                    yield compress_info

                continue
            else:
                add_assistant_message(session, full_content, reasoning_content=full_reasoning or None)
                return
        else:
            yield {"type": "error", "message": "达到最大迭代次数上限"}
