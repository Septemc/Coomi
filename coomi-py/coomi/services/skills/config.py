"""Persistent Skill configuration."""
from __future__ import annotations

import json
from pathlib import Path

from .models import SkillRecord


class SkillConfig:
    def __init__(
        self,
        config_path: str | Path | None = None,
        skills_dir: str | Path | None = None,
    ):
        home = Path.home()
        self.config_path = Path(config_path) if config_path else home / ".coomi" / "config" / "skills.json"
        self.skills_dir = Path(skills_dir) if skills_dir else home / ".coomi" / "skills"
        self.data = self._load()

    def _load(self) -> dict:
        if self.config_path.exists():
            try:
                data = json.loads(self.config_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data.setdefault("version", 1)
                    data.setdefault("skills", {})
                    return data
            except json.JSONDecodeError:
                backup = self.config_path.with_suffix(".json.bak")
                self.config_path.replace(backup)
        return {"version": 1, "skills": {}}

    def save(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def reload(self) -> None:
        self.data = self._load()

    def list_records(self) -> list[SkillRecord]:
        return [
            SkillRecord.from_dict(record)
            for record in self.data.get("skills", {}).values()
        ]

    def get(self, name: str) -> SkillRecord | None:
        record = self.data.get("skills", {}).get(name)
        return SkillRecord.from_dict(record) if isinstance(record, dict) else None

    def put(self, record: SkillRecord) -> None:
        self.data.setdefault("skills", {})[record.name] = record.to_dict()
        self.save()

    def remove(self, name: str) -> SkillRecord | None:
        raw = self.data.setdefault("skills", {}).pop(name, None)
        if raw is None:
            return None
        self.save()
        return SkillRecord.from_dict(raw)
