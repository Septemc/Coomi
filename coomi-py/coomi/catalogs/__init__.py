"""Built-in, offline-readable catalogs for Skills and MCP servers."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True)
class CatalogInput:
    key: str
    label: str
    description: str = ""
    placeholder: str = ""
    required: bool = True
    secret: bool = False
    kind: str = "text"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CatalogInput":
        return cls(
            key=str(data.get("key", "")),
            label=str(data.get("label", "")),
            description=str(data.get("description", "")),
            placeholder=str(data.get("placeholder", "")),
            required=bool(data.get("required", True)),
            secret=bool(data.get("secret", False)),
            kind=str(data.get("kind", "text")),
        )

    def validate(self, value: str) -> str:
        if not value:
            return ""
        if self.kind in {"directory", "git_repository"}:
            path = Path(value).expanduser()
            if not path.is_absolute():
                raise ValueError(f"{self.label}必须使用绝对路径")
            if not path.is_dir():
                raise ValueError(f"{self.label}不存在或不是目录")
            if self.kind == "git_repository" and not (path / ".git").exists():
                raise ValueError(f"{self.label}不是可识别的 Git 仓库")
        elif self.kind == "postgres_uri":
            if urlparse(value).scheme not in {"postgres", "postgresql"}:
                raise ValueError(f"{self.label}必须是 postgres:// 或 postgresql:// URI")
        elif self.kind == "sqlite_dsn":
            if not value.casefold().startswith("sqlite:"):
                raise ValueError(f"{self.label}必须以 sqlite: 开头")
        return value


@dataclass(frozen=True)
class SkillCatalogEntry:
    id: str
    name: str
    description: str
    source_url: str
    repository: str
    ref: str
    subdir: str
    homepage: str
    author: str
    tags: tuple[str, ...] = ()
    requirements: tuple[str, ...] = ()
    verified: bool = False
    license: str = ""
    install_notes: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SkillCatalogEntry":
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            description=str(data.get("description", "")),
            source_url=str(data.get("source_url", "")),
            repository=str(data.get("repository", "")),
            ref=str(data.get("ref", "")),
            subdir=str(data.get("subdir", "")),
            homepage=str(data.get("homepage", "")),
            author=str(data.get("author", "")),
            tags=tuple(str(item) for item in data.get("tags", [])),
            requirements=tuple(str(item) for item in data.get("requirements", [])),
            verified=bool(data.get("verified", False)),
            license=str(data.get("license", "")),
            install_notes=str(data.get("install_notes", "")),
        )


@dataclass(frozen=True)
class McpCatalogEntry:
    id: str
    name: str
    description: str
    homepage: str
    transport: str
    command: str = ""
    args: tuple[str, ...] = ()
    url_template: str = ""
    env: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    required_env: tuple[CatalogInput, ...] = ()
    required_parameters: tuple[CatalogInput, ...] = ()
    runtime_requirements: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ()
    official: bool = False
    verified: bool = False
    license: str = ""
    install_notes: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "McpCatalogEntry":
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            description=str(data.get("description", "")),
            homepage=str(data.get("homepage", "")),
            transport=str(data.get("transport", "stdio")),
            command=str(data.get("command", "")),
            args=tuple(str(item) for item in data.get("args", [])),
            url_template=str(data.get("url_template", "")),
            env={str(k): str(v) for k, v in data.get("env", {}).items()},
            headers={str(k): str(v) for k, v in data.get("headers", {}).items()},
            required_env=tuple(
                CatalogInput.from_dict(item) for item in data.get("required_env", [])
            ),
            required_parameters=tuple(
                CatalogInput.from_dict(item)
                for item in data.get("required_parameters", [])
            ),
            runtime_requirements=tuple(
                str(item) for item in data.get("runtime_requirements", [])
            ),
            platforms=tuple(str(item) for item in data.get("platforms", [])),
            official=bool(data.get("official", False)),
            verified=bool(data.get("verified", False)),
            license=str(data.get("license", "")),
            install_notes=str(data.get("install_notes", "")),
        )

    @property
    def inputs(self) -> tuple[CatalogInput, ...]:
        return self.required_parameters + self.required_env

    @property
    def signature(self) -> str:
        payload = json.dumps(
            {
                "transport": self.transport,
                "command": self.command,
                "args": self.args,
                "url_template": self.url_template,
                "env_keys": sorted(self.env),
                "header_keys": sorted(self.headers),
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def render(self, values: dict[str, str]) -> dict[str, Any]:
        missing = [item.label for item in self.inputs if item.required and not values.get(item.key)]
        if missing:
            raise ValueError(f"缺少必填配置：{', '.join(missing)}")
        clean_values = {
            item.key: item.validate(values.get(item.key, "").strip()) for item in self.inputs
        }
        clean_values.update(
            {key: value for key, value in values.items() if key not in clean_values}
        )

        def substitute(value: str) -> str:
            rendered = value
            for key, replacement in clean_values.items():
                rendered = rendered.replace("{{" + key + "}}", replacement)
            return rendered

        command = substitute(self.command)
        args = [substitute(item) for item in self.args]
        url = substitute(self.url_template)
        env = {key: substitute(value) for key, value in self.env.items()}
        headers = {key: substitute(value) for key, value in self.headers.items()}
        rendered_values = [command, *args, url, *env.values(), *headers.values()]
        if any("{{" in value or "}}" in value for value in rendered_values):
            raise ValueError(f"{self.name} 的精选配置仍包含未填写的模板变量")
        if os.name == "nt" and command.casefold() in {"npx", "npm", "pnpm", "yarn"}:
            args = ["/d", "/s", "/c", command, *args]
            command = os.environ.get("COMSPEC", "cmd.exe")
        return {
            "name": self.id,
            "transport": self.transport,
            "command": command,
            "args": args,
            "url": url,
            "env": env,
            "headers": headers,
            "catalog_id": self.id,
            "catalog_signature": self.signature,
        }


def _load_json(name: str) -> list[dict[str, Any]]:
    resource = files(__package__).joinpath(name)
    data = json.loads(resource.read_text(encoding="utf-8"))
    entries = data.get("entries", []) if isinstance(data, dict) else []
    if not isinstance(entries, list):
        raise ValueError(f"Invalid catalog format: {name}")
    return [item for item in entries if isinstance(item, dict)]


def load_skill_catalog() -> list[SkillCatalogEntry]:
    return [SkillCatalogEntry.from_dict(item) for item in _load_json("skills.json")]


def load_mcp_catalog() -> list[McpCatalogEntry]:
    return [McpCatalogEntry.from_dict(item) for item in _load_json("mcp.json")]


__all__ = [
    "CatalogInput",
    "McpCatalogEntry",
    "SkillCatalogEntry",
    "load_mcp_catalog",
    "load_skill_catalog",
]
