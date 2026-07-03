"""Skill installation helpers."""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse


class SkillInstallError(RuntimeError):
    pass


class GitHubSkillSource:
    def __init__(self, clone_url: str, ref: str = "", subdir: str = ""):
        self.clone_url = clone_url
        self.ref = ref
        self.subdir = subdir.strip("/\\")


def is_github_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and parsed.netloc.lower() == "github.com"


def parse_github_url(url: str) -> GitHubSkillSource:
    parsed = urlparse(url)
    if parsed.netloc.lower() != "github.com":
        raise SkillInstallError("Only github.com URLs are supported")

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        raise SkillInstallError("GitHub URL must include owner and repo")

    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    clone_url = f"https://github.com/{owner}/{repo}.git"

    query = parse_qs(parsed.query)
    ref = (query.get("ref") or query.get("branch") or query.get("tag") or [""])[0]
    subdir = (query.get("subdir") or [""])[0]

    if len(parts) >= 4 and parts[2] in {"tree", "blob"}:
        ref = ref or parts[3]
        if len(parts) > 4:
            subdir = subdir or "/".join(parts[4:])

    return GitHubSkillSource(clone_url=clone_url, ref=ref, subdir=subdir)


def normalize_skill_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", name.strip()).strip("-._")
    if not cleaned:
        raise SkillInstallError("Skill name cannot be empty")
    return cleaned[:80]


def find_skill_root(path: Path) -> Path:
    if (path / "SKILL.md").is_file():
        return path
    matches = list(path.rglob("SKILL.md"))
    if len(matches) == 1:
        return matches[0].parent
    if not matches:
        raise SkillInstallError("Skill directory must contain SKILL.md")
    raise SkillInstallError("Multiple SKILL.md files found; install a specific subdirectory")


def copy_skill_tree(source: Path, destination: Path) -> None:
    source = source.resolve()
    if not source.is_dir():
        raise SkillInstallError(f"Skill source is not a directory: {source}")
    find_skill_root(source)
    if destination.exists():
        shutil.rmtree(destination)
    ignore = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".venv", "node_modules")
    shutil.copytree(source, destination, ignore=ignore)


def install_from_github(url: str, destination: Path) -> tuple[Path, GitHubSkillSource, str]:
    source = parse_github_url(url)
    with tempfile.TemporaryDirectory(prefix="coomi-skill-") as temp:
        temp_path = Path(temp)
        cmd = ["git", "clone", "--depth", "1"]
        if source.ref:
            cmd += ["--branch", source.ref]
        cmd += [source.clone_url, str(temp_path / "repo")]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            # Depth-limited clone cannot checkout arbitrary commits; retry full clone.
            if source.ref:
                proc = subprocess.run(
                    ["git", "clone", source.clone_url, str(temp_path / "repo")],
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
                if proc.returncode == 0:
                    checkout = subprocess.run(
                        ["git", "-C", str(temp_path / "repo"), "checkout", source.ref],
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                    if checkout.returncode != 0:
                        raise SkillInstallError(checkout.stderr.strip() or "git checkout failed")
            if proc.returncode != 0:
                raise SkillInstallError(proc.stderr.strip() or "git clone failed")

        repo_path = temp_path / "repo"
        skill_source = repo_path / source.subdir if source.subdir else find_skill_root(repo_path)
        if source.subdir:
            skill_source = find_skill_root(skill_source)
        copy_skill_tree(skill_source, destination)
        commit = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        commit_id = commit.stdout.strip() if commit.returncode == 0 else ""
    return destination, source, commit_id
