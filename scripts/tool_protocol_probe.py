"""Manual active-provider tool protocol probe.

This script reads the active provider from ~/.coomi/config/providers.json, asks it
to call core tools, and validates the calls against a no-op registry. It never
prints API keys and never executes real file, shell, network, MCP, or Skill side
effects.

Run manually:
    python scripts/tool_protocol_probe.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from coomi.engine.tool_executor import ToolExecutor
from coomi.security import PermissionMode, PermissionSystem
from coomi.services.llm.config import ConfigManager
from coomi.services.llm.factory import get_llm_provider
from coomi.services.llm.text_tool_calls import TextToolCallFilter
from coomi.tools.base import BaseTool, ToolAccess, ToolConcurrency, ToolResult
from coomi.tools.registry import ToolRegistry, create_default_registry
from coomi.types import Session, ToolCall


TARGETS = {
    "Read": {"file_path": "F:\\_WorkSpace\\Projects\\Coomi\\README.md"},
    "Glob": {"pattern": "**/*.py", "path": "F:\\_WorkSpace\\Projects\\Coomi"},
    "Grep": {"pattern": "class ToolRegistry", "path": "F:\\_WorkSpace\\Projects\\Coomi"},
    "Bash": {"command": "echo coomi-probe"},
    "PowerShell": {"command": "Write-Output coomi-probe"},
    "WebSearch": {"query": "Coomi Agent tool protocol"},
    "WebFetch": {"url": "https://example.com"},
    "TodoWrite": {
        "todos": [
            {
                "content": "Probe tool protocol",
                "status": "pending",
                "activeForm": "Probing tool protocol",
            }
        ]
    },
    "AskUserQuestion": {
        "questions": [
            {
                "header": "Probe",
                "question": "Choose a no-op probe option.",
                "options": [
                    {
                        "label": "Continue",
                        "summary": "Safe no-op",
                        "description": "Records that the provider can call AskUserQuestion.",
                    }
                ],
            }
        ]
    },
    "Task": {
        "description": "Inspect tool protocol aliases",
        "prompt": "Inspect tool protocol aliases without changing files.",
    },
    "Agent": {
        "description": "Inspect tool protocol aliases",
        "prompt": "Inspect tool protocol aliases without changing files.",
    },
}


class NoopTool(BaseTool):
    def __init__(self, wrapped: BaseTool):
        self.name = wrapped.name
        self.description = wrapped.description
        self.access = wrapped.access
        self.concurrency = wrapped.concurrency
        self.requires_confirmation = wrapped.requires_confirmation
        self._schema = wrapped.get_parameters_schema()

    def get_parameters_schema(self) -> dict[str, Any]:
        return self._schema

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(
            success=True,
            output=f"NOOP {self.name}: validated but not executed",
        )


def build_noop_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in create_default_registry().list_tools():
        registry.register(NoopTool(tool))
    return registry


def redact_provider(config: Any) -> dict[str, Any]:
    data = asdict(config) if hasattr(config, "__dataclass_fields__") else dict(config.__dict__)
    for key in list(data):
        lowered = key.casefold()
        if "key" in lowered or "token" in lowered or "secret" in lowered:
            data[key] = "[REDACTED]" if data.get(key) else ""
    return data


def build_prompt(name: str, arguments: dict[str, Any]) -> str:
    return (
        "Call exactly one tool for this diagnostic. Do not explain. "
        f"Tool name: {name}. Arguments JSON: {json.dumps(arguments, ensure_ascii=False)}"
    )


async def collect_tool_calls(provider: Any, registry: ToolRegistry, prompt: str) -> list[ToolCall]:
    messages = [
        {"role": "system", "content": "You are Coomi Agent. Follow the diagnostic prompt exactly."},
        {"role": "user", "content": prompt},
    ]
    text_mode = provider.get_text_tool_mode()
    text_filter = TextToolCallFilter(mode=text_mode)
    calls: list[ToolCall] = []
    tools = registry.get_tool_definitions()

    async for chunk in provider.chat_stream_with_tools(messages, tools=tools):
        if not isinstance(chunk, dict):
            continue
        chunk_type = chunk.get("type")
        if chunk_type == "tool_call":
            data = chunk.get("data") or {}
            calls.append(
                ToolCall(
                    id=str(data.get("id") or f"probe_{uuid.uuid4().hex[:12]}"),
                    name=str(data.get("name") or ""),
                    arguments=data.get("arguments") if isinstance(data.get("arguments"), dict) else {},
                    raw_arguments=data.get("raw_arguments"),
                    parse_error=data.get("parse_error"),
                    source=str(data.get("source") or "native"),
                )
            )
            continue
        if chunk_type in {"content", "reasoning_content"}:
            _, parsed = text_filter.feed(str(chunk.get("content") or ""))
            calls.extend(_tool_calls_from_text(parsed))

    _, parsed_tail = text_filter.flush()
    calls.extend(_tool_calls_from_text(parsed_tail))
    return calls


def _tool_calls_from_text(parsed: list[dict[str, Any]]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for item in parsed:
        calls.append(
            ToolCall(
                id=str(item.get("id") or f"probe_{uuid.uuid4().hex[:12]}"),
                name=str(item.get("name") or ""),
                arguments=item.get("arguments") if isinstance(item.get("arguments"), dict) else {},
                raw_arguments=item.get("raw_arguments"),
                parse_error=item.get("parse_error"),
                source=str(item.get("source") or "text_fallback"),
            )
        )
    return calls


async def validate_call(
    executor: ToolExecutor,
    registry: ToolRegistry,
    session: Session,
    requested_name: str,
    call: ToolCall | None,
) -> dict[str, Any]:
    if call is None:
        return {
            "requested": requested_name,
            "detected": False,
            "error": "No tool call detected",
        }

    canonical = registry.canonical_name(call.name)
    outcome = await executor.execute(session, call)
    return {
        "requested": requested_name,
        "detected": True,
        "name": call.name,
        "canonical": canonical,
        "arguments": call.arguments,
        "source": call.source,
        "parse_error": call.parse_error,
        "is_error": outcome.is_error,
        "result": outcome.result_text[:500],
    }


async def main() -> int:
    config_manager = ConfigManager()
    config = config_manager.get_active()
    if config is None:
        print("No active provider configured.")
        return 1

    print("Active provider:")
    print(json.dumps(redact_provider(config), ensure_ascii=False, indent=2))

    provider = get_llm_provider()
    registry = build_noop_registry()
    permissions = PermissionSystem()
    permissions.set_mode(PermissionMode.FULL_ACCESS)
    executor = ToolExecutor(registry, permission_system=permissions, project_path=str(ROOT))
    session = Session(id="tool-protocol-probe", system_prompt="You are Coomi Agent.")

    results: list[dict[str, Any]] = []
    for name, arguments in TARGETS.items():
        prompt = build_prompt(name, arguments)
        try:
            calls = await collect_tool_calls(provider, registry, prompt)
            result = await validate_call(
                executor,
                registry,
                session,
                requested_name=name,
                call=calls[0] if calls else None,
            )
        except Exception as exc:
            result = {
                "requested": name,
                "detected": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        results.append(result)
        print(json.dumps(result, ensure_ascii=False))

    failures = [item for item in results if not item.get("detected")]
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
