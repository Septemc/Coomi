"""Skill data models."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class SkillRecord:
    name: str
    path: str
    enabled: bool = True
    source_type: str = "local"
    source: str = ""
    version: str = ""
    commit: str = ""
    branch: str = ""
    subdir: str = ""
    description: str = ""
    installed_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SkillRecord":
        return cls(
            name=str(data.get("name", "")),
            path=str(data.get("path", "")),
            enabled=bool(data.get("enabled", True)),
            source_type=str(data.get("source_type", "local")),
            source=str(data.get("source", "")),
            version=str(data.get("version", "")),
            commit=str(data.get("commit", "")),
            branch=str(data.get("branch", "")),
            subdir=str(data.get("subdir", "")),
            description=str(data.get("description", "")),
            installed_at=str(data.get("installed_at", "")) or utc_now(),
            updated_at=str(data.get("updated_at", "")) or utc_now(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "source_type": self.source_type,
            "source": self.source,
            "path": self.path,
            "version": self.version,
            "commit": self.commit,
            "branch": self.branch,
            "subdir": self.subdir,
            "description": self.description,
            "installed_at": self.installed_at,
            "updated_at": self.updated_at,
        }

    @property
    def skill_path(self) -> Path:
        return Path(self.path)

    @property
    def skill_md_path(self) -> Path:
        return self.skill_path / "SKILL.md"


@dataclass(frozen=True)
class SkillUpdateStatus:
    name: str
    source_type: str
    current_commit: str = ""
    remote_commit: str = ""
    update_available: bool = False
    checked_at: str = field(default_factory=utc_now)
    message: str = ""
    error: str = ""
