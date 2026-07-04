"""Tool execution gateway with validation, permissions, hooks, and result sizing."""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..security import HookSystem, PermissionLevel, PermissionSystem
from ..services.llm.text_tool_calls import MALFORMED_TEXT_TOOL_CALL_NAME
from ..tools.base import BaseTool, ToolAccess, ToolResult
from ..tools.registry import ToolRegistry
from ..types import Session, ToolCall


LARGE_RESULT_THRESHOLD = 50 * 1024
PREVIEW_CHARS = 4 * 1024
PLAN_MODE_ALLOWED_SHELL_COMMANDS = {
    "cat",
    "dir",
    "find",
    "findstr",
    "grep",
    "get-childitem",
    "get-content",
    "get-location",
    "git",
    "head",
    "ls",
    "pwd",
    "rg",
    "select-object",
    "select-string",
    "tail",
    "test-path",
    "type",
    "where-object",
}
PLAN_MODE_ALLOWED_GIT_SUBCOMMANDS = {
    "diff",
    "log",
    "ls-files",
    "show",
    "status",
}
PLAN_MODE_BLOCKED_SHELL_TOKENS = (
    ">",
    ">>",
    "2>",
    "2>>",
    "1>",
    "1>>",
    "*>",
    "Out-File",
    "Set-Content",
    "Add-Content",
    "Remove-Item",
    "Move-Item",
    "Copy-Item",
    "New-Item",
    "Rename-Item",
    "Invoke-Expression",
    "Start-Process",
    "git commit",
    "git checkout",
    "git reset",
    "git restore",
    "git clean",
    "git switch",
    "git add",
    "git push",
    "git pull",
    "git merge",
    "git rebase",
    "-delete",
    " tee ",
    " rm ",
    " del ",
    " erase ",
)


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
        canonical_name = self.tool_registry.canonical_name(tool_call.name)
        if canonical_name and canonical_name != tool_call.name:
            tool_call = replace(tool_call, name=canonical_name)

        if tool_call.parse_error:
            if tool_call.source == "text_fallback" and tool_call.name == MALFORMED_TEXT_TOOL_CALL_NAME:
                error = tool_call.parse_error
            else:
                error = (
                    f"Invalid JSON arguments for tool '{tool_call.name}': {tool_call.parse_error}. "
                    "The tool was not executed. Retry with valid JSON arguments."
                )
            return self._outcome(
                session,
                tool_call,
                result=ToolResult(
                    success=False,
                    output="",
                    error=error,
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

        validation_error: str | None
        try:
            schema = tool.get_parameters_schema()
        except Exception as exc:
            validation_error = f"Could not load schema: {exc}"
        else:
            if _should_coerce_arguments(tool_call):
                coerced_arguments = _coerce_arguments_for_schema(tool_call.arguments, schema)
                if coerced_arguments != tool_call.arguments:
                    tool_call = replace(tool_call, arguments=coerced_arguments)
            validation_error = self._validate_arguments(
                tool,
                tool_call.arguments,
                schema=schema,
            )
        if validation_error:
            return self._outcome(
                session,
                tool_call,
                ToolResult(False, "", f"InputValidationError: {validation_error}"),
                start,
                persist=False,
            )

        if self.read_only_mode and not self._is_allowed_in_read_only_mode(tool, tool_call.arguments):
            return self._outcome(
                session,
                tool_call,
                ToolResult(
                    False,
                    "",
                    (
                        f"Plan Mode is active: tool '{tool.name}' is not allowed because it "
                        "can modify state. Use read-only tools or AskUserQuestion. The user "
                        "must leave Plan Mode before any implementation or write operation."
                    ),
                ),
                start,
                persist=False,
            )

        allowed, denial_message = await self._check_permission(
            tool,
            tool_call.arguments,
            source=tool_call.source,
        )
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
        source: str = "native",
    ) -> tuple[bool, str | None]:
        level = self.permission_system.check_permission(tool.name, arguments)
        if source == "text_fallback" and tool.access in {ToolAccess.WRITE, ToolAccess.DESTRUCTIVE}:
            level = PermissionLevel.ASK
        if level == PermissionLevel.AUTO:
            return True, None
        if level == PermissionLevel.DENY:
            return False, f"Permission denied for tool '{tool.name}'"

        if not self.app_context or not hasattr(self.app_context, "_handle_ask_questions"):
            return False, (
                f"Permission required for tool '{tool.name}', but no interactive "
                "app context is available to request approval."
            )

        description = _summarize_arguments(arguments, tool_name=tool.name)
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

    def _validate_arguments(
        self,
        tool: BaseTool,
        arguments: dict[str, Any],
        schema: dict[str, Any] | None = None,
    ) -> str | None:
        if schema is None:
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

    def _is_allowed_in_read_only_mode(self, tool: BaseTool, arguments: dict[str, Any]) -> bool:
        if tool.access == ToolAccess.READ_ONLY:
            return True
        if tool.name in {"Bash", "PowerShell"}:
            return _is_read_only_shell_command(str(arguments.get("command", "")))
        return False

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


def _should_coerce_arguments(tool_call: ToolCall) -> bool:
    return tool_call.source == "text_fallback" or tool_call.id.startswith("text_call_")


def _coerce_arguments_for_schema(
    arguments: dict[str, Any],
    schema: dict[str, Any],
) -> dict[str, Any]:
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return arguments

    coerced: dict[str, Any] = {}
    changed = False
    for key, value in arguments.items():
        prop = properties.get(key)
        next_value = _coerce_value_for_schema(value, prop if isinstance(prop, dict) else None)
        coerced[key] = next_value
        if next_value != value or type(next_value) is not type(value):
            changed = True
    return coerced if changed else arguments


def _coerce_value_for_schema(value: Any, schema: dict[str, Any] | None) -> Any:
    if not schema:
        return value

    expected_types = _schema_types(schema)
    if "string" in expected_types:
        return value

    if isinstance(value, dict):
        if "object" in expected_types:
            return _coerce_object_for_schema(value, schema)
        return value

    if isinstance(value, list):
        if "array" in expected_types:
            return _coerce_array_for_schema(value, schema)
        return value

    if not isinstance(value, str):
        return value

    if "integer" in expected_types:
        stripped = value.strip()
        if re.fullmatch(r"[+-]?\d+", stripped):
            try:
                return int(stripped)
            except ValueError:
                return value
        return value

    if "number" in expected_types:
        stripped = value.strip()
        try:
            number = float(stripped)
        except ValueError:
            return value
        return number if math.isfinite(number) else value

    if "boolean" in expected_types:
        lowered = value.strip().casefold()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        return value

    if "object" in expected_types:
        parsed = _parse_json_string(value)
        if isinstance(parsed, dict):
            return _coerce_object_for_schema(parsed, schema)
        return value

    if "array" in expected_types:
        parsed = _parse_json_string(value)
        if isinstance(parsed, list):
            return _coerce_array_for_schema(parsed, schema)
        return value

    return value


def _coerce_object_for_schema(value: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return value
    coerced: dict[str, Any] = {}
    changed = False
    for key, item in value.items():
        prop = properties.get(key)
        next_item = _coerce_value_for_schema(item, prop if isinstance(prop, dict) else None)
        coerced[key] = next_item
        if next_item != item or type(next_item) is not type(item):
            changed = True
    return coerced if changed else value


def _coerce_array_for_schema(value: list[Any], schema: dict[str, Any]) -> list[Any]:
    items_schema = schema.get("items")
    if not isinstance(items_schema, dict):
        return value
    coerced = [_coerce_value_for_schema(item, items_schema) for item in value]
    if any(next_item != item or type(next_item) is not type(item) for item, next_item in zip(value, coerced)):
        return coerced
    return value


def _schema_types(schema: dict[str, Any]) -> set[str]:
    raw_type = schema.get("type")
    if isinstance(raw_type, str):
        return {raw_type}
    if isinstance(raw_type, list):
        return {item for item in raw_type if isinstance(item, str)}
    return set()


def _parse_json_string(value: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None


def _is_read_only_shell_command(command: str) -> bool:
    normalized = " ".join((command or "").strip().split())
    if not normalized:
        return False

    lowered = normalized.casefold()
    if any(token.casefold() in lowered for token in PLAN_MODE_BLOCKED_SHELL_TOKENS):
        return False
    if re.search(r"(^|[^&])(&&|\|\||;|`|\$\()", normalized):
        return False

    segments = [segment.strip() for segment in normalized.split("|")]
    if not segments:
        return False

    for segment in segments:
        match = re.match(r"^(?:&\s*)?([A-Za-z][\w.-]*|dir|ls|pwd|type)\b", segment)
        if not match:
            return False
        command_name = match.group(1).casefold()
        if command_name not in PLAN_MODE_ALLOWED_SHELL_COMMANDS:
            return False
        if command_name == "git" and not _is_read_only_git_command(segment):
            return False

    return True


def _is_read_only_git_command(command: str) -> bool:
    parts = re.findall(r'"[^"]*"|\'[^\']*\'|\S+', command)
    args = [part.strip("\"'") for part in parts[1:]]
    idx = 0
    while idx < len(args):
        raw_arg = args[idx]
        arg = raw_arg.casefold()
        if raw_arg == "-C" and idx + 1 < len(args):
            idx += 2
            continue
        if arg.startswith("-"):
            idx += 1
            continue
        return arg in PLAN_MODE_ALLOWED_GIT_SUBCOMMANDS
    return False


def _summarize_arguments(
    arguments: dict[str, Any],
    limit: int = 800,
    tool_name: str = "",
) -> str:
    lines = _argument_summary_lines(arguments, tool_name)
    text = "\n".join(lines) if lines else "(no arguments)"
    if len(text) > limit:
        return text[:limit] + "... [truncated]"
    return text


def _argument_summary_lines(arguments: dict[str, Any], tool_name: str) -> list[str]:
    if tool_name == "AskUserQuestion":
        questions = arguments.get("questions")
        if isinstance(questions, list):
            question_lines = [f"Questions: {len(questions)}"]
            for index, question in enumerate(questions[:4], start=1):
                if not isinstance(question, dict):
                    continue
                text = _compact_text(str(question.get("question") or ""), 140)
                options = question.get("options")
                option_count = len(options) if isinstance(options, list) else 0
                multi = " multi-select" if question.get("multiSelect") else ""
                question_lines.append(f"Q{index}: {text} ({option_count} options{multi})")
            return question_lines

    preferred_keys = (
        "file_path",
        "path",
        "pattern",
        "query",
        "url",
        "command",
        "description",
        "prompt",
    )
    lines: list[str] = []
    for key in preferred_keys:
        if key in arguments:
            lines.append(f"{key}: {_compact_text(str(arguments[key]), 220)}")

    if lines:
        return lines

    for key, value in list(arguments.items())[:8]:
        if isinstance(value, (str, int, float, bool)) or value is None:
            rendered = str(value)
        elif isinstance(value, list):
            rendered = f"{len(value)} item(s)"
        elif isinstance(value, dict):
            rendered = f"{len(value)} field(s)"
        else:
            rendered = type(value).__name__
        lines.append(f"{key}: {_compact_text(rendered, 220)}")
    return lines


def _compact_text(value: str, limit: int) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


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
