"""Parse text-style tool call blocks emitted by imperfect tool-call providers."""
from __future__ import annotations

import json
import re
import uuid
from typing import Any


_START_TAG = "<tool_call"
_END_TAG = "</tool_call>"
_END_TAG_LEN = len(_END_TAG)

_FUNCTION_PATTERNS = (
    re.compile(r"<\s*function\s*=\s*['\"]?([^'\">\s]+)['\"]?\s*>", re.IGNORECASE),
    re.compile(r"<\s*function\b[^>]*\bname\s*=\s*['\"]([^'\"]+)['\"][^>]*>", re.IGNORECASE),
)
_PARAMETER_PATTERN = re.compile(
    r"<\s*parameter\s*=\s*['\"]?([a-zA-Z_][\w.-]*)['\"]?\s*>",
    re.IGNORECASE,
)
_NAMED_PARAMETER_PATTERN = re.compile(
    r"<\s*parameter\b[^>]*\bname\s*=\s*['\"]([^'\"]+)['\"][^>]*>",
    re.IGNORECASE,
)
_CLOSING_PARAMETER_PATTERN = re.compile(r"</\s*parameter\s*>", re.IGNORECASE)


class TextToolCallFilter:
    """Remove textual tool-call blocks from visible text and return parsed calls."""

    def __init__(self):
        self._pending = ""
        self._in_tool_call = False
        self._tool_buffer = ""

    def feed(self, chunk: str) -> tuple[str, list[dict[str, Any]]]:
        text = self._pending + chunk
        self._pending = ""
        visible_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []

        while text:
            if self._in_tool_call:
                lower = text.lower()
                end = lower.find(_END_TAG)
                if end == -1:
                    self._tool_buffer += text
                    text = ""
                    continue

                end_index = end + _END_TAG_LEN
                self._tool_buffer += text[:end_index]
                tool_call = parse_text_tool_call(self._tool_buffer)
                if tool_call:
                    tool_calls.append(tool_call)
                self._tool_buffer = ""
                self._in_tool_call = False
                text = text[end_index:]
                continue

            lower = text.lower()
            start = lower.find(_START_TAG)
            if start == -1:
                keep = _possible_start_prefix_len(text)
                if keep:
                    visible_parts.append(text[:-keep])
                    self._pending = text[-keep:]
                else:
                    visible_parts.append(text)
                text = ""
                continue

            visible_parts.append(text[:start])
            self._tool_buffer = ""
            self._in_tool_call = True
            text = text[start:]

        return "".join(visible_parts), tool_calls

    def flush(self) -> tuple[str, list[dict[str, Any]]]:
        """Flush unterminated buffered text at stream end."""
        visible = self._pending
        self._pending = ""
        tool_calls: list[dict[str, Any]] = []

        if self._in_tool_call and self._tool_buffer:
            tool_call = parse_text_tool_call(self._tool_buffer)
            if tool_call:
                tool_call["parse_error"] = (
                    "Textual tool call was not closed with </tool_call>. "
                    "The tool was not executed. Retry with a complete tool call."
                )
                tool_calls.append(tool_call)
            else:
                visible += self._tool_buffer
            self._tool_buffer = ""
            self._in_tool_call = False

        return visible, tool_calls


def parse_text_tool_call(raw: str) -> dict[str, Any] | None:
    """Parse one XML-ish or JSON-ish tool call block into provider event data."""
    raw = raw.strip()
    if not raw:
        return None

    json_call = _parse_json_tool_call(raw)
    if json_call:
        return json_call

    name = _extract_function_name(raw)
    if not name:
        return None

    arguments = _extract_parameters(raw)
    return {
        "id": f"text_call_{uuid.uuid4().hex[:12]}",
        "name": name,
        "arguments": arguments,
        "raw_arguments": raw,
        "parse_error": None,
    }


def strip_text_tool_calls(text: str | None) -> tuple[str | None, list[dict[str, Any]]]:
    """Parse all complete text tool calls from a non-streaming response."""
    if not text:
        return text, []
    parser = TextToolCallFilter()
    visible, calls = parser.feed(text)
    tail, tail_calls = parser.flush()
    visible += tail
    calls.extend(tail_calls)
    visible = visible.strip()
    return visible or None, calls


def _parse_json_tool_call(raw: str) -> dict[str, Any] | None:
    body = re.sub(r"^<\s*tool_call[^>]*>", "", raw, flags=re.IGNORECASE).strip()
    body = re.sub(r"</\s*tool_call\s*>$", "", body, flags=re.IGNORECASE).strip()
    if not body.startswith("{"):
        return None

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None

    name = data.get("name") or data.get("tool") or data.get("function")
    if isinstance(name, dict):
        name = name.get("name")
    if not isinstance(name, str) or not name.strip():
        return None

    arguments = data.get("arguments") or data.get("input") or data.get("parameters") or {}
    if not isinstance(arguments, dict):
        return {
            "id": str(data.get("id") or f"text_call_{uuid.uuid4().hex[:12]}"),
            "name": name.strip(),
            "arguments": {},
            "raw_arguments": raw,
            "parse_error": "Textual JSON tool call arguments must be an object.",
        }

    return {
        "id": str(data.get("id") or f"text_call_{uuid.uuid4().hex[:12]}"),
        "name": name.strip(),
        "arguments": arguments,
        "raw_arguments": raw,
        "parse_error": None,
    }


def _extract_function_name(raw: str) -> str | None:
    for pattern in _FUNCTION_PATTERNS:
        match = pattern.search(raw)
        if match:
            name = match.group(1).strip()
            if name:
                return name
    return None


def _extract_parameters(raw: str) -> dict[str, Any]:
    matches = list(_PARAMETER_PATTERN.finditer(raw)) + list(_NAMED_PARAMETER_PATTERN.finditer(raw))
    matches.sort(key=lambda item: item.start())
    arguments: dict[str, Any] = {}
    for index, match in enumerate(matches):
        name = match.group(1).strip()
        value_start = match.end()
        value_end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        value = raw[value_start:value_end]
        value = re.sub(r"</\s*tool_call\s*>.*$", "", value, flags=re.IGNORECASE | re.DOTALL)
        value = _CLOSING_PARAMETER_PATTERN.sub("", value)
        arguments[name] = _coerce_parameter_value(value.strip())
    return arguments


def _coerce_parameter_value(value: str) -> Any:
    if not value:
        return ""
    lowered = value.casefold()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null":
        return None
    if (value.startswith("[") and value.endswith("]")) or (value.startswith("{") and value.endswith("}")):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _possible_start_prefix_len(text: str) -> int:
    lower = text.lower()
    max_len = min(len(_START_TAG) - 1, len(lower))
    for size in range(max_len, 0, -1):
        if _START_TAG.startswith(lower[-size:]):
            return size
    return 0
