"""Anthropic Provider 实现"""
from __future__ import annotations

import json
import os
from typing import Any, Iterator

import anthropic

from ...types import LLMResponse, ToolCall
from .provider import LLMProvider


class AnthropicProvider(LLMProvider):
    """Anthropic LLM Provider (Claude)"""

    def __init__(self):
        self.client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        self.model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")

    def get_tool_call_mode(self):
        from .provider import ToolCallMode
        return ToolCallMode.NATIVE

    def switch_model(self, model_name: str) -> str:
        """运行时切换模型"""
        self.model = model_name
        return self.model

    def get_model_display_name(self) -> str:
        """获取人类可读的模型显示名称"""
        return "Claude"

    def _convert_messages(
        self, messages: list[dict[str, Any]]
    ) -> tuple[str, list[dict[str, Any]]]:
        """转换消息格式（OpenAI -> Anthropic）

        Anthropic 的消息格式与 OpenAI 不同：
        - system 是单独的参数，不在 messages 中
        - tool 结果需要用 tool_result 类型
        """
        system = ""
        converted = []

        for msg in messages:
            if msg["role"] == "system":
                system = msg.get("content", "")
            elif msg["role"] == "tool":
                # 工具结果
                converted.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": msg.get("tool_call_id", ""),
                            "content": msg.get("content", ""),
                        }
                    ],
                })
            elif msg["role"] == "assistant" and msg.get("tool_calls"):
                # 助手消息带工具调用
                content = []
                if msg.get("content"):
                    content.append({"type": "text", "text": msg["content"]})
                for tc in msg["tool_calls"]:
                    content.append({
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["name"],
                        "input": tc["arguments"],
                    })
                converted.append({"role": "assistant", "content": content})
            else:
                converted.append(msg)

        return system, converted

    def _convert_tools(
        self, tools: list[dict[str, Any]] | None
    ) -> list[dict[str, Any]] | None:
        """转换工具定义格式（OpenAI -> Anthropic）"""
        if not tools:
            return None

        converted = []
        for tool in tools:
            func = tool.get("function", {})
            converted.append({
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", {}),
            })
        return converted

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs,
    ) -> LLMResponse:
        """同步调用"""
        system, converted_messages = self._convert_messages(messages)
        converted_tools = self._convert_tools(tools)

        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 4096,
            "messages": converted_messages,
        }

        if system:
            params["system"] = system

        if converted_tools:
            params["tools"] = converted_tools

        response = self.client.messages.create(**params)

        # 解析响应
        content = ""
        tool_calls = None

        for block in response.content:
            if block.type == "text":
                content += block.text
            elif block.type == "tool_use":
                if tool_calls is None:
                    tool_calls = []
                tool_calls.append(ToolCall(
                    id=block.id,
                    name=block.name,
                    arguments=block.input,
                ))

        usage = None
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.input_tokens,
                "completion_tokens": response.usage.output_tokens,
                "total_tokens": response.usage.input_tokens + response.usage.output_tokens,
            }

        return LLMResponse(
            content=content or None,
            tool_calls=tool_calls,
            usage=usage,
        )

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        **kwargs,
    ) -> Iterator[str]:
        """流式纯文本输出"""
        system, converted_messages = self._convert_messages(messages)

        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 4096,
            "messages": converted_messages,
        }

        if system:
            params["system"] = system

        with self.client.messages.stream(**params) as stream:
            for text in stream.text_stream:
                yield text

    def chat_stream_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs,
    ) -> Iterator[dict[str, Any]]:
        """流式输出 + 工具调用

        使用 Anthropic SDK streaming API，正确累积 tool_use JSON 片段，
        通过 get_final_message() 获取 usage，不发起重复 API 调用。
        """
        system, converted_messages = self._convert_messages(messages)
        converted_tools = self._convert_tools(tools)

        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 4096,
            "messages": converted_messages,
        }

        if system:
            params["system"] = system

        if converted_tools:
            params["tools"] = converted_tools

        # 累积流式 tool_use JSON 片段，keyed by content block index
        tool_input_accum: dict[int, dict[str, Any]] = {}

        with self.client.messages.stream(**params) as stream:
            for event in stream:
                if event.type == "content_block_start":
                    if event.content_block.type == "tool_use":
                        tool_input_accum[event.index] = {
                            "id": event.content_block.id,
                            "name": event.content_block.name,
                            "json_fragments": [],
                        }
                elif event.type == "content_block_delta":
                    if event.delta.type == "text_delta":
                        yield {"type": "content", "content": event.delta.text}
                    elif event.delta.type == "input_json_delta":
                        idx = event.index
                        if idx in tool_input_accum:
                            tool_input_accum[idx]["json_fragments"].append(
                                event.delta.partial_json
                            )

            # 流结束后获取最终消息，包含 usage 信息
            final_msg = stream.get_final_message()

        # 输出 usage
        if final_msg.usage:
            yield {
                "type": "usage",
                "data": {
                    "prompt_tokens": final_msg.usage.input_tokens,
                    "completion_tokens": final_msg.usage.output_tokens,
                    "total_tokens": final_msg.usage.input_tokens + final_msg.usage.output_tokens,
                },
            }

        # 输出累积的工具调用
        for idx in sorted(tool_input_accum.keys()):
            acc = tool_input_accum[idx]
            try:
                arguments = json.loads("".join(acc["json_fragments"]))
            except (json.JSONDecodeError, KeyError):
                arguments = {}
            yield {
                "type": "tool_call",
                "data": {
                    "id": acc["id"],
                    "name": acc["name"],
                    "arguments": arguments,
                },
            }
