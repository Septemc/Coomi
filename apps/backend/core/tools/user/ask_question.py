"""AskUserQuestion 工具 - 向用户提问"""
from __future__ import annotations

from typing import Any

from ..base import BaseTool, ToolAccess, ToolConcurrency, ToolResult


class AskUserQuestionTool(BaseTool):
    """向用户提问"""

    name = "AskUserQuestion"
    description = "Use this tool when you need to ask the user questions during execution."
    access = ToolAccess.READ_ONLY
    concurrency = ToolConcurrency.BLOCKING
    requires_confirmation = False

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
                                        "label": {"type": "string"},
                                        "description": {"type": "string"},
                                    },
                                    "required": ["label", "description"],
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

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        questions = arguments["questions"]

        try:
            # 简化实现：格式化问题
            output_lines = []
            for q in questions:
                output_lines.append(f"## {q['header']}")
                output_lines.append(q["question"])
                output_lines.append("")
                for i, opt in enumerate(q["options"], 1):
                    output_lines.append(f"{i}. **{opt['label']}**: {opt['description']}")
                output_lines.append("")

            output = "\n".join(output_lines)
            return ToolResult(success=True, output=output)
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))
