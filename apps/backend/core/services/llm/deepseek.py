"""DeepSeek Provider 实现"""
from __future__ import annotations

import json
import os
from typing import Any, Iterator

from openai import OpenAI

from ...types import LLMResponse, ToolCall
from .provider import LLMProvider, ToolCallMode

# 模型别名映射
MODEL_ALIASES: dict[str, str] = {
    "pro": "deepseek-v4-pro",
    "flash": "deepseek-v4-flash",
    "dsv4pro": "deepseek-v4-pro",
    "dsv4flash": "deepseek-v4-flash",
    "v4pro": "deepseek-v4-pro",
    "v4flash": "deepseek-v4-flash",
}


class DeepSeekProvider(LLMProvider):
    """DeepSeek LLM Provider"""

    def __init__(self):
        self.client = OpenAI(
            api_key=os.getenv("DEEPSEEK_API_KEY"),
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        )
        self.model = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
        # 判断是否为 flash 模型
        self.is_flash = "flash" in self.model.lower()

    def get_tool_call_mode(self) -> ToolCallMode:
        """返回支持的工具调用模式"""
        return ToolCallMode.NATIVE

    def switch_model(self, model_name: str) -> str:
        """运行时切换模型"""
        resolved = MODEL_ALIASES.get(model_name.lower().strip(), model_name)
        self.model = resolved
        self.is_flash = "flash" in self.model.lower()
        return self.model

    def get_model_display_name(self) -> str:
        """获取人类可读的模型显示名称"""
        if self.is_flash:
            return "DeepSeek V4 Flash"
        return "DeepSeek V4 Pro"

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs,
    ) -> LLMResponse:
        """同步调用"""
        tool_choice = kwargs.get("tool_choice", "auto")

        params: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }

        if tools:
            params["tools"] = tools
            params["tool_choice"] = tool_choice
            # 使用原生工具调用时禁用 thinking mode，避免 reasoning_content 传回问题
            params["extra_body"] = {"thinking": {"type": "disabled"}}
        else:
            # 没有工具时可以启用 thinking mode
            params["extra_body"] = {"thinking": {"type": "enabled"}}

        response = self.client.chat.completions.create(**params)

        # 解析响应
        choice = response.choices[0]
        content = choice.message.content
        tool_calls = None

        if choice.message.tool_calls:
            tool_calls = [
                ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=json.loads(tc.function.arguments),
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

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        **kwargs,
    ) -> Iterator[str]:
        """流式纯文本输出"""
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
        **kwargs,
    ) -> Iterator[dict[str, Any]]:
        """流式输出 + 工具调用"""
        tool_choice = kwargs.get("tool_choice", "auto")

        params: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        if tools:
            params["tools"] = tools
            params["tool_choice"] = tool_choice
            # 使用原生工具调用时禁用 thinking mode，避免 reasoning_content 传回问题
            params["extra_body"] = {"thinking": {"type": "disabled"}}
        else:
            # 没有工具时可以启用 thinking mode
            params["extra_body"] = {"thinking": {"type": "enabled"}}

        response = self.client.chat.completions.create(**params)

        # 累积工具调用
        tool_calls_accum: dict[int, dict[str, Any]] = {}
        usage_yielded = False

        for chunk in response:
            # 捕获 usage 数据（可能在任何 chunk 中）
            if chunk.usage:
                usage_yielded = True
                yield {
                    "type": "usage",
                    "data": {
                        "prompt_tokens": chunk.usage.prompt_tokens,
                        "completion_tokens": chunk.usage.completion_tokens,
                        "total_tokens": chunk.usage.total_tokens,
                    },
                }

            # 处理空 choices
            if not chunk.choices:
                continue

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

        # 如果 API 未返回 usage（stream_options 不被支持），
        # 用字符数估算作为降级方案，确保压缩逻辑能正常工作
        if not usage_yielded:
            from ..context.compressor import _estimate_tokens_from_dicts
            estimated = _estimate_tokens_from_dicts(messages)
            yield {
                "type": "usage",
                "data": {
                    "prompt_tokens": estimated,
                    "completion_tokens": 0,
                    "total_tokens": estimated,
                },
            }

        # 输出累积的工具调用
        for idx in sorted(tool_calls_accum.keys()):
            tc = tool_calls_accum[idx]
            try:
                tc["arguments"] = json.loads(tc["arguments"])
            except json.JSONDecodeError:
                tc["arguments"] = {}
            yield {"type": "tool_call", "data": tc}
