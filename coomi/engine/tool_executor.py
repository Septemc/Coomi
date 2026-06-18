"""Tool execution gateway with validation, permissions, hooks, and result sizing."""
from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..security import HookSystem, PermissionLevel, PermissionSystem
from ..tools.base import BaseTool, ToolAccess, ToolResult
from ..tools.registry import ToolRegistry
from ..types import Session, ToolCall


LARGE_RESULT_THRESHOLD = 50 * 1024
PREVIEW_CHARS = 4 * 1024


@dataclass
class ToolExecutionOutcome:
    """Normalized result of a tool invocation."""

    tool_call: ToolCall
    result_text: str
    is_error: bool
    elapsed: float
    cache_hit: bool = False


class ToolExecutor:
    """Single execution boundary for all tools."""

    def __init__(
        self,
        tool_registry: ToolRegistry,
        permission_system: PermissionSystem | None = None,
        hook_system: HookSystem | None = None,
        app_context: Any = None,
        project_path: str | None = None,
        large_result_threshold: int = LARGE_RESULT_THRESHOLD,
        read_only_mode: bool = False,
    ):
        self.tool_registry = tool_registry
        self.permission_system = permission_system or PermissionSystem()
        self.hook_system = hook_system or HookSystem()
        self.app_context = app_context
        self.project_path = Path(project_path or os.getcwd())
        self.large_result_threshold = large_result_threshold
        self.read_only_mode = read_only_mode

    async def execute(self, session: Session, tool_call: ToolCall) -> ToolExecutionOutcome:
        start = time.perf_counter()

        if tool_call.parse_error:
            return self._outcome(
                session,
                tool_call,
                result=ToolResult(
                    success=False,
                    output="",
                    error=(
                        f"Invalid JSON arguments for tool '{tool_call.name}': {tool_call.parse_error}. "
                        "The tool was not executed. Retry with valid JSON arguments."
                    ),
                ),
                start=start,
                persist=False,
            )

        tool = self.tool_registry.get(tool_call.name)
        if tool is None:
            return self._outcome(
                session,
                tool_call,
                ToolResult(False, "", f"Tool '{tool_call.name}' not found"),
                start,
                persist=False,
            )

        validation_error = self._validate_arguments(tool, tool_call.arguments)
        if validation_error:
            return self._outcome(
                session,
                tool_call,
                ToolResult(False, "", f"InputValidationError: {validation_error}"),
                start,
                persist=False,
            )

        if self.read_only_mode and tool.access != ToolAccess.READ_ONLY and tool.name != "ExitPlanMode":
            return self._outcome(
                session,
                tool_call,
                ToolResult(
                    False,
                    "",
                    (
                        f"Plan Mode is active: tool '{tool.name}' is not allowed because it "
                        "can modify state. Use read-only tools, AskUserQuestion, or ExitPlanMode."
                    ),
                ),
                start,
                persist=False,
            )

        allowed, denial_message = await self._check_permission(tool, tool_call.arguments)
        if not allowed:
            return self._outcome(
                session,
                tool_call,
                ToolResult(False, "", denial_message or "Permission denied"),
                start,
                persist=False,
            )

        try:
            pre_ctx = await self.hook_system.run_pre_hooks(tool.name, tool_call.arguments)
            if pre_ctx.skip:
                hook_result = self._result_from_hook(pre_ctx.result, pre_ctx.error)
                return self._outcome(session, tool_call, hook_result, start)
            arguments = pre_ctx.arguments
        except Exception as exc:
            return self._outcome(
                session,
                tool_call,
                ToolResult(False, "", f"Pre-hook failed: {type(exc).__name__}: {exc}"),
                start,
                persist=False,
            )

        try:
            if tool.is_interactive:
                result = await tool.run_async(arguments, self.app_context)
            else:
                result = await asyncio.to_thread(tool.run, arguments)
        except Exception as exc:
            result = ToolResult(False, "", f"Tool execution crashed: {type(exc).__name__}: {exc}")

        try:
            post_ctx = await self.hook_system.run_post_hooks(tool.name, arguments, result)
            if isinstance(post_ctx.result, ToolResult):
                result = post_ctx.result
        except Exception as exc:
            result = ToolResult(False, "", f"Post-hook failed: {type(exc).__name__}: {exc}")

        return self._outcome(session, tool_call, result, start)

    async def _check_permission(
        self,
        tool: BaseTool,
        arguments: dict[str, Any],
    ) -> tuple[bool, str | None]:
        level = self.permission_system.check_permission(tool.name, arguments)
        if level == PermissionLevel.AUTO:
            return True, None
        if level == PermissionLevel.DENY:
            return False, f"Permission denied for tool '{tool.name}'"

        if not self.app_context or not hasattr(self.app_context, "_handle_ask_questions"):
            return False, (
                f"Permission required for tool '{tool.name}', but no interactive "
                "app context is available to request approval."
            )

        description = _summarize_arguments(arguments)
        answers = await self.app_context._handle_ask_questions(
            [
                {
                    "header": "权限",
                    "question": f"Allow tool '{tool.name}' to run?\n{description}",
                    "options": [
                        {
                            "label": "Allow",
                            "value": "allow",
                            "description": "Run this tool call once.",
                            "is_recommended": True,
                        },
                        {
                            "label": "Deny",
                            "value": "deny",
                            "description": "Return a permission denied result to the model.",
                        },
                    ],
                }
            ]
        )
        if answers.get("__cancelled__"):
            return False, f"Permission request for tool '{tool.name}' was cancelled"

        answer = answers.get(0, {})
        if answer.get("option") == "allow":
            return True, None
        return False, f"Permission denied for tool '{tool.name}'"

    def _validate_arguments(self, tool: BaseTool, arguments: dict[str, Any]) -> str | None:
        try:
            schema = tool.get_parameters_schema()
        except Exception as exc:
            return f"Could not load schema: {exc}"

        required = schema.get("required", [])
        for key in required:
            if key not in arguments:
                return f"Missing required parameter '{key}'"

        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extras = sorted(set(arguments) - set(properties))
            if extras:
                return f"Unexpected parameter(s): {', '.join(extras)}"

        for key, value in arguments.items():
            prop = properties.get(key)
            if not prop:
                continue
            expected_type = prop.get("type")
            if expected_type and not _matches_json_type(value, expected_type):
                return f"Parameter '{key}' must be {expected_type}, got {type(value).__name__}"
            enum = prop.get("enum")
            if enum is not None and value not in enum:
                return f"Parameter '{key}' must be one of {enum!r}"

        return None

    def _outcome(
        self,
        session: Session,
        tool_call: ToolCall,
        result: ToolResult,
        start: float,
        persist: bool = True,
    ) -> ToolExecutionOutcome:
        result_text = self._result_to_text(result)
        if persist and len(result_text) > self.large_result_threshold:
            result_text = self._persist_large_result(session, tool_call, result_text)
        return ToolExecutionOutcome(
            tool_call=tool_call,
            result_text=result_text,
            is_error=not result.success,
            elapsed=time.perf_counter() - start,
        )

    def _result_to_text(self, result: ToolResult) -> str:
        output = result.output or ""
        if result.success:
            return output or "(Tool completed with no output)"
        if output and result.error:
            return f"{output}\n\nError: {result.error}"
        return f"Error: {result.error or 'Tool failed'}"

    def _persist_large_result(self, session: Session, tool_call: ToolCall, content: str) -> str:
        try:
            session_id = _safe_path_part(session.id)
            tool_call_id = _safe_path_part(tool_call.id)
            result_dir = self.project_path / ".coomi" / "sessions" / session_id / "tool_results"
            result_dir.mkdir(parents=True, exist_ok=True)
            filepath = result_dir / f"{tool_call_id}.txt"
            filepath.write_text(content, encoding="utf-8")
            preview = _preview(content)
            return (
                "[Large tool result stored]\n"
                f"Output too large ({len(content)} characters). Full output saved to: {filepath}\n\n"
                f"Preview:\n{preview}"
            )
        except Exception:
            return content

    def _result_from_hook(self, result: Any, error: str | None) -> ToolResult:
        if isinstance(result, ToolResult):
            return result
        if result is not None:
            return ToolResult(True, str(result))
        return ToolResult(False, "", error or "Tool execution skipped by hook")


def _matches_json_type(value: Any, expected_type: str | list[str]) -> bool:
    if isinstance(expected_type, list):
        return any(_matches_json_type(value, item) for item in expected_type)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "null":
        return value is None
    return True


def _summarize_arguments(arguments: dict[str, Any], limit: int = 800) -> str:
    text = repr(arguments)
    if len(text) > limit:
        return text[:limit] + "... [truncated]"
    return text


def _safe_path_part(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value)
    return safe or "unknown"


def _preview(content: str) -> str:
    if len(content) <= PREVIEW_CHARS:
        return content
    truncated = content[:PREVIEW_CHARS]
    newline = truncated.rfind("\n")
    if newline > PREVIEW_CHARS // 2:
        truncated = truncated[:newline]
    return truncated + "\n..."
