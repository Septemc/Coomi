"""MCP data models."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class McpServerConfig:
    name: str
    transport: str = "stdio"
    enabled: bool = True
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    last_error: str = ""
    last_checked_at: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "McpServerConfig":
        return cls(
            name=str(data.get("name", "")),
            transport=str(data.get("transport", "stdio")),
            enabled=bool(data.get("enabled", True)),
            command=str(data.get("command", "")),
            args=[str(item) for item in data.get("args", [])],
            env={str(k): str(v) for k, v in data.get("env", {}).items()},
            cwd=str(data.get("cwd", "")),
            url=str(data.get("url", "")),
            headers={str(k): str(v) for k, v in data.get("headers", {}).items()},
            last_error=str(data.get("last_error", "")),
            last_checked_at=str(data.get("last_checked_at", "")),
            created_at=str(data.get("created_at", "")) or utc_now(),
            updated_at=str(data.get("updated_at", "")) or utc_now(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "transport": self.transport,
            "enabled": self.enabled,
            "command": self.command,
            "args": self.args,
            "env": self.env,
            "cwd": self.cwd,
            "url": self.url,
            "headers": self.headers,
            "last_error": self.last_error,
            "last_checked_at": self.last_checked_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class McpToolSpec:
    server_name: str
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
