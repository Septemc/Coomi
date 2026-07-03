"""Detect and apply pasted Provider, Skill, and MCP configuration inputs."""
from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .llm.config import ConfigManager, ProviderConfig, TOOL_PROTOCOLS
from .mcp import McpManager
from .skills.installer import (
    SkillInstallError,
    is_github_url,
    normalize_skill_name,
    parse_github_url,
)
from .skills.manager import SkillManager
from ..tools.registry import ToolRegistry


INTENT_NONE = "none"
INTENT_PROVIDER = "provider_configure"
INTENT_SKILL = "skill_install"
INTENT_MCP = "mcp_configure"


@dataclass
class InputIntent:
    kind: str
    raw: str
    data: dict[str, Any] = field(default_factory=dict)
    detected_as: str = ""

    @property
    def is_config_intent(self) -> bool:
        return self.kind in {INTENT_PROVIDER, INTENT_SKILL, INTENT_MCP}


@dataclass
class AutoConfigResult:
    success: bool
    detected: str
    status: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    repairs: list[str] = field(default_factory=list)


class InputIntentDetector:
    """Deterministic parser for pasted auto-configuration inputs."""

    def detect(self, text: str) -> InputIntent:
        raw = (text or "").strip()
        if not raw:
            return InputIntent(INTENT_NONE, text)

        json_payload = _extract_json_object(raw)
        if isinstance(json_payload, dict):
            if _looks_like_provider_json(json_payload, raw):
                return InputIntent(
                    INTENT_PROVIDER,
                    raw,
                    {"config": json_payload},
                    "LLM Provider JSON",
                )
            if _looks_like_mcp_json(json_payload, raw):
                return InputIntent(
                    INTENT_MCP,
                    raw,
                    {"config": json_payload},
                    "MCP JSON",
                )

        command = _strip_leading_action(raw)
        if _looks_like_mcp_command(command):
            return InputIntent(
                INTENT_MCP,
                raw,
                {"command_text": command},
                "MCP command",
            )

        maybe_value = _strip_labeled_value(raw).strip().strip('"')
        if _is_github_skill_url(maybe_value) and _is_skill_context(raw, maybe_value):
            return InputIntent(
                INTENT_SKILL,
                raw,
                {"source": maybe_value},
                "Skill GitHub URL",
            )
        if _looks_like_local_skill_path(maybe_value) and _is_skill_context(raw, maybe_value):
            return InputIntent(
                INTENT_SKILL,
                raw,
                {"source": maybe_value},
                "Skill local path",
            )
        if _looks_like_mcp_url(maybe_value, raw):
            return InputIntent(
                INTENT_MCP,
                raw,
                {"config": {"url": maybe_value}},
                "MCP URL",
            )

        return InputIntent(INTENT_NONE, raw)


class ProviderAutoConfigurator:
    def __init__(self, config_manager: ConfigManager | None = None):
        self.config_manager = config_manager or ConfigManager()

    def configure(self, config: dict[str, Any]) -> AutoConfigResult:
        try:
            provider = normalize_provider_config(config)
        except ValueError as exc:
            return AutoConfigResult(
                success=False,
                detected="LLM Provider JSON",
                status="Error",
                message=f"Detected: LLM Provider JSON\nStatus: Error\nReason: {exc}",
            )

        self.config_manager.add_provider(provider)
        self.config_manager.set_active(provider.id)
        api_key = redact_secret(provider.api_key)
        message = "\n".join(
            [
                "Detected: LLM Provider JSON",
                "Action: Added and activated",
                f"Provider: {provider.id}",
                f"Model: {provider.model}",
                f"Tool protocol: {provider.tool_protocol}",
                f"API key: {api_key}",
                "Status: Ready",
            ]
        )
        return AutoConfigResult(
            success=True,
            detected="LLM Provider JSON",
            status="Ready",
            message=message,
            data={"provider_id": provider.id, "model": provider.model},
        )


class SkillAutoInstaller:
    def __init__(self, manager: SkillManager):
        self.manager = manager

    def install(self, source: str, name: str | None = None) -> AutoConfigResult:
        if is_github_url(source):
            return self._install_github(source, name=name)
        return self._install_local(Path(source), name=name)

    def _install_local(self, source: Path, name: str | None = None) -> AutoConfigResult:
        root = source.expanduser().resolve()
        if not root.is_dir():
            return AutoConfigResult(
                success=False,
                detected="Skill local path",
                status="Error",
                message=f"Detected: Skill local path\nStatus: Error\nReason: source not found: {source}",
            )

        candidates = _find_skill_candidates(root)
        if len(candidates) > 1:
            lines = [
                "Detected: Skill local path",
                "Status: Needs selection",
                "Reason: Multiple SKILL.md files found",
                "Candidates:",
            ]
            for index, candidate in enumerate(candidates, start=1):
                lines.append(f"{index}. {candidate.relative_to(root)}")
            return AutoConfigResult(
                success=False,
                detected="Skill local path",
                status="Needs selection",
                message="\n".join(lines),
                data={"candidates": [str(candidate) for candidate in candidates]},
            )

        skill_root = candidates[0].parent if candidates else root
        repairs: list[str] = []
        with tempfile.TemporaryDirectory(prefix="coomi-skill-repair-") as temp:
            repaired = Path(temp) / "skill"
            _copy_tree(skill_root, repaired)
            _repair_skill_metadata(repaired, fallback_name=name or skill_root.name, repairs=repairs)
            record = self.manager.install(str(repaired), name=name, enabled=True)
            record.source = str(root)
            self.manager.config.put(record)

        context = self.manager.build_prompt_context(f"use ${record.name}")
        if f"${record.name}" not in context:
            repairs.append("installed skill was not selected by prompt context verification")

        message = _format_skill_success(record.name, record.path, repairs)
        return AutoConfigResult(
            success=True,
            detected="Skill local path",
            status="Ready",
            message=message,
            data={"name": record.name, "path": record.path},
            repairs=repairs,
        )

    def _install_github(self, url: str, name: str | None = None) -> AutoConfigResult:
        source = parse_github_url(url)
        repairs: list[str] = []
        with tempfile.TemporaryDirectory(prefix="coomi-skill-github-") as temp:
            repo_path = Path(temp) / "repo"
            try:
                _clone_github_source(source.clone_url, repo_path, source.ref)
            except SkillInstallError as exc:
                return AutoConfigResult(
                    success=False,
                    detected="Skill GitHub URL",
                    status="Error",
                    message=f"Detected: Skill GitHub URL\nStatus: Error\nReason: {exc}",
                )

            search_root = repo_path / source.subdir if source.subdir else repo_path
            candidates = _find_skill_candidates(search_root)
            if len(candidates) > 1 and not source.subdir:
                lines = [
                    "Detected: Skill URL",
                    "Status: Needs selection",
                    "Reason: Multiple SKILL.md files found",
                    "Candidates:",
                ]
                for index, candidate in enumerate(candidates, start=1):
                    lines.append(f"{index}. {candidate.relative_to(repo_path)}")
                return AutoConfigResult(
                    success=False,
                    detected="Skill GitHub URL",
                    status="Needs selection",
                    message="\n".join(lines),
                    data={"candidates": [str(candidate.relative_to(repo_path)) for candidate in candidates]},
                )

            if candidates:
                skill_root = candidates[0].parent
            else:
                skill_root = search_root
            if source.subdir:
                repairs.append(f"selected subdirectory {source.subdir}")

            repaired = Path(temp) / "skill"
            _copy_tree(skill_root, repaired)
            _repair_skill_metadata(repaired, fallback_name=name or repaired.name, repairs=repairs)
            record = self.manager.install(str(repaired), name=name, enabled=True)
            record.source_type = "github"
            record.source = url
            record.branch = source.ref
            record.subdir = source.subdir
            try:
                commit = subprocess.run(
                    ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                record.commit = commit.stdout.strip() if commit.returncode == 0 else ""
            except Exception:
                record.commit = ""
            self.manager.config.put(record)

        message = _format_skill_success(record.name, record.path, repairs, detected="Skill GitHub URL")
        return AutoConfigResult(
            success=True,
            detected="Skill GitHub URL",
            status="Ready",
            message=message,
            data={"name": record.name, "path": record.path},
            repairs=repairs,
        )


class McpAutoConfigurator:
    def __init__(self, manager: McpManager, registry: ToolRegistry | None = None):
        self.manager = manager
        self.registry = registry

    def configure(self, config: dict[str, Any] | str) -> AutoConfigResult:
        try:
            normalized = normalize_mcp_config(config)
        except ValueError as exc:
            return AutoConfigResult(
                success=False,
                detected="MCP configuration",
                status="Error",
                message=f"Detected: MCP configuration\nStatus: Error\nReason: {exc}",
            )

        transport = normalized["transport"]
        if transport == "stdio":
            server = self.manager.add_stdio(
                normalized["name"],
                normalized["command"],
                args=normalized.get("args", []),
                env=normalized.get("env", {}),
                cwd=normalized.get("cwd", ""),
                enabled=True,
            )
        elif transport == "sse":
            server = self.manager.add_sse(
                normalized["name"],
                normalized["url"],
                headers=normalized.get("headers", {}),
                enabled=True,
            )
        else:
            server = self.manager.add_http(
                normalized["name"],
                normalized["url"],
                headers=normalized.get("headers", {}),
                enabled=True,
            )

        ok, test_message = self.manager.test(server.name)
        registered: list[str] = []
        if ok and self.registry:
            registered = self.manager.register_enabled_tools(self.registry)

        detected = f"MCP {transport} server"
        lines = [
            f"Detected: {detected}",
            f"Name: {server.name}",
            f"Transport: {server.transport}",
            f"Test: {'Passed' if ok else 'Failed'}",
        ]
        if not ok:
            lines.append(f"Reason: {test_message}")
            lines.append("Next: verify the server command, URL, credentials, and network access.")
        else:
            lines.append(f"Tools registered: {len(registered)}")
            if registered:
                lines.append("Registered names:")
                lines.extend(f"- {name}" for name in registered)

        return AutoConfigResult(
            success=ok,
            detected=detected,
            status="Ready" if ok else "Test failed",
            message="\n".join(lines),
            data={"name": server.name, "transport": server.transport, "registered": registered},
        )


def normalize_provider_config(data: dict[str, Any]) -> ProviderConfig:
    normalized = _alias_dict(
        data,
        {
            "id": ("id", "name", "provider_id"),
            "type": ("type", "provider", "type_name"),
            "display": ("display", "title", "label"),
            "api_key": ("api_key", "key", "token"),
            "base_url": ("base_url", "baseUrl", "url", "endpoint"),
            "model": ("model", "model_name"),
            "fast_model": ("fast_model", "fastModel"),
            "tool_protocol": ("tool_protocol", "toolProtocol", "protocol"),
        },
    )
    provider_id = _normalize_name(str(normalized.get("id") or ""))
    api_key = str(normalized.get("api_key") or "")
    model = str(normalized.get("model") or "")
    if not provider_id:
        raise ValueError("Provider id is required")
    if not api_key:
        raise ValueError("Provider api_key is required")
    if not model:
        raise ValueError("Provider model is required")
    protocol = str(normalized.get("tool_protocol") or "auto").strip().casefold().replace("-", "_")
    if protocol not in TOOL_PROTOCOLS:
        raise ValueError(f"tool_protocol must be one of {sorted(TOOL_PROTOCOLS)}")

    provider_type = str(normalized.get("type") or "generic").strip().casefold()
    if provider_type == "deepseek":
        provider_type = "generic"
    return ProviderConfig(
        id=provider_id,
        type=provider_type,
        display=str(normalized.get("display") or provider_id),
        api_key=api_key,
        base_url=str(normalized.get("base_url") or ""),
        model=model,
        fast_model=str(normalized.get("fast_model") or "") or None,
        tool_protocol=protocol,
    )


def normalize_mcp_config(config: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(config, str):
        return _normalize_mcp_command(config)
    data = _alias_dict(
        config,
        {
            "name": ("name", "server", "id"),
            "transport": ("transport", "type", "protocol"),
            "url": ("url", "base_url", "baseUrl", "endpoint"),
            "command": ("command", "cmd", "bin"),
            "args": ("args", "arguments", "params"),
            "env": ("env", "environment"),
            "headers": ("headers",),
            "cwd": ("cwd", "working_directory", "workingDirectory"),
        },
    )
    name = _normalize_name(str(data.get("name") or _name_from_url(str(data.get("url") or "")) or "mcp"))
    command = str(data.get("command") or "")
    url = str(data.get("url") or "")
    transport = str(data.get("transport") or "").strip().casefold()
    if command:
        transport = "stdio"
    elif transport == "sse" or "sse" in url.casefold():
        transport = "sse"
    elif url:
        transport = "http"
    if transport not in {"stdio", "http", "sse"}:
        raise ValueError("MCP transport must be stdio, http, or sse")
    if transport == "stdio" and not command:
        raise ValueError("MCP stdio server requires command")
    if transport in {"http", "sse"}:
        if not url:
            raise ValueError("MCP HTTP/SSE server requires url")
        url = _ensure_url_scheme(url)
    args = data.get("args") or []
    if isinstance(args, str):
        args = [args]
    env = data.get("env") or {}
    if not isinstance(env, dict):
        env = {}
    headers = data.get("headers") or {}
    if not isinstance(headers, dict):
        headers = {}
    return {
        "name": name,
        "transport": transport,
        "command": command,
        "args": [str(item) for item in args],
        "env": {str(k): str(v) for k, v in env.items()},
        "cwd": str(data.get("cwd") or ""),
        "url": url,
        "headers": {str(k): str(v) for k, v in headers.items()},
    }


def redact_secret(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "****"
    return f"{text[:3]}****{text[-4:]}"


def _extract_json_object(text: str) -> dict[str, Any] | None:
    stripped = _strip_labeled_value(text)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(stripped[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _strip_labeled_value(text: str) -> str:
    stripped = text.strip()
    match = re.search(r"(?i)(?:install skill|add skill|skill|mcp|provider|模型配置|配置 provider|添加这个 mcp|安装这个 skill)\s*[:：]\s*(.+)$", stripped, re.DOTALL)
    if match:
        return match.group(1).strip()
    return stripped


def _strip_leading_action(text: str) -> str:
    stripped = text.strip()
    lowered = stripped.casefold()
    if lowered.startswith("mcp add "):
        return "/" + stripped
    return stripped


def _looks_like_provider_json(data: dict[str, Any], raw: str) -> bool:
    keys = {_normalize_key(key) for key in data}
    has_key = bool(keys & {"apikey", "key", "token"})
    has_model = bool(keys & {"model", "modelname"})
    return (has_key and has_model) or ("provider" in raw.casefold() and has_model)


def _looks_like_mcp_json(data: dict[str, Any], raw: str) -> bool:
    keys = {_normalize_key(key) for key in data}
    return bool(keys & {"transport", "protocol", "command", "cmd", "bin", "url", "endpoint"}) and (
        "mcp" in raw.casefold() or not bool(keys & {"apikey", "key", "token"})
    )


def _looks_like_mcp_command(text: str) -> bool:
    return bool(re.match(r"^/?mcp\s+add\s+\S+\s+(stdio|http|sse)\s+\S+", text.strip(), re.IGNORECASE))


def _normalize_mcp_command(text: str) -> dict[str, Any]:
    try:
        parts = shlex.split(text, posix=False)
    except ValueError as exc:
        raise ValueError(f"Invalid MCP command: {exc}") from exc
    if parts and parts[0].lower() in {"/mcp", "mcp"}:
        parts = parts[1:]
    if len(parts) < 4 or parts[0].lower() != "add":
        raise ValueError("Usage: /mcp add <name> stdio <command> [args...] | /mcp add <name> http|sse <url>")
    name = parts[1]
    transport = parts[2].lower()
    if transport == "stdio":
        return normalize_mcp_config(
            {"name": name, "transport": transport, "command": parts[3], "args": parts[4:]}
        )
    return normalize_mcp_config({"name": name, "transport": transport, "url": parts[3]})


def _is_github_skill_url(value: str) -> bool:
    return is_github_url(value)


def _is_skill_context(raw: str, value: str) -> bool:
    lowered = raw.casefold()
    if "skill" in lowered:
        return True
    return raw.strip().strip('"') == value.strip().strip('"') and (
        _is_github_skill_url(value) or _looks_like_local_skill_path(value)
    )


def _looks_like_local_skill_path(value: str) -> bool:
    stripped = value.strip().strip('"')
    return bool(
        re.match(r"^[A-Za-z]:[\\/].+", stripped)
        or stripped.startswith("./")
        or stripped.startswith("../")
        or stripped.startswith("~/")
    )


def _looks_like_mcp_url(value: str, raw: str) -> bool:
    parsed = urlparse(value if "://" in value else f"https://{value}")
    if not parsed.netloc:
        return False
    lowered = f"{raw} {parsed.path}".casefold()
    return "mcp" in lowered or "sse" in lowered


def _alias_dict(data: dict[str, Any], aliases: dict[str, tuple[str, ...]]) -> dict[str, Any]:
    normalized_lookup = {_normalize_key(key): value for key, value in data.items()}
    result: dict[str, Any] = {}
    for target, candidates in aliases.items():
        for candidate in candidates:
            key = _normalize_key(candidate)
            if key in normalized_lookup:
                result[target] = normalized_lookup[key]
                break
    return result


def _normalize_key(key: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).casefold())


def _normalize_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", name.strip()).strip("-._")
    return cleaned[:80]


def _name_from_url(url: str) -> str:
    parsed = urlparse(_ensure_url_scheme(url)) if url else None
    if not parsed:
        return ""
    path_name = Path(parsed.path).name
    return path_name or parsed.netloc.split(".")[0]


def _ensure_url_scheme(url: str) -> str:
    value = url.strip()
    if not re.match(r"^https?://", value, re.IGNORECASE):
        return "https://" + value
    return value


def _find_skill_candidates(root: Path) -> list[Path]:
    if (root / "SKILL.md").is_file():
        return [root / "SKILL.md"]
    return sorted(root.rglob("SKILL.md"))


def _copy_tree(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    ignore = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".venv", "node_modules")
    shutil.copytree(source, destination, ignore=ignore)


def _repair_skill_metadata(path: Path, fallback_name: str, repairs: list[str]) -> None:
    skill_md = path / "SKILL.md"
    fallback_title = normalize_skill_name(fallback_name)
    description = _description_from_readme(path) or f"Skill instructions for {fallback_title}."
    if not skill_md.exists():
        skill_md.write_text(
            f"# {fallback_title}\n\nDescription: {description}\n\nUse this skill when relevant.",
            encoding="utf-8",
        )
        repairs.append("generated SKILL.md")
        return

    text = skill_md.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    changed = False
    if not any(line.strip().startswith("# ") for line in lines):
        lines.insert(0, f"# {fallback_title}")
        changed = True
        repairs.append("added skill title")
    if not any(line.strip().casefold().startswith("description:") for line in lines):
        insert_at = 1 if lines and lines[0].strip().startswith("# ") else 0
        lines.insert(insert_at, f"Description: {description}")
        changed = True
        repairs.append("added skill description")
    repaired_text = "\n".join(lines).strip() + "\n"
    if changed or repaired_text != text:
        skill_md.write_text(repaired_text, encoding="utf-8")
        if "normalized skill encoding" not in repairs:
            repairs.append("normalized skill encoding")


def _description_from_readme(path: Path) -> str:
    for name in ("README.md", "readme.md", "README.txt"):
        readme = path / name
        if readme.is_file():
            for line in readme.read_text(encoding="utf-8", errors="replace").splitlines():
                stripped = line.strip("# ").strip()
                if stripped:
                    return stripped[:240]
    return ""


def _clone_github_source(clone_url: str, destination: Path, ref: str = "") -> None:
    cmd = ["git", "clone", "--depth", "1"]
    if ref:
        cmd += ["--branch", ref]
    cmd += [clone_url, str(destination)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode == 0:
        return
    if ref:
        proc = subprocess.run(
            ["git", "clone", clone_url, str(destination)],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if proc.returncode == 0:
            checkout = subprocess.run(
                ["git", "-C", str(destination), "checkout", ref],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if checkout.returncode != 0:
                raise SkillInstallError(checkout.stderr.strip() or "git checkout failed")
            return
    raise SkillInstallError(proc.stderr.strip() or "git clone failed")


def _format_skill_success(
    name: str,
    path: str,
    repairs: list[str],
    detected: str = "Skill local path",
) -> str:
    lines = [
        f"Detected: {detected}",
        "Action: Installed and enabled",
        f"Name: {name}",
        f"Path: {path}",
    ]
    if repairs:
        lines.append("Repair:")
        lines.extend(f"- {item}" for item in repairs)
    lines.append("Status: Ready")
    return "\n".join(lines)
