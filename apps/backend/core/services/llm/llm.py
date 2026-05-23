"""LLM服务层 - 大模型API调用"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from openai import OpenAI
from dotenv import load_dotenv

from ...types import LLMResponse, ToolCall

# 加载项目根目录的 .env 文件
env_path = Path(__file__).resolve().parents[5] / ".env"
load_dotenv(env_path, override=True)


class LLMService:
    """LLM服务 - 支持工具调用"""

    def __init__(self):
        self.client = OpenAI(
            api_key=os.getenv("DEEPSEEK_API_KEY"),
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        )
        self.model = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        """同步调用LLM"""
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "reasoning_effort": "high",
            "extra_body": {"thinking": {"type": "enabled"}},
        }

        if tools:
            kwargs["tools"] = tools

        response = self.client.chat.completions.create(**kwargs)

        # 解析响应
        choice = response.choices[0]
        content = choice.message.content
        tool_calls = None

        if choice.message.tool_calls:
            tool_calls = [
                ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=tc.function.arguments,
                )
                for tc in choice.message.tool_calls
            ]

        usage = None
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }

        return LLMResponse(content=content, tool_calls=tool_calls, usage=usage)

    def chat_stream(self, messages: list[dict[str, Any]]):
        """流式调用LLM（不支持工具调用）"""
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            stream=True,
            reasoning_effort="high",
            extra_body={"thinking": {"type": "enabled"}},
        )
        for chunk in response:
            if chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    def chat_stream_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ):
        """流式调用LLM（支持工具调用）"""
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "reasoning_effort": "high",
            "extra_body": {"thinking": {"type": "enabled"}},
        }

        if tools:
            kwargs["tools"] = tools

        response = self.client.chat.completions.create(**kwargs)

        # 累积工具调用
        tool_calls_accum = {}

        for chunk in response:
            delta = chunk.choices[0].delta

            # 内容
            if delta.content:
                yield {"type": "content", "content": delta.content}

            # 工具调用
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_calls_accum:
                        tool_calls_accum[idx] = {
                            "id": tc.id or "",
                            "name": tc.function.name if tc.function and tc.function.name else "",
                            "arguments": tc.function.arguments if tc.function and tc.function.arguments else "",
                        }
                    else:
                        if tc.id:
                            tool_calls_accum[idx]["id"] = tc.id
                        if tc.function and tc.function.name:
                            tool_calls_accum[idx]["name"] = tc.function.name
                        if tc.function and tc.function.arguments:
                            tool_calls_accum[idx]["arguments"] += tc.function.arguments

        # 输出累积的工具调用
        for idx in sorted(tool_calls_accum.keys()):
            tc = tool_calls_accum[idx]
            # 解析 arguments JSON
            import json
            try:
                tc["arguments"] = json.loads(tc["arguments"])
            except json.JSONDecodeError:
                tc["arguments"] = {}
            yield {"type": "tool_call", "data": tc}
