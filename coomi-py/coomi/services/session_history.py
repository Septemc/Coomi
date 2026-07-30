"""JSONL-backed session history."""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..types import Message, Session, TokenUsage, ToolCall


SESSION_FILENAME_PREFIX = "coomi"


@dataclass(frozen=True)
class SessionHistoryRecord:
    """Small summary used by the welcome screen history list."""

    path: Path
    session_id: str
    title: str
    created_at: datetime | None
    updated_at: datetime | None
    message_count: int
    model: str = ""
    cwd: str = ""


def default_sessions_dir() -> Path:
    return Path.home() / ".coomi" / "sessions"


def build_session_filename(
    created_at: datetime | None = None,
    session_uuid: str | None = None,
) -> str:
    created_at = created_at or datetime.now()
    session_uuid = session_uuid or str(uuid.uuid4())
    timestamp = created_at.strftime("%Y-%m-%dT%H-%M-%S")
    return f"{SESSION_FILENAME_PREFIX}-{timestamp}-{session_uuid}.jsonl"


def create_session_file(
    session: Session,
    history_dir: Path | None = None,
    cwd: str | None = None,
    model: str = "",
) -> Path:
    directory = history_dir or default_sessions_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / build_session_filename(session.created_at, session.id)
    session.history_path = str(path)
    _append_jsonl(
        path,
        {
            "type": "session",
            "id": session.id,
            "created_at": session.created_at.isoformat(),
            "updated_at": session.created_at.isoformat(),
            "system_prompt": session.system_prompt,
            "cwd": cwd or os.getcwd(),
            "model": model,
        },
    )
    return path


def append_message(session: Session, message: Message) -> None:
    if not session.history_path:
        return
    _append_jsonl(
        Path(session.history_path),
        {
            "type": "message",
            "session_id": session.id,
            "created_at": message.created_at.isoformat(),
            "message": _message_to_json(message),
        },
    )


def append_session_state(session: Session) -> None:
    """Persist extension activation state without rewriting history."""
    if not session.history_path:
        return
    _append_jsonl(
        Path(session.history_path),
        {
            "type": "state",
            "session_id": session.id,
            "created_at": datetime.now().isoformat(),
            "active_skills": session.active_skills,
            "selected_mcps": session.selected_mcps,
        },
    )


def list_session_records(
    history_dir: Path | None = None,
    limit: int = 20,
    include_empty: bool = False,
) -> list[SessionHistoryRecord]:
    directory = history_dir or default_sessions_dir()
    if not directory.exists():
        return []

    records: list[SessionHistoryRecord] = []
    for path in directory.glob(f"{SESSION_FILENAME_PREFIX}-*.jsonl"):
        record = _read_record(path)
        if record is None:
            continue
        if not include_empty and record.message_count == 0:
            continue
        records.append(record)

    records.sort(key=lambda item: item.updated_at or item.created_at or datetime.min, reverse=True)
    return records[:limit]


def delete_session_record(path: str | Path) -> bool:
    """Delete one persisted Coomi session selected by the user."""
    source = Path(path)
    if source.suffix.casefold() != ".jsonl":
        return False
    if not source.name.startswith(f"{SESSION_FILENAME_PREFIX}-"):
        return False
    try:
        source.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True


def load_session_from_jsonl(path: str | Path) -> Session:
    source = Path(path)
    metadata: dict[str, Any] = {}
    messages: list[Message] = []

    with source.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") == "session":
                metadata.update(entry)
            elif entry.get("type") == "message":
                msg_data = entry.get("message") or {}
                messages.append(_message_from_json(msg_data))
            elif entry.get("type") == "state":
                metadata["active_skills"] = entry.get("active_skills") or []
                metadata["selected_mcps"] = entry.get("selected_mcps") or []

    created_at = _parse_dt(metadata.get("created_at")) or datetime.now()
    session = Session(
        id=metadata.get("id") or source.stem,
        system_prompt=metadata.get("system_prompt") or "You are Coomi Agent.",
        messages=messages,
        created_at=created_at,
        current_model=metadata.get("model") or None,
        history_path=str(source),
        active_skills=list(metadata.get("active_skills") or []),
        selected_mcps=list(metadata.get("selected_mcps") or []),
    )
    session.token_usage = TokenUsage()
    return session


def _read_record(path: Path) -> SessionHistoryRecord | None:
    metadata: dict[str, Any] = {}
    first_user = ""
    message_count = 0
    updated_at: datetime | None = None

    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if entry.get("type") == "session":
                    metadata.update(entry)
                    updated_at = _parse_dt(entry.get("updated_at")) or updated_at
                    continue

                if entry.get("type") != "message":
                    continue

                message_count += 1
                updated_at = _parse_dt(entry.get("created_at")) or updated_at
                message = entry.get("message") or {}
                if not first_user and message.get("role") == "user":
                    first_user = _clean_title(message.get("content") or "")
    except OSError:
        return None

    created_at = _parse_dt(metadata.get("created_at")) or _created_from_filename(path)
    title = first_user or "New session"
    return SessionHistoryRecord(
        path=path,
        session_id=metadata.get("id") or path.stem,
        title=title,
        created_at=created_at,
        updated_at=updated_at or created_at,
        message_count=message_count,
        model=metadata.get("model") or "",
        cwd=metadata.get("cwd") or "",
    )


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        handle.write("\n")


def _message_to_json(message: Message) -> dict[str, Any]:
    return {
        "role": message.role,
        "content": message.content,
        "tool_call_id": message.tool_call_id,
        "reasoning_content": message.reasoning_content,
        "tool_calls": [
            {
                "id": tool_call.id,
                "name": tool_call.name,
                "arguments": tool_call.arguments,
                "raw_arguments": tool_call.raw_arguments,
                "parse_error": tool_call.parse_error,
                "source": tool_call.source,
            }
            for tool_call in (message.tool_calls or [])
        ],
        "created_at": message.created_at.isoformat(),
    }


def _message_from_json(data: dict[str, Any]) -> Message:
    tool_calls = [
        ToolCall(
            id=item.get("id") or "",
            name=item.get("name") or "",
            arguments=item.get("arguments") or {},
            raw_arguments=item.get("raw_arguments"),
            parse_error=item.get("parse_error"),
            source=item.get("source") or "native",
        )
        for item in data.get("tool_calls") or []
    ]
    return Message(
        role=data.get("role", "user"),
        content=data.get("content"),
        tool_call_id=data.get("tool_call_id"),
        reasoning_content=data.get("reasoning_content"),
        tool_calls=tool_calls or None,
        created_at=_parse_dt(data.get("created_at")) or datetime.now(),
    )


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _created_from_filename(path: Path) -> datetime | None:
    parts = path.stem.split("-")
    if len(parts) < 7 or parts[0] != SESSION_FILENAME_PREFIX:
        return None
    timestamp = "-".join(parts[1:7])
    try:
        return datetime.strptime(timestamp, "%Y-%m-%dT%H-%M-%S")
    except ValueError:
        return None


def _clean_title(value: str, max_len: int = 44) -> str:
    title = " ".join(value.split())
    if len(title) <= max_len:
        return title
    return title[: max_len - 3] + "..."
