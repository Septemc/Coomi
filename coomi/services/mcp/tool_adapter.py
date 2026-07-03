"""Adapt MCP tools into Coomi tools."""
from __future__ import annotations

import json
from typing import Any

from ...tools.base import BaseTool, ToolAccess, ToolConcurrency, ToolResult
from .client import McpError, open_mcp_client
from .models import McpServerConfig, McpToolSpec


class McpToolAdapter(BaseTool):
    access = ToolAccess.WRITE
    concurrency = ToolConcurrency.BLOCKING
    requires_confirmation = True

    def __init__(self, server: McpServerConfig, spec: McpToolSpec):
        self.server = server
        self.spec = spec
        self.name = f"mcp__{server.name}__{spec.name}"
        self.description = f"[MCP:{server.name}] {spec.description or spec.name}"

    def get_parameters_schema(self) -> dict[str, Any]:
        schema = self.spec.input_schema or {"type": "object", "properties": {}}
        if not isinstance(schema, dict):
            return {"type": "object", "properties": {}}
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        return schema

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        try:
            with open_mcp_client(self.server) as client:
                result = client.call_tool(self.spec.name, arguments)
            return ToolResult(success=True, output=_format_mcp_result(result))
        except McpError as exc:
            return ToolResult(success=False, output="", error=f"MCP tool failed: {exc}")


def _format_mcp_result(result: dict[str, Any]) -> str:
    content = result.get("content") if isinstance(result, dict) else None
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict):
                parts.append(str(item))
                continue
            if item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            else:
                parts.append(json.dumps(item, ensure_ascii=False))
        return "\n".join(part for part in parts if part) or json.dumps(result, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False)
