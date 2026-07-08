"""PyPI update checks for the TUI."""
from __future__ import annotations

import re
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version

import httpx


PACKAGE_NAME = "coomi-agent"
PYPI_JSON_URL = f"https://pypi.org/pypi/{PACKAGE_NAME}/json"


@dataclass(frozen=True)
class UpdateCheckResult:
    current_version: str
    latest_version: str | None
    update_available: bool = False
    error: str | None = None


def get_current_version() -> str:
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        from coomi import __version__

        return __version__


def fetch_latest_version(timeout: float = 2.0) -> str:
    response = httpx.get(PYPI_JSON_URL, timeout=timeout, follow_redirects=True)
    response.raise_for_status()
    data = response.json()
    latest = data.get("info", {}).get("version")
    if not isinstance(latest, str) or not latest.strip():
        raise ValueError("PyPI response did not include info.version")
    return latest.strip()


def check_for_update(timeout: float = 2.0) -> UpdateCheckResult:
    current = get_current_version()
    try:
        latest = fetch_latest_version(timeout=timeout)
    except Exception as exc:
        return UpdateCheckResult(current, None, error=f"{type(exc).__name__}: {exc}")
    return UpdateCheckResult(
        current_version=current,
        latest_version=latest,
        update_available=is_newer_version(latest, current),
    )


def build_update_prompt_suffix(result: UpdateCheckResult) -> str | None:
    if not result.update_available or not result.latest_version:
        return None
    return (
        f"当前使用的是{result.current_version}，"
        f"建议通过“pip install -U {PACKAGE_NAME}”更新到{result.latest_version}"
    )


def is_newer_version(candidate: str, current: str) -> bool:
    try:
        from packaging.version import InvalidVersion, Version

        try:
            return Version(candidate) > Version(current)
        except InvalidVersion:
            pass
    except ImportError:
        pass

    return _numeric_version_key(candidate) > _numeric_version_key(current)


def _numeric_version_key(raw: str) -> tuple[int, ...]:
    parts = [int(part) for part in re.findall(r"\d+", raw)]
    return tuple(parts) if parts else (0,)
