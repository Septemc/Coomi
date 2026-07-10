"""Skill management and prompt selection."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .config import SkillConfig
from .installer import (
    SkillInstallError,
    copy_skill_tree,
    find_skill_root,
    install_from_github,
    is_github_url,
    normalize_skill_name,
    resolve_github_commit,
)
from .models import SkillRecord, SkillUpdateStatus, utc_now


class SkillManager:
    def __init__(self, config: SkillConfig | None = None):
        self.config = config or SkillConfig()

    @property
    def skills_dir(self) -> Path:
        return self.config.skills_dir

    def list(self, enabled_only: bool = False) -> list[SkillRecord]:
        records = self.config.list_records()
        if enabled_only:
            records = [record for record in records if record.enabled]
        return sorted(records, key=lambda record: record.name.casefold())

    def get(self, name: str) -> SkillRecord | None:
        return self.config.get(name)

    def install(self, source: str, name: str | None = None, enabled: bool = True) -> SkillRecord:
        if is_github_url(source):
            return self._install_github(source, name=name, enabled=enabled)
        return self._install_local(Path(source), name=name, enabled=enabled)

    def _install_local(self, source: Path, name: str | None, enabled: bool) -> SkillRecord:
        root = source.expanduser().resolve()
        if not root.is_dir():
            raise SkillInstallError(f"Skill source not found: {source}")
        root = find_skill_root(root)
        skill_name = normalize_skill_name(name or _read_skill_name(root) or root.name)
        destination = self._destination_for(skill_name)
        copy_skill_tree(root, destination)
        record = SkillRecord(
            name=skill_name,
            path=str(destination),
            enabled=enabled,
            source_type="local",
            source=str(root),
            description=_read_skill_description(destination),
        )
        self.config.put(record)
        return record

    def _install_github(self, url: str, name: str | None, enabled: bool) -> SkillRecord:
        parsed_name = _name_from_github_url(url)
        skill_name = normalize_skill_name(name or parsed_name)
        destination = self._destination_for(skill_name)
        _, github_source, commit = install_from_github(url, destination)
        record = SkillRecord(
            name=skill_name,
            path=str(destination),
            enabled=enabled,
            source_type="github",
            source=url,
            branch=github_source.ref,
            subdir=github_source.subdir,
            commit=commit,
            description=_read_skill_description(destination),
        )
        self.config.put(record)
        return record

    def update(self, name: str) -> SkillRecord:
        record = self._require(name)
        if record.source_type == "github":
            return self._install_github(record.source, name=record.name, enabled=record.enabled)
        if record.source_type == "local":
            return self._install_local(Path(record.source), name=record.name, enabled=record.enabled)
        raise SkillInstallError(f"Unsupported source type: {record.source_type}")

    def check_update(self, name: str) -> SkillUpdateStatus:
        record = self._require(name)
        if record.source_type == "local":
            return SkillUpdateStatus(
                name=record.name,
                source_type="local",
                update_available=True,
                message="本地来源可重新扫描并安装；再次按 Enter 应用。",
            )
        if record.source_type != "github":
            raise SkillInstallError(f"Unsupported source type: {record.source_type}")

        try:
            remote_commit, immutable = resolve_github_commit(record.source)
        except (OSError, subprocess.SubprocessError, SkillInstallError) as exc:
            return SkillUpdateStatus(
                name=record.name,
                source_type="github",
                current_commit=record.commit,
                message="检查更新失败。",
                error=str(exc),
            )

        if immutable:
            return SkillUpdateStatus(
                name=record.name,
                source_type="github",
                current_commit=record.commit,
                remote_commit=remote_commit,
                update_available=False,
                message="该 Skill 固定到 tag 或 commit，不自动跟随更新。",
            )
        update_available = not record.commit or record.commit != remote_commit
        return SkillUpdateStatus(
            name=record.name,
            source_type="github",
            current_commit=record.commit,
            remote_commit=remote_commit,
            update_available=update_available,
            message="发现新版本。" if update_available else "已是最新版本。",
        )

    def enable(self, name: str, enabled: bool = True) -> SkillRecord:
        record = self._require(name)
        record.enabled = enabled
        record.updated_at = utc_now()
        self.config.put(record)
        return record

    def remove(self, name: str) -> SkillRecord:
        record = self._require(name)
        path = self._safe_installed_path(record.path)
        removed = self.config.remove(record.name)
        if path.exists():
            shutil.rmtree(path)
        return removed or record

    def info(self, name: str) -> str:
        record = self._require(name)
        lines = [
            f"Skill: {record.name}",
            f"Enabled: {record.enabled}",
            f"Source: {record.source_type} {record.source}",
            f"Path: {record.path}",
        ]
        if record.branch:
            lines.append(f"Ref: {record.branch}")
        if record.subdir:
            lines.append(f"Subdir: {record.subdir}")
        if record.commit:
            lines.append(f"Commit: {record.commit}")
        if record.description:
            lines.append(f"Description: {record.description}")
        return "\n".join(lines)

    def build_prompt_context(self, current_context: str = "") -> str:
        enabled = self.list(enabled_only=True)
        if not enabled:
            return ""

        selected = [record for record in enabled if _skill_requested(record, current_context)]
        index_lines = [
            "## Available Skills",
            "Enabled skills are available by name. Use a skill when it is clearly relevant, "
            "or when the user references it with $SkillName.",
        ]
        for record in enabled:
            desc = record.description or _read_skill_description(Path(record.path))
            suffix = f": {desc}" if desc else ""
            index_lines.append(f"- ${record.name}{suffix}")

        if not selected:
            return "\n".join(index_lines)

        loaded = ["## Loaded Skill Instructions"]
        for record in selected:
            content = _read_skill_body(Path(record.path), limit=12_000)
            loaded.append(f"### ${record.name}\n{content}")
        return "\n\n".join(["\n".join(index_lines), "\n\n".join(loaded)])

    def _destination_for(self, name: str) -> Path:
        return self.skills_dir / name

    def _safe_installed_path(self, path: str) -> Path:
        resolved = Path(path).resolve()
        root = self.skills_dir.resolve()
        if resolved == root or root not in resolved.parents:
            raise SkillInstallError(f"Refusing to modify path outside skills dir: {resolved}")
        return resolved

    def _require(self, name: str) -> SkillRecord:
        record = self.config.get(name)
        if not record:
            raise SkillInstallError(f"Skill not found: {name}")
        return record


def _read_skill_name(path: Path) -> str:
    skill_md = path / "SKILL.md"
    if not skill_md.exists():
        return ""
    for line in skill_md.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return ""


def _read_skill_description(path: Path) -> str:
    skill_md = path / "SKILL.md"
    if not skill_md.exists():
        return ""
    lines = skill_md.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.lower().startswith("description:"):
            return stripped.split(":", 1)[1].strip()
        return stripped[:240]
    return ""


def _read_skill_body(path: Path, limit: int) -> str:
    skill_md = path / "SKILL.md"
    if not skill_md.exists():
        return "(SKILL.md missing)"
    text = skill_md.read_text(encoding="utf-8", errors="replace")
    return text[:limit] + ("\n[truncated]" if len(text) > limit else "")


def _skill_requested(record: SkillRecord, current_context: str) -> bool:
    context = current_context.casefold()
    name = record.name.casefold()
    return f"${name}" in context or f"@{name}" in context


def _name_from_github_url(url: str) -> str:
    tail = url.split("github.com/", 1)[-1].split("?", 1)[0].strip("/")
    parts = [part for part in tail.split("/") if part]
    if len(parts) >= 5 and parts[2] in {"tree", "blob"}:
        return parts[-1]
    if len(parts) >= 2:
        return parts[1].removesuffix(".git")
    return "skill"
