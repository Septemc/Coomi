"""AskUserQuestion 工具 - 交互式提问（阻塞 Agent 循环，等待用户输入）

交互式工具：
- is_interactive = True → AgentLoop 走 run_async 路径
- run_async 调用 app_context._handle_ask_questions() 阻塞等待用户
- run() 作为同步 fallback（不应被调用）
"""
from __future__ import annotations

import json
from typing import Any

from ..base import BaseTool, ToolAccess, ToolConcurrency, ToolResult


class AskUserQuestionTool(BaseTool):
    """交互式提问工具 — 阻塞 Agent 循环，等待用户回答"""

    name = "AskUserQuestion"
    description = (
        "Use this tool when you need to ask the user questions during execution. "
        "This allows you to:\n"
        "- Gather user preferences before making design decisions\n"
        "- Clarify ambiguous requirements in plan mode\n"
        "- Let the user choose between multiple valid approaches\n\n"
        "The tool presents a multi-question panel with arrow key navigation. "
        "Each question has 2-4 options plus an 'Other' option for free text. "
        "The tool blocks your execution until the user answers.\n\n"
        "Usage guidelines:\n"
        "- Provide 1-4 questions, each with a short header (<=4 chars for the nav bar)\n"
        "- Provide 2-4 options per question\n"
        "- Each option must include a short label, a concise summary, and a detailed paragraph\n"
        "- Keep option labels brief; put the immediate impact in summary and tradeoffs in description\n"
        "- Set a recommendation on questions where you have a strong preference\n"
        "- Do NOT use for trivial decisions you can make yourself"
    )
    access = ToolAccess.READ_ONLY
    concurrency = ToolConcurrency.BLOCKING
    requires_confirmation = False

    @property
    def is_interactive(self) -> bool:
        """交互式工具，走 async 路径而非线程池"""
        return True

    def get_parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string"},
                            "header": {"type": "string"},
                            "options": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {
                                            "type": "string",
                                            "description": "Short option name, usually 2-6 words.",
                                        },
                                        "summary": {
                                            "type": "string",
                                            "description": (
                                                "Concise opening description shown beside the label. "
                                                "State the option's main effect in one short phrase."
                                            ),
                                        },
                                        "description": {
                                            "type": "string",
                                            "description": (
                                                "Detailed paragraph explaining what this option means, "
                                                "including implications, tradeoffs, and when to choose it."
                                            ),
                                        },
                                        "preview": {
                                            "type": "string",
                                            "description": "Deprecated alias for summary; prefer summary.",
                                        },
                                    },
                                    "required": ["label", "summary", "description"],
                                },
                                "minItems": 2,
                                "maxItems": 4,
                            },
                            "multiSelect": {"type": "boolean"},
                        },
                        "required": ["question", "header", "options"],
                    },
                    "minItems": 1,
                    "maxItems": 4,
                },
            },
            "required": ["questions"],
        }

    async def run_async(self, arguments: dict[str, Any], app_context: Any = None) -> ToolResult:
        """交互式执行入口 — 调用 CoomiApp._handle_ask_questions() 阻塞等待用户输入

        Args:
            arguments: {"questions": [...]}
            app_context: CoomiApp 实例（由 AgentLoop 注入）

        Returns:
            ToolResult(success=True, output=JSON answers)
            ToolResult(success=False, error="User cancelled")
        """
        if app_context is None:
            return ToolResult(success=False, output="", error="No app context — interactive tool cannot run")

        questions = arguments["questions"]
        try:
            answers = await app_context._handle_ask_questions(questions)
            if "__cancelled__" in answers:
                return ToolResult(success=False, output="", error="User cancelled")
            return ToolResult(success=True, output=json.dumps(answers, ensure_ascii=False))
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Question handling failed: {e}")

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        """同步 fallback — 不应被调用，仅作保底"""
        return ToolResult(success=False, output="", error="Use run_async — this tool requires async execution")
