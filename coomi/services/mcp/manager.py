"""MCP server manager."""
from __future__ import annotations

import re

from ...tools.registry import ToolRegistry
from .client import McpError, open_mcp_client
from .config import McpConfigStore
from .models import McpServerConfig, McpToolSpec, utc_now
from .tool_adapter import McpToolAdapter


class McpManager:
    def __init__(self, config: McpConfigStore | None = None):
        self.config = config or McpConfigStore()

    def list(self, enabled_only: bool = False) -> list[McpServerConfig]:
        servers = self.config.list_servers()
        if enabled_only:
            servers = [server for server in servers if server.enabled]
        return sorted(servers, key=lambda server: server.name.casefold())

    def get(self, name: str) -> McpServerConfig | None:
        return self.config.get(name)

    def add_stdio(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str = "",
        enabled: bool = True,
    ) -> McpServerConfig:
        server = McpServerConfig(
            name=_normalize_name(name),
            transport="stdio",
            enabled=enabled,
            command=command,
            args=args or [],
            env=env or {},
            cwd=cwd,
        )
        self.config.put(server)
        return server

    def add_http(
        self,
        name: str,
        url: str,
        headers: dict[str, str] | None = None,
        enabled: bool = True,
    ) -> McpServerConfig:
        server = McpServerConfig(
            name=_normalize_name(name),
            transport="http",
            enabled=enabled,
            url=url,
            headers=headers or {},
        )
        self.config.put(server)
        return server

    def add_sse(
        self,
        name: str,
        url: str,
        headers: dict[str, str] | None = None,
        enabled: bool = True,
    ) -> McpServerConfig:
        server = McpServerConfig(
            name=_normalize_name(name),
            transport="sse",
            enabled=enabled,
            url=url,
            headers=headers or {},
        )
        self.config.put(server)
        return server

    def enable(self, name: str, enabled: bool = True) -> McpServerConfig:
        server = self._require(name)
        server.enabled = enabled
        server.updated_at = utc_now()
        self.config.put(server)
        return server

    def remove(self, name: str) -> McpServerConfig:
        removed = self.config.remove(name)
        if not removed:
            raise McpError(f"MCP server not found: {name}")
        return removed

    def test(self, name: str) -> tuple[bool, str]:
        server = self._require(name)
        try:
            tools = self.list_tools(name)
            server.last_error = ""
            server.last_checked_at = utc_now()
            self.config.put(server)
            return True, f"Connected. Tools discovered: {len(tools)}"
        except Exception as exc:
            server.last_error = str(exc)
            server.last_checked_at = utc_now()
            self.config.put(server)
            return False, str(exc)

    def list_tools(self, name: str) -> list[McpToolSpec]:
        server = self._require(name)
        with open_mcp_client(server) as client:
            return client.list_tools()

    def register_enabled_tools(self, registry: ToolRegistry) -> list[str]:
        registered: list[str] = []
        for server in self.list(enabled_only=True):
            try:
                for spec in self.list_tools(server.name):
                    adapter = McpToolAdapter(server, spec)
                    registry.register(adapter)
                    registered.append(adapter.name)
                server.last_error = ""
                server.last_checked_at = utc_now()
            except Exception as exc:
                server.last_error = str(exc)
                server.last_checked_at = utc_now()
            self.config.put(server)
        return registered

    def info(self, name: str) -> str:
        server = self._require(name)
        lines = [
            f"MCP server: {server.name}",
            f"Enabled: {server.enabled}",
            f"Transport: {server.transport}",
        ]
        if server.transport == "stdio":
            lines.append(f"Command: {' '.join([server.command, *server.args]).strip()}")
            if server.cwd:
                lines.append(f"CWD: {server.cwd}")
        if server.url:
            lines.append(f"URL: {server.url}")
        if server.last_checked_at:
            lines.append(f"Last checked: {server.last_checked_at}")
        if server.last_error:
            lines.append(f"Last error: {server.last_error}")
        return "\n".join(lines)

    def _require(self, name: str) -> McpServerConfig:
        server = self.config.get(name)
        if not server:
            raise McpError(f"MCP server not found: {name}")
        return server


def _normalize_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", name.strip()).strip("-._")
    if not cleaned:
        raise McpError("MCP server name cannot be empty")
    return cleaned[:80]
