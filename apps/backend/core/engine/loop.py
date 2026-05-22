"""Agent主循环 - 核心引擎"""
from __future__ import annotations

from typing import Any, Iterator

from ..services.provider import LLMProvider
from ..tools.registry import ToolRegistry
from .session import Session, add_assistant_message, add_tool_result, add_user_message, update_token_usage


class AgentLoop:
    """Agent主循环 - 协调LLM调用和工具执行

    核心流程：
    1. 拼接消息（系统提示 + 历史 + 用户输入）
    2. 调用LLM
    3. 如果有工具调用 → 执行工具 → 递归继续
    4. 如果无工具调用 → 返回结果
    """

    def __init__(self, llm: LLMProvider, tool_registry: ToolRegistry):
        self.llm = llm
        self.tool_registry = tool_registry

    def run(self, session: Session, user_input: str) -> str:
        """执行Agent主循环（非流式）"""
        add_user_message(session, user_input)

        while True:
            messages = session.get_messages_for_api()
            tools = self.tool_registry.get_tool_definitions() or None
            response = self.llm.chat(messages, tools=tools)

            if response.tool_calls:
                add_assistant_message(session, response.content, response.tool_calls)
                for tool_call in response.tool_calls:
                    result = self.tool_registry.execute_sync(tool_call)
                    result_text = result.output if result.success else f"Error: {result.error}"
                    add_tool_result(session, tool_call.id, result_text)
                continue
            else:
                add_assistant_message(session, response.content)
                return response.content or ""

    def run_stream(self, session: Session, user_input: str) -> Iterator[str]:
        """执行Agent主循环（流式输出）"""
        add_user_message(session, user_input)

        while True:
            messages = session.get_messages_for_api()
            tools = self.tool_registry.get_tool_definitions() or None

            # 流式调用LLM
            full_content = ""
            tool_calls_data = []

            for chunk in self.llm.chat_stream_with_tools(messages, tools=tools):
                if chunk["type"] == "content":
                    full_content += chunk["content"]
                    yield chunk["content"]
                elif chunk["type"] == "tool_call":
                    tool_calls_data.append(chunk["data"])
                elif chunk["type"] == "usage":
                    update_token_usage(session, chunk["data"])
                    yield {"type": "usage", "data": chunk["data"]}

            # 处理工具调用
            if tool_calls_data:
                # 通知前端停止加载动画
                yield {"type": "tool_start"}

                from ..types import ToolCall
                tool_calls = [
                    ToolCall(id=tc["id"], name=tc["name"], arguments=tc["arguments"])
                    for tc in tool_calls_data
                ]
                add_assistant_message(session, full_content or None, tool_calls)

                for tool_call in tool_calls:
                    result = self.tool_registry.execute_sync(tool_call)
                    result_text = result.output if result.success else f"Error: {result.error}"
                    add_tool_result(session, tool_call.id, result_text)

                # 继续循环处理工具结果
                continue
            else:
                add_assistant_message(session, full_content)
                return
