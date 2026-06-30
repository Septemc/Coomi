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
MALFORMED_TEXT_TOOL_CALL_NAME = "InvalidToolCall"
MALFORMED_TEXT_TOOL_CALL_CORRECTION = """Malformed text tool call detected.

Coomi could not execute your previous tool call because its text format was invalid.
Regenerate exactly one complete, parseable tool call. Do not explain the error and do not
emit the tool call as ordinary prose.

Supported examples:

XML:
<tool_call>
<function=Read>
<parameter=file_path>F:\\path\\file.txt
</tool_call>

DSML:
<| | DSML | | tool_calls>
<| | DSML | | invoke name="Read">
<| | DSML | | parameter name="file_path" string="true">F:\\path\\file.txt</| | DSML | | parameter>
</| | DSML | | invoke>
</| | DSML | | tool_calls>

JSON:
{"name":"Read","arguments":{"file_path":"F:\\\\path\\\\file.txt"}}

Common required parameters:
Read: file_path
Edit: file_path, old_string, new_string
Write: file_path, content
Bash: command
PowerShell: command
Glob: pattern, path
Grep: pattern, path
WebSearch: query
WebFetch: url
TodoWrite: todos
AskUserQuestion: questions
"""

_TOOL_BLOCK_TAGS = ("tool_call", "tool_code")
_DIRECT_TOOL_TAGS = (
    "read",
    "read_file",
    "readfile",
    "glob",
    "grep",
    "bash",
    "powershell",
    "web_search",
    "websearch",
    "web_fetch",
    "webfetch",
    "write",
    "edit",
    "todo",
    "todo_write",
    "todowrite",
    "ask_user",
    "ask_user_question",
    "askuserquestion",
    "enter_plan_mode",
    "enterplanmode",
    "exit_plan_mode",
    "exitplanmode",
)
_SINGLE_TOOL_TAGS = _DIRECT_TOOL_TAGS
_MIMO_TOOL_START_TAGS = (*_TOOL_BLOCK_TAGS, *_SINGLE_TOOL_TAGS)
_KNOWN_TOOL_NAME_ALIASES = {
    "read",
    "readfile",
    "openfile",
    "cat",
    "edit",
    "editfile",
    "write",
    "writefile",
    "bash",
    "shell",
    "runcommand",
    "powershell",
    "pwsh",
    "glob",
    "listfiles",
    "findfiles",
    "grep",
    "search",
    "searchcontent",
    "searchfiles",
    "websearch",
    "searchweb",
    "webfetch",
    "fetch",
    "fetchurl",
    "todowrite",
    "todo",
    "askuserquestion",
    "askuser",
    "planmode",
    "enterplanmode",
    "exitplanmode",
}
_STRUCTURE_HINT_PATTERN = re.compile(
    r"tool_calls?|tool_code|function_call|function\s*=|invoke\b|parameter\b|"
    r"arguments\b|input\b|name\s*=|tool\s*=|<\s*\|\s*\|\s*dsml\s*\|\s*\|",
    re.IGNORECASE,
)
_FENCE_START_PATTERN = re.compile(r"```\s*(?:json|xml|tool|tools|tool_call)\b", re.IGNORECASE)
_FUNCTION_CALL_START_PATTERN = re.compile(
    r"\b([A-Za-z_][\w.]*)\s*\(",
    re.DOTALL,
)

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
_DSML_TOOL_START_TAGS = ("tool_calls", "invoke")
_DSML_TAG_PATTERN = re.compile(
    r"<\s*(/?)\s*[|\uff5c]\s*[|\uff5c]\s*DSML\s*[|\uff5c]\s*[|\uff5c]\s*"
    r"([a-zA-Z_][\w.-]*)\b([^>]*)>",
    re.IGNORECASE | re.DOTALL,
)
_ATTRIBUTE_PATTERN = re.compile(
    r"([a-zA-Z_][\w.-]*)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))",
    re.DOTALL,
)


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
                combined = self._tool_buffer + text
                end_span = _find_tool_end(combined, self._tool_tag or "")
                if end_span is None:
                    self._tool_buffer = combined
                    text = ""
                    continue

                _, end_index = end_span
                raw_call = combined[:end_index]
                parsed_calls = parse_text_tool_calls(raw_call, mode=self.mode)
                if parsed_calls:
                    tool_calls.extend(parsed_calls)
                elif is_likely_text_tool_call(raw_call, mode=self.mode):
                    tool_calls.append(_build_malformed_text_tool_call(raw_call))
                else:
                    visible_parts.append(raw_call)
                self._tool_buffer = ""
                self._in_tool_call = False
                self._tool_tag = None
                text = combined[end_index:]
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
            parsed_tool_calls = parse_text_tool_calls(self._tool_buffer, mode=self.mode)
            if parsed_tool_calls:
                for tool_call in parsed_tool_calls:
                    tool_call["parse_error"] = _format_malformed_text_tool_call_error(
                        "The text tool call was not closed with the expected closing tag."
                    )
                tool_calls.extend(parsed_tool_calls)
            elif is_likely_text_tool_call(self._tool_buffer, mode=self.mode):
                tool_calls.append(
                    _build_malformed_text_tool_call(
                        self._tool_buffer,
                        reason="The text tool call was incomplete or syntactically invalid.",
                    )
                )
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


def parse_text_tool_calls(
    raw: str,
    mode: str | None = TEXT_TOOL_MODE_STRUCTURED,
) -> list[dict[str, Any]]:
    """Parse one or more textual tool calls from a single hidden block."""
    mode = normalize_text_tool_mode(mode)
    if mode == TEXT_TOOL_MODE_DISABLED:
        return []
    raw = _strip_code_fence(raw.strip())
    if not raw:
        return []
    if _contains_dsml_tool_call(raw):
        calls = _parse_dsml_tool_calls(raw)
        return calls or [_build_malformed_text_tool_call(raw)]
    tool_call = parse_text_tool_call(raw, mode=mode)
    return [tool_call] if tool_call else []


def is_likely_text_tool_call(
    text: str | None,
    mode: str | None = TEXT_TOOL_MODE_STRUCTURED,
) -> bool:
    """Return True when text has enough structure to be treated as a tool-call attempt."""
    if not text or normalize_text_tool_mode(mode) == TEXT_TOOL_MODE_DISABLED:
        return False

    stripped = _strip_code_fence(text.strip())
    if not stripped:
        return False

    lowered = stripped.casefold()
    if _contains_dsml_tool_call(stripped):
        return True
    fence_match = _FENCE_START_PATTERN.search(text)
    if fence_match:
        newline = text.find("\n", fence_match.end())
        if newline == -1:
            return True
        fenced_body = text[newline + 1:]
        return True if "```" not in fenced_body else is_likely_text_tool_call(stripped, mode=mode)
    if re.search(r"<\s*/?\s*(tool_call|tool_code|function|parameter)\b", stripped, re.IGNORECASE):
        return True
    if re.search(r"<\s*(function|parameter)\s*=", stripped, re.IGNORECASE):
        return True
    if _looks_like_json_tool_call(stripped, allow_partial=True):
        return True
    if _looks_like_function_tool_call(stripped, allow_partial=True):
        return True

    structure_score = 1 if _STRUCTURE_HINT_PATTERN.search(stripped) else 0
    tool_score = 1 if any(alias in _normalize_name(lowered) for alias in _KNOWN_TOOL_NAME_ALIASES) else 0
    return bool(structure_score and tool_score)


def parse_text_tool_call(
    raw: str,
    mode: str | None = TEXT_TOOL_MODE_STRUCTURED,
) -> dict[str, Any] | None:
    """Parse one XML-ish, DSML-ish, or JSON-ish tool call block into provider event data."""
    mode = normalize_text_tool_mode(mode)
    if mode == TEXT_TOOL_MODE_DISABLED:
        return None

    raw = _strip_code_fence(raw.strip())
    if not raw:
        return None

    if _contains_dsml_tool_call(raw):
        calls = _parse_dsml_tool_calls(raw)
        return calls[0] if calls else _build_malformed_text_tool_call(raw)

    tag = _opening_tag_name(raw)
    if tag == "tool_code":
        return _parse_tool_code_call(raw)
    if tag in _SINGLE_TOOL_TAGS and mode == TEXT_TOOL_MODE_MIMO:
        return _parse_direct_tag_tool_call(raw, tag)
    if tag in _SINGLE_TOOL_TAGS:
        return None

    json_call = _parse_json_tool_call(raw)
    if json_call:
        return json_call
    if raw.startswith("{") and _looks_like_json_tool_call(raw, allow_partial=True):
        return _build_malformed_text_tool_call(raw, reason="The JSON text tool call is invalid.")

    function_call = _parse_function_style_tool_call(raw)
    if function_call:
        return function_call
    if _looks_like_function_tool_call(raw, allow_partial=True):
        return _build_malformed_text_tool_call(
            raw,
            reason="The function-style text tool call is incomplete or invalid.",
        )

    name = _extract_function_name(raw)
    if not name:
        return None

    arguments = _extract_parameters(raw)
    return _build_text_tool_call(name, arguments, raw)


def _contains_dsml_tool_call(raw: str) -> bool:
    for match in _DSML_TAG_PATTERN.finditer(raw):
        if match.group(1):
            continue
        tag = _normalize_dsml_tag_name(match.group(2))
        if tag in _DSML_TOOL_START_TAGS:
            return True
    return False


def _parse_dsml_tool_calls(raw: str) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    search_from = 0
    while True:
        invoke_open = _find_dsml_tag(raw, "invoke", closing=False, start=search_from)
        if invoke_open is None:
            break
        invoke_close = _find_dsml_tag(raw, "invoke", closing=True, start=invoke_open.end())
        if invoke_close is None:
            invoke_body = raw[invoke_open.end():]
            invoke_raw = raw[invoke_open.start():]
            search_from = len(raw)
        else:
            invoke_body = raw[invoke_open.end():invoke_close.start()]
            invoke_raw = raw[invoke_open.start():invoke_close.end()]
            search_from = invoke_close.end()

        attrs = _parse_attributes(invoke_open.group(3))
        name = (attrs.get("name") or attrs.get("tool") or attrs.get("function") or "").strip()
        if not name:
            continue
        arguments = _normalize_argument_names(name, _extract_dsml_parameters(invoke_body))
        calls.append(_build_text_tool_call(name, arguments, invoke_raw))

    return calls


def _extract_dsml_parameters(body: str) -> dict[str, Any]:
    arguments: dict[str, Any] = {}
    search_from = 0
    while True:
        parameter_open = _find_dsml_tag(body, "parameter", closing=False, start=search_from)
        if parameter_open is None:
            break
        parameter_close = _find_dsml_tag(
            body,
            "parameter",
            closing=True,
            start=parameter_open.end(),
        )
        if parameter_close is None:
            value_text = body[parameter_open.end():]
            search_from = len(body)
        else:
            value_text = body[parameter_open.end():parameter_close.start()]
            search_from = parameter_close.end()

        attrs = _parse_attributes(parameter_open.group(3))
        name = (attrs.get("name") or attrs.get("parameter") or "").strip()
        if not name:
            continue
        arguments[name] = _coerce_dsml_parameter_value(value_text.strip(), attrs)

    return arguments


def _find_dsml_tag(
    text: str,
    tag_name: str,
    *,
    closing: bool,
    start: int = 0,
) -> re.Match[str] | None:
    normalized_tag_name = _normalize_dsml_tag_name(tag_name)
    for match in _DSML_TAG_PATTERN.finditer(text, start):
        if bool(match.group(1)) != closing:
            continue
        if _normalize_dsml_tag_name(match.group(2)) == normalized_tag_name:
            return match
    return None


def _parse_attributes(raw_attributes: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in _ATTRIBUTE_PATTERN.finditer(raw_attributes or ""):
        value = next(group for group in match.groups()[1:] if group is not None)
        attrs[match.group(1).casefold()] = value
    return attrs


def _coerce_dsml_parameter_value(value: str, attrs: dict[str, str]) -> Any:
    if _attribute_is_true(attrs, "string"):
        return _strip_quotes_preserving_backslashes(value)
    if _attribute_is_true(attrs, "json"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value
        return parsed
    return _coerce_parameter_value(value)


def _attribute_is_true(attrs: dict[str, str], name: str) -> bool:
    return (attrs.get(name) or "").casefold() == "true"


def _normalize_dsml_tag_name(tag_name: str) -> str:
    return tag_name.casefold().replace("-", "_")


def _find_tool_end(text: str, tag: str) -> tuple[int, int] | None:
    if tag.startswith("dsml:"):
        match = _find_dsml_tag(text, tag.removeprefix("dsml:"), closing=True)
        return (match.start(), match.end()) if match else None
    if tag == "fence":
        first_line_end = text.find("\n")
        if first_line_end == -1:
            return None
        closing = text.find("```", first_line_end + 1)
        if closing == -1:
            return None
        return closing, closing + len("```")
    if tag == "json":
        end = _balanced_span_end(text, 0, "{", "}")
        return (end - 1, end) if end is not None else None
    if tag == "function_call":
        match = _FUNCTION_CALL_START_PATTERN.search(text)
        if not match:
            return None
        end = _balanced_span_end(text, match.end() - 1, "(", ")")
        if end is None:
            return None
        while end < len(text) and text[end] == ";":
            end += 1
        return end - 1, end

    lower = text.lower()
    end_tag = f"</{tag}>"
    end = lower.find(end_tag)
    if end == -1:
        return None
    return end, end + len(end_tag)


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

    argument_name = _single_tag_argument_name(tag)
    if not argument_name:
        return _build_text_tool_call(tag, {}, raw)
    return _build_text_tool_call(tag, {argument_name: _coerce_parameter_value(body)}, raw)


def _parse_direct_tag_tool_call(raw: str, tag: str) -> dict[str, Any] | None:
    body = _strip_outer_tag(raw, tag).strip()
    if not body:
        return _build_text_tool_call(tag, {}, raw)

    arguments = _extract_direct_tag_parameters(body)
    if arguments:
        return _build_text_tool_call(tag, _normalize_argument_names(tag, arguments), raw)

    argument_name = _single_tag_argument_name(tag)
    if argument_name:
        return _build_text_tool_call(tag, {argument_name: _coerce_parameter_value(body)}, raw)
    return _build_text_tool_call(tag, {}, raw)


def _extract_direct_tag_parameters(body: str) -> dict[str, Any]:
    arguments: dict[str, Any] = {}
    pattern = re.compile(
        r"<\s*([a-zA-Z_][\w.-]*)\b[^>]*>(.*?)</\s*\1\s*>",
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(body):
        name = match.group(1).strip()
        value = match.group(2).strip()
        if not name:
            continue
        arguments[name] = _coerce_parameter_value(value)
    return arguments


def _single_tag_argument_name(tag: str) -> str | None:
    return {
        "read": "file_path",
        "read_file": "file_path",
        "readfile": "file_path",
        "glob": "pattern",
        "grep": "pattern",
        "bash": "command",
        "powershell": "command",
        "web_search": "query",
        "websearch": "query",
        "web_fetch": "url",
        "webfetch": "url",
        "write": "file_path",
        "edit": "file_path",
    }.get(tag)


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
            parse_error=_format_malformed_text_tool_call_error(
                "Textual JSON tool call arguments must be an object."
            ),
        )

    return _build_text_tool_call(
        name.strip(),
        _normalize_argument_names(name.strip(), arguments),
        raw,
        call_id=str(data.get("id") or f"text_call_{uuid.uuid4().hex[:12]}"),
    )


def _parse_function_style_tool_call(raw: str) -> dict[str, Any] | None:
    parsed = _parse_function_call_text(raw)
    if parsed is None:
        return None
    name, positional, keyword = parsed
    if not _is_known_tool_name(name):
        return None
    arguments = _arguments_from_function_call(name, positional, keyword)
    return _build_text_tool_call(name, arguments, raw)


def _looks_like_json_tool_call(raw: str, allow_partial: bool = False) -> bool:
    stripped = raw.strip()
    if not stripped.startswith("{"):
        return False
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return allow_partial and bool(
            re.search(r'"(?:name|tool|function)"\s*:', stripped, re.IGNORECASE)
            or re.search(r'"(?:arguments|input|parameters)"\s*:', stripped, re.IGNORECASE)
        )
    if not isinstance(data, dict):
        return False
    name = data.get("name") or data.get("tool") or data.get("function")
    if isinstance(name, dict):
        name = name.get("name")
    has_name = isinstance(name, str) and _is_known_tool_name(name)
    has_args = any(key in data for key in ("arguments", "input", "parameters"))
    return bool(has_name and has_args)


def _looks_like_function_tool_call(raw: str, allow_partial: bool = False) -> bool:
    stripped = _strip_code_fence(raw.strip())
    match = _FUNCTION_CALL_START_PATTERN.search(stripped)
    if not match:
        return False
    name = match.group(1).split(".")[-1]
    if not _is_known_tool_name(name):
        return False
    if allow_partial:
        return True
    return _balanced_span_end(stripped, match.end() - 1, "(", ")") is not None


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


def _build_malformed_text_tool_call(raw: str, reason: str | None = None) -> dict[str, Any]:
    return _build_text_tool_call(
        MALFORMED_TEXT_TOOL_CALL_NAME,
        {},
        raw,
        parse_error=_format_malformed_text_tool_call_error(reason),
    )


def _format_malformed_text_tool_call_error(reason: str | None = None) -> str:
    if reason:
        return f"{reason}\n\n{MALFORMED_TEXT_TOOL_CALL_CORRECTION}"
    return MALFORMED_TEXT_TOOL_CALL_CORRECTION


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

    dsml_match = _find_next_dsml_tool_start(text)
    if dsml_match is not None and (best is None or dsml_match[0] < best[0]):
        best = dsml_match
    generic_match = _find_next_generic_tool_start(text)
    if generic_match is not None and (best is None or generic_match[0] < best[0]):
        best = generic_match
    return best


def _find_next_dsml_tool_start(text: str) -> tuple[int, str] | None:
    best: tuple[int, str] | None = None
    for match in _DSML_TAG_PATTERN.finditer(text):
        if match.group(1):
            continue
        tag = _normalize_dsml_tag_name(match.group(2))
        if tag not in _DSML_TOOL_START_TAGS:
            continue
        candidate = (match.start(), f"dsml:{tag}")
        if best is None or candidate[0] < best[0]:
            best = candidate
    return best


def _find_next_generic_tool_start(text: str) -> tuple[int, str] | None:
    best: tuple[int, str] | None = None
    fence = _FENCE_START_PATTERN.search(text)
    if fence and is_likely_text_tool_call(text[fence.start():], mode=TEXT_TOOL_MODE_STRUCTURED):
        best = (fence.start(), "fence")

    for match in re.finditer(r"\{", text):
        candidate = text[match.start():]
        if _looks_like_json_tool_call(candidate, allow_partial=True):
            item = (match.start(), "json")
            if best is None or item[0] < best[0]:
                best = item
            break

    for match in _FUNCTION_CALL_START_PATTERN.finditer(text):
        name = match.group(1).split(".")[-1]
        if not _is_known_tool_name(name):
            continue
        item = (match.start(), "function_call")
        if best is None or item[0] < best[0]:
            best = item
        break

    return best


def _possible_start_prefix_len(text: str, tags: tuple[str, ...]) -> int:
    if not tags:
        return 0
    lower = text.lower()
    max_len = min(max(len(f"<{tag}") for tag in tags) - 1, len(lower))
    xml_keep = 0
    for size in range(max_len, 0, -1):
        suffix = lower[-size:]
        if any(f"<{tag}".startswith(suffix) for tag in tags):
            xml_keep = size
            break
    return max(xml_keep, _possible_dsml_start_prefix_len(text), _possible_generic_start_prefix_len(text))


def _possible_dsml_start_prefix_len(text: str) -> int:
    starts = [f"<||dsml||{tag}" for tag in _DSML_TOOL_START_TAGS]
    max_len = min(80, len(text))
    for size in range(max_len, 0, -1):
        suffix = text[-size:]
        if not suffix.startswith("<"):
            continue
        compact_suffix = _compact_dsml_marker(suffix)
        if compact_suffix and any(start.startswith(compact_suffix) for start in starts):
            return size
    return 0


def _compact_dsml_marker(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold().replace("\uff5c", "|")


def _possible_generic_start_prefix_len(text: str) -> int:
    starts = ["```json", "```xml", "```tool", "{"]
    lower = text.casefold()
    max_len = min(32, len(text))
    for size in range(max_len, 0, -1):
        suffix = lower[-size:]
        compact_suffix = re.sub(r"\s+", "", suffix)
        if any(start.startswith(compact_suffix) for start in starts):
            return size
    return 0


def _balanced_span_end(text: str, open_index: int, open_char: str, close_char: str) -> int | None:
    quote: str | None = None
    escape = False
    depth = 0
    for index in range(open_index, len(text)):
        char = text[index]
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
        if char == open_char:
            depth += 1
            continue
        if char == close_char:
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _is_known_tool_name(name: str) -> bool:
    return _normalize_name(name) in _KNOWN_TOOL_NAME_ALIASES


def _normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").casefold())
