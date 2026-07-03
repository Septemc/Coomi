"""Persistent MCP server configuration."""
from __future__ import annotations

import json
from pathlib import Path

from .models import McpServerConfig


class McpConfigStore:
    def __init__(self, config_path: str | Path | None = None):
        self.config_path = (
            Path(config_path)
            if config_path
            else Path.home() / ".coomi" / "config" / "mcp_servers.json"
        )
        self.data = self._load()

    def _load(self) -> dict:
        if self.config_path.exists():
            try:
                data = json.loads(self.config_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data.setdefault("version", 1)
                    data.setdefault("servers", {})
                    return data
            except json.JSONDecodeError:
                backup = self.config_path.with_suffix(".json.bak")
                self.config_path.replace(backup)
        return {"version": 1, "servers": {}}

    def save(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def reload(self) -> None:
        self.data = self._load()

    def list_servers(self) -> list[McpServerConfig]:
        return [
            McpServerConfig.from_dict(server)
            for server in self.data.get("servers", {}).values()
        ]

    def get(self, name: str) -> McpServerConfig | None:
        raw = self.data.get("servers", {}).get(name)
        return McpServerConfig.from_dict(raw) if isinstance(raw, dict) else None

    def put(self, server: McpServerConfig) -> None:
        self.data.setdefault("servers", {})[server.name] = server.to_dict()
        self.save()

    def remove(self, name: str) -> McpServerConfig | None:
        raw = self.data.setdefault("servers", {}).pop(name, None)
        if raw is None:
            return None
        self.save()
        return McpServerConfig.from_dict(raw)
