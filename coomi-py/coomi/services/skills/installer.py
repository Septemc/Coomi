"""Skill installation helpers."""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import uuid
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
    ignore = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".venv", "node_modules")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.staging-", dir=destination.parent)
    )
    staging = staging_root / "skill"
    backup = destination.parent / f".{destination.name}.backup-{uuid.uuid4().hex}"
    moved_existing = False
    try:
        shutil.copytree(source, staging, ignore=ignore)
        find_skill_root(staging)
        if destination.exists():
            destination.replace(backup)
            moved_existing = True
        try:
            staging.replace(destination)
        except Exception:
            if moved_existing and backup.exists() and not destination.exists():
                backup.replace(destination)
            raise
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
    finally:
        if staging_root.exists():
            shutil.rmtree(staging_root, ignore_errors=True)


def resolve_github_commit(url: str, timeout: int = 30) -> tuple[str, bool]:
    """Resolve the remote commit for a GitHub source.

    Returns ``(commit, immutable)``. Tags and explicit commit hashes are
    considered immutable so the marketplace doesn't repeatedly offer updates.
    """
    source = parse_github_url(url)
    ref = source.ref.strip()
    if re.fullmatch(r"[0-9a-fA-F]{7,40}", ref):
        return ref.lower(), True

    if not ref:
        return _ls_remote(source.clone_url, ["HEAD"], timeout)[0][0], False

    patterns = [
        f"refs/heads/{ref}",
        f"refs/tags/{ref}^{{}}",
        f"refs/tags/{ref}",
    ]
    rows = _ls_remote(source.clone_url, patterns, timeout)
    by_ref = {remote_ref: commit for commit, remote_ref in rows}
    branch_ref = f"refs/heads/{ref}"
    if branch_ref in by_ref:
        return by_ref[branch_ref], False
    peeled_tag = f"refs/tags/{ref}^{{}}"
    tag_ref = f"refs/tags/{ref}"
    if peeled_tag in by_ref:
        return by_ref[peeled_tag], True
    if tag_ref in by_ref:
        return by_ref[tag_ref], True
    raise SkillInstallError(f"GitHub ref not found: {ref}")


def _ls_remote(
    clone_url: str, patterns: list[str], timeout: int
) -> list[tuple[str, str]]:
    proc = subprocess.run(
        ["git", "ls-remote", clone_url, *patterns],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise SkillInstallError(proc.stderr.strip() or "git ls-remote failed")
    rows: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2:
            rows.append((parts[0], parts[1]))
    if not rows:
        raise SkillInstallError("GitHub source returned no matching commit")
    return rows


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
