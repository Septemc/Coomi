"""Parse text-style tool call blocks emitted by imperfect tool-call providers."""
from __future__ import annotations

import json
import re
import uuid
from typing import Any


TEXT_TOOL_MODE_DISABLED = "disabled"
TEXT_TOOL_MODE_STRUCTURED = "structured"
TEXT_TOOL_MODE_MIMO = "mimo"
TEXT_TOOL_MODES = {
    TEXT_TOOL_MODE_DISABLED,
    TEXT_TOOL_MODE_STRUCTURED,
    TEXT_TOOL_MODE_MIMO,
}

_TOOL_BLOCK_TAGS = ("tool_call", "tool_code")
_SINGLE_TOOL_TAGS = (
    "read_file",
    "glob",
    "grep",
    "bash",
    "powershell",
    "web_search",
    "web_fetch",
)
_MIMO_TOOL_START_TAGS = (*_TOOL_BLOCK_TAGS, *_SINGLE_TOOL_TAGS)

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

    def __init__(self, mode: str | None = TEXT_TOOL_MODE_STRUCTURED):
        self.mode = normalize_text_tool_mode(mode)
        self._tool_start_tags = _tool_start_tags_for_mode(self.mode)
        self._pending = ""
        self._in_tool_call = False
        self._tool_buffer = ""
        self._tool_tag: str | None = None

    def feed(self, chunk: str) -> tuple[str, list[dict[str, Any]]]:
        if self.mode == TEXT_TOOL_MODE_DISABLED:
            return chunk, []

        text = self._pending + chunk
        self._pending = ""
        visible_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []

        while text:
            if self._in_tool_call:
                lower = text.lower()
                end_tag = f"</{self._tool_tag}>"
                end = lower.find(end_tag)
                if end == -1:
                    self._tool_buffer += text
                    text = ""
                    continue

                end_index = end + len(end_tag)
                self._tool_buffer += text[:end_index]
                tool_call = parse_text_tool_call(self._tool_buffer, mode=self.mode)
                if tool_call:
                    tool_calls.append(tool_call)
                self._tool_buffer = ""
                self._in_tool_call = False
                self._tool_tag = None
                text = text[end_index:]
                continue

            match = _find_next_tool_start(text, self._tool_start_tags)
            if match is None:
                keep = _possible_start_prefix_len(text, self._tool_start_tags)
                if keep:
                    visible_parts.append(text[:-keep])
                    self._pending = text[-keep:]
                else:
                    visible_parts.append(text)
                text = ""
                continue

            start, tag = match
            visible_parts.append(text[:start])
            self._tool_buffer = ""
            self._in_tool_call = True
            self._tool_tag = tag
            text = text[start:]

        return "".join(visible_parts), tool_calls

    def flush(self) -> tuple[str, list[dict[str, Any]]]:
        """Flush unterminated buffered text at stream end."""
        if self.mode == TEXT_TOOL_MODE_DISABLED:
            return "", []

        visible = self._pending
        self._pending = ""
        tool_calls: list[dict[str, Any]] = []

        if self._in_tool_call and self._tool_buffer:
            tool_call = parse_text_tool_call(self._tool_buffer, mode=self.mode)
            if tool_call:
                tool_call["parse_error"] = (
                    "Textual tool call was not closed with the expected closing tag. "
                    "The tool was not executed. Retry with a complete tool call."
                )
                tool_calls.append(tool_call)
            else:
                visible += self._tool_buffer
            self._tool_buffer = ""
            self._in_tool_call = False
            self._tool_tag = None

        return visible, tool_calls


def normalize_text_tool_mode(mode: str | None) -> str:
    normalized = (mode or TEXT_TOOL_MODE_STRUCTURED).strip().casefold()
    aliases = {
        "off": TEXT_TOOL_MODE_DISABLED,
        "none": TEXT_TOOL_MODE_DISABLED,
        "native": TEXT_TOOL_MODE_DISABLED,
        "no_text": TEXT_TOOL_MODE_DISABLED,
        "text": TEXT_TOOL_MODE_STRUCTURED,
        "text_fallback": TEXT_TOOL_MODE_STRUCTURED,
        "structured_only": TEXT_TOOL_MODE_STRUCTURED,
        "xml": TEXT_TOOL_MODE_STRUCTURED,
        "mimo_text": TEXT_TOOL_MODE_MIMO,
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in TEXT_TOOL_MODES:
        return TEXT_TOOL_MODE_STRUCTURED
    return normalized


def parse_text_tool_call(
    raw: str,
    mode: str | None = TEXT_TOOL_MODE_STRUCTURED,
) -> dict[str, Any] | None:
    """Parse one XML-ish or JSON-ish tool call block into provider event data."""
    mode = normalize_text_tool_mode(mode)
    if mode == TEXT_TOOL_MODE_DISABLED:
        return None

    raw = raw.strip()
    if not raw:
        return None

    tag = _opening_tag_name(raw)
    if tag == "tool_code":
        return _parse_tool_code_call(raw)
    if tag in _SINGLE_TOOL_TAGS and mode == TEXT_TOOL_MODE_MIMO:
        return _parse_single_tag_tool_call(raw, tag)
    if tag in _SINGLE_TOOL_TAGS:
        return None

    json_call = _parse_json_tool_call(raw)
    if json_call:
        return json_call

    name = _extract_function_name(raw)
    if not name:
        return None

    arguments = _extract_parameters(raw)
    return _build_text_tool_call(name, arguments, raw)


def _parse_tool_code_call(raw: str) -> dict[str, Any] | None:
    body = _strip_outer_tag(raw, "tool_code").strip()
    if not body:
        return None

    parsed = _parse_function_call_text(body)
    if parsed is None:
        return _build_text_tool_call(
            "InvalidToolCall",
            {},
            raw,
            parse_error=(
                "Could not parse <tool_code> as a supported function call. "
                "Use a real tool call or <tool_call><function=...><parameter=...>...</tool_call>."
            ),
        )

    name, positional, keyword = parsed
    arguments = _arguments_from_function_call(name, positional, keyword)
    return _build_text_tool_call(name, arguments, raw)


def _parse_single_tag_tool_call(raw: str, tag: str) -> dict[str, Any] | None:
    body = _strip_outer_tag(raw, tag).strip()
    if not body:
        return None

    argument_name = {
        "read_file": "file_path",
        "glob": "pattern",
        "grep": "pattern",
        "bash": "command",
        "powershell": "command",
        "web_search": "query",
        "web_fetch": "url",
    }[tag]
    return _build_text_tool_call(tag, {argument_name: _coerce_parameter_value(body)}, raw)


def strip_text_tool_calls(
    text: str | None,
    mode: str | None = TEXT_TOOL_MODE_STRUCTURED,
) -> tuple[str | None, list[dict[str, Any]]]:
    """Parse all complete text tool calls from a non-streaming response."""
    if not text:
        return text, []
    if normalize_text_tool_mode(mode) == TEXT_TOOL_MODE_DISABLED:
        return text, []
    parser = TextToolCallFilter(mode=mode)
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
        return _build_text_tool_call(
            name.strip(),
            {},
            raw,
            call_id=str(data.get("id") or f"text_call_{uuid.uuid4().hex[:12]}"),
            parse_error="Textual JSON tool call arguments must be an object.",
        )

    return _build_text_tool_call(
        name.strip(),
        _normalize_argument_names(name.strip(), arguments),
        raw,
        call_id=str(data.get("id") or f"text_call_{uuid.uuid4().hex[:12]}"),
    )


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
    tool_name = _extract_function_name(raw) or ""
    return _normalize_argument_names(tool_name, arguments)


def _parse_function_call_text(text: str) -> tuple[str, list[Any], dict[str, Any]] | None:
    text = _strip_code_fence(text.strip())
    if not text:
        return None

    match = re.match(r"^\s*([a-zA-Z_][\w.]*)\s*\((.*)\)\s*;?\s*$", text, re.DOTALL)
    if match:
        name = match.group(1).split(".")[-1]
        return name, *_parse_call_arguments(match.group(2))

    return None


def _parse_call_arguments(arguments_text: str) -> tuple[list[Any], dict[str, Any]]:
    positional: list[Any] = []
    keyword: dict[str, Any] = {}
    for item in _split_top_level(arguments_text):
        item = item.strip()
        if not item:
            continue
        key, value = _split_keyword_argument(item)
        if key:
            keyword[key] = _coerce_parameter_value(value.strip())
        else:
            positional.append(_coerce_parameter_value(item))
    return positional, keyword


def _split_top_level(text: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escape = False
    depth = 0

    for char in text:
        if quote:
            current.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote:
                quote = None
            continue

        if char in {'"', "'"}:
            quote = char
            current.append(char)
            continue
        if char in "([{":
            depth += 1
            current.append(char)
            continue
        if char in ")]}":
            depth = max(0, depth - 1)
            current.append(char)
            continue
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)

    if current:
        parts.append("".join(current))
    return parts


def _split_keyword_argument(text: str) -> tuple[str | None, str]:
    quote: str | None = None
    escape = False
    depth = 0
    for index, char in enumerate(text):
        if quote:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
            continue
        if char in "([{":
            depth += 1
            continue
        if char in ")]}":
            depth = max(0, depth - 1)
            continue
        if char == "=" and depth == 0:
            key = text[:index].strip()
            if re.match(r"^[a-zA-Z_][\w.-]*$", key):
                return key, text[index + 1:]
            return None, text
    return None, text


def _arguments_from_function_call(
    tool_name: str,
    positional: list[Any],
    keyword: dict[str, Any],
) -> dict[str, Any]:
    normalized_name = _normalize_name(tool_name)
    positional_keys = {
        "glob": ["pattern", "path"],
        "listfiles": ["pattern", "path"],
        "findfiles": ["pattern", "path"],
        "read": ["file_path"],
        "readfile": ["file_path"],
        "openfile": ["file_path"],
        "grep": ["pattern", "path"],
        "searchcontent": ["pattern", "path"],
        "bash": ["command"],
        "shell": ["command"],
        "runcommand": ["command"],
        "powershell": ["command"],
        "pwsh": ["command"],
        "websearch": ["query"],
        "searchweb": ["query"],
        "webfetch": ["url", "prompt"],
        "fetchurl": ["url", "prompt"],
    }.get(normalized_name, [])

    arguments = dict(keyword)
    for index, value in enumerate(positional):
        if index < len(positional_keys) and positional_keys[index] not in arguments:
            arguments[positional_keys[index]] = value
        else:
            arguments[f"arg{index + 1}"] = value
    return _normalize_argument_names(tool_name, arguments)


def _normalize_argument_names(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    normalized_name = _normalize_name(tool_name)
    normalized: dict[str, Any] = {}
    for key, value in arguments.items():
        normalized_key = _normalize_name(str(key))
        target_key = key
        if normalized_name in {"read", "readfile", "openfile"}:
            if normalized_key in {"path", "file", "filepath", "filename"}:
                target_key = "file_path"
        elif normalized_name in {"glob", "listfiles", "findfiles"}:
            if normalized_key in {"directory", "dir", "root", "cwd"}:
                target_key = "path"
            elif normalized_key in {"glob", "query"}:
                target_key = "pattern"
        elif normalized_name in {"grep", "searchcontent"}:
            if normalized_key in {"regex", "query", "text"}:
                target_key = "pattern"
            elif normalized_key in {"directory", "dir", "root", "cwd"}:
                target_key = "path"
        elif normalized_name in {"bash", "shell", "runcommand", "powershell", "pwsh"}:
            if normalized_key in {"cmd", "commandline", "script"}:
                target_key = "command"
        elif normalized_name in {"websearch", "searchweb"}:
            if normalized_key in {"q", "text"}:
                target_key = "query"
        elif normalized_name in {"webfetch", "fetchurl"}:
            if normalized_key in {"href", "link"}:
                target_key = "url"
        normalized[target_key] = value
    return normalized


def _strip_code_fence(text: str) -> str:
    match = re.match(r"^```(?:\w+)?\s*(.*?)\s*```$", text, flags=re.DOTALL)
    if match:
        return match.group(1).strip()
    return text


def _strip_outer_tag(raw: str, tag: str) -> str:
    body = re.sub(rf"^<\s*{re.escape(tag)}\b[^>]*>", "", raw, flags=re.IGNORECASE).strip()
    body = re.sub(rf"</\s*{re.escape(tag)}\s*>$", "", body, flags=re.IGNORECASE).strip()
    return body


def _opening_tag_name(raw: str) -> str | None:
    match = re.match(r"^<\s*([a-zA-Z_][\w.-]*)\b", raw.strip())
    if not match:
        return None
    return match.group(1).casefold().replace("-", "_")


def _coerce_parameter_value(value: str) -> Any:
    if not value:
        return ""
    value = _strip_quotes_preserving_backslashes(value)
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


def _strip_quotes_preserving_backslashes(value: str) -> str:
    value = value.strip()
    if len(value) < 2:
        return value
    quote = value[0]
    if quote not in {'"', "'"} or value[-1] != quote:
        return value
    inner = value[1:-1]
    return inner.replace(f"\\{quote}", quote).replace("\\\\", "\\")


def _build_text_tool_call(
    name: str,
    arguments: dict[str, Any],
    raw: str,
    call_id: str | None = None,
    parse_error: str | None = None,
) -> dict[str, Any]:
    return {
        "id": call_id or f"text_call_{uuid.uuid4().hex[:12]}",
        "name": name,
        "arguments": arguments,
        "raw_arguments": raw,
        "parse_error": parse_error,
        "source": "text_fallback",
    }


def _tool_start_tags_for_mode(mode: str) -> tuple[str, ...]:
    if mode == TEXT_TOOL_MODE_DISABLED:
        return ()
    if mode == TEXT_TOOL_MODE_MIMO:
        return _MIMO_TOOL_START_TAGS
    return _TOOL_BLOCK_TAGS


def _find_next_tool_start(text: str, tags: tuple[str, ...]) -> tuple[int, str] | None:
    if not tags:
        return None
    lower = text.lower()
    best: tuple[int, str] | None = None
    for tag in tags:
        token = f"<{tag}"
        start = lower.find(token)
        if start == -1:
            continue
        after_index = start + len(token)
        if after_index < len(lower):
            after = lower[after_index]
            if after not in {" ", "\t", "\r", "\n", ">"}:
                continue
        if best is None or start < best[0]:
            best = (start, tag)
    return best


def _possible_start_prefix_len(text: str, tags: tuple[str, ...]) -> int:
    if not tags:
        return 0
    lower = text.lower()
    max_len = min(max(len(f"<{tag}") for tag in tags) - 1, len(lower))
    for size in range(max_len, 0, -1):
        suffix = lower[-size:]
        if any(f"<{tag}".startswith(suffix) for tag in tags):
            return size
    return 0


def _normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").casefold())
