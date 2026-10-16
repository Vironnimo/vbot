"""Read JSON text that Models write: repair two unambiguous slips, explain the rest.

Models write Tool-call arguments as JSON text, and some Providers pass on an
argument value they could not decode as text. Two slips have only one reading
and are repaired here: a bare value (``"path": C:/repo`` means
``"path": "C:/repo"``) and
a ``"`` inside the text of a known field that should have been written ``\\"``.
Every other break is explained for the error the Model reads: where it is, in
which field, the text around it and how to write it.

Callers decide whether a repaired text is the call that was meant: the Provider
layer accepts only an object, Tool argument decoding only a value its schema
accepts.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection
from dataclasses import dataclass

JsonPath = tuple[str | int, ...]

# A bare value: one run without whitespace, quotes, backslashes, commas,
# braces or brackets, ending where JSON structure continues. A colon belongs to
# it only before a slash, as in a drive path (C:/repo) or a URL (https://...);
# any other colon can be the separator of a field written without braces.
_BARE_VALUE = re.compile(r"[^\s\"',{}\[\]:\\](?:[^\s\"',{}\[\]:\\]|:(?=/))*")
_BARE_VALUE_END = re.compile(r"\s*[,}\]]")
# Words that name a value rather than being text: quoting them would turn a
# missing value or a boolean into text.
_VALUE_WORDS = frozenset(
    {
        "true",
        "false",
        "null",
        "none",
        "nil",
        "undefined",
        "nan",
        "inf",
        "infinity",
        "+infinity",
        "-infinity",
    }
)
_MAX_BARE_VALUES = 32
_SHOWN_VALUE = re.compile(r"[^\s\",}\]]+")
# The shown value is the whole value: structure follows, maybe after its closing quote.
_SHOWN_VALUE_END = re.compile(r"\"?\s*[,}\]]")
_SHOWN_VALUE_CHARS = 200
# Markup of text tool-call formats that leaks into the arguments of a parsed call.
_TOOL_CALL_MARKUP = re.compile(
    r"</?(?:tool_call|function|parameter|invoke|arg_key|arg_value)\b|<function=", re.IGNORECASE
)
# Markup this close after a break stands in for the JSON that should follow.
_MARKUP_REACH = 16
_FIELD_NAME_AFTER_QUOTE = re.compile(r'"([^"\\\s]{1,64})"\s*:')
_EXCERPT_BEFORE = 28
_EXCERPT_AFTER = 14
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f]")
_TEXT_ERRORS = frozenset({"quote", "missing_quote", "escape", "control"})


def quote_bare_values(text: str) -> str | None:
    """Return ``text`` with its bare values quoted when that makes it JSON, else ``None``.

    A bare value stands where JSON expects a value, after ``:`` or as an array
    item, is one run of characters without whitespace, quotes, backslashes,
    commas, braces or brackets, and is followed by ``,``, ``}`` or ``]``:
    ``"path": .``, ``"glob": *.py`` or ``"path": C:/repo``. A colon belongs to
    it only before a slash (a drive path or a URL); ``"options": recursive:true``
    can be a field that lost its braces. Its one reading is that text. A word
    that names a value instead (``None``, ``undefined``, ``True``) is not text,
    so a call holding one stays invalid.
    """
    repaired = text
    for _ in range(_MAX_BARE_VALUES):
        try:
            json.loads(repaired)
        except json.JSONDecodeError as error:
            span = _bare_value_span(repaired, error)
            if span is None:
                return None
            start, end = span
            repaired = f'{repaired[:start]}"{repaired[start:end]}"{repaired[end:]}'
            continue
        except ValueError, RecursionError:
            return None
        return repaired if repaired != text else None
    return None


def _bare_value_span(text: str, error: json.JSONDecodeError) -> tuple[int, int] | None:
    if error.msg != "Expecting value":
        return None
    if _previous_character(text, error.pos) not in (":", "[", ","):
        return None
    match = _BARE_VALUE.match(text, error.pos)
    if match is None or match.group().casefold() in _VALUE_WORDS:
        return None
    if _BARE_VALUE_END.match(text, match.end()) is None:
        return None
    return error.pos, match.end()


def escape_inner_quotes(text: str, fields: Collection[str]) -> str | None:
    """Return ``text`` with the stray quotes inside its ``fields`` escaped, else ``None``.

    Inside the string value of one of ``fields``, a ``"`` ends the text only
    where JSON structure follows it: ``}`` or ``]``, a ``:`` before a value, a
    comma before the next string, object or array, a comma before one of
    ``fields`` that lost its opening quote, or the end of ``text``. Every other
    ``"`` there belongs to the text and is escaped: read as structure, it would
    leave text where JSON needs a value (``got "x": not a number``). ``None``
    means no quote was escaped.
    """
    names = "|".join(re.escape(name) for name in sorted(fields, key=len, reverse=True) if name)
    if not names:
        return None
    opening = re.compile(rf'"(?:{names})"\s*:\s*"')
    closing = re.compile(
        rf'"\s*(?:[}}\]]|:\s*(?:["{{\[\-\d]|(?:true|false|null)\b)|,\s*["{{\[]'
        rf'|,\s*(?:{names})"\s*:|\Z)'
    )
    parts: list[str] = []
    position = 0
    inside = False
    escaped = 0
    while position < len(text):
        if not inside:
            match = opening.match(text, position)
            if match is not None:
                parts.append(match.group())
                position = match.end()
                inside = True
            else:
                parts.append(text[position])
                position += 1
            continue
        character = text[position]
        if character == "\\" and position + 1 < len(text):
            parts.append(text[position : position + 2])
            position += 2
            continue
        if character == '"':
            if closing.match(text, position) is not None:
                parts.append(character)
                inside = False
            else:
                parts.append('\\"')
                escaped += 1
        else:
            parts.append(character)
        position += 1
    return "".join(parts) if escaped else None


@dataclass(frozen=True)
class JsonBreak:
    """Where and why a text stops being JSON, in words a Model can act on."""

    kind: str
    position: int
    path: JsonPath
    excerpt: str
    value: str = ""
    name: str = ""

    @property
    def concerns_text(self) -> bool:
        """Whether the break is inside the text of a value: a quote, escape or line break."""
        return self.kind in _TEXT_ERRORS

    def explain(self, field: str | None) -> str:
        """Return the break as lines: where and what, the text there, how to write it.

        ``field`` names the value the break is in, such as ``"path"``; ``None``
        when it is not inside one.
        """
        problem, fix = self._problem(field), self._fix()
        lines = [f"At character {self.position}, {problem}:", self.excerpt]
        if fix:
            lines.append(fix)
        return "\n".join(lines)

    def _problem(self, field: str | None) -> str:
        of_field = f" of {field}" if field else ""
        in_text = f"the text of {field}" if field else "a text"
        match self.kind:
            case "bare_value":
                return f"the value{of_field} is not in double quotes"
            case "value_word":
                return f"{self.value}, the value{of_field}, is not a JSON value"
            case "quote":
                return f'a " inside {in_text} ends it early'
            case "missing_quote":
                return f'{in_text} has no closing quote before "{self.name}"'
            case "escape":
                return f"{self.value} in {in_text} is not a JSON escape"
            case "control":
                return f"{self.value} stands unescaped in {in_text}"
            case "markup_in_text":
                return f"tool-call markup follows {in_text} instead of its closing quote"
            case "markup":
                return "tool-call markup stands where the JSON goes on"
            case "incomplete":
                return "the JSON ends before it is complete"
            case "extra":
                return "more text follows the end of the JSON"
            case "trailing_comma":
                return f"a comma stands before the closing {self.value}"
            case "plain_text":
                return "the text is not JSON"
            case _:
                return f"JSON expects {self.value} here"

    def _fix(self) -> str:
        match self.kind:
            case "bare_value" if not self.value:
                return "Write it in double quotes."
            case "bare_value":
                quoted = json.dumps(self.value, ensure_ascii=False)
                if self.path and isinstance(self.path[-1], str):
                    return f"Write {json.dumps(self.path[-1], ensure_ascii=False)}: {quoted}."
                return f"Write {quoted}."
            case "value_word":
                return "Write true, false or null, or text in double quotes."
            case "quote":
                return 'Write a " inside text as \\".'
            case "missing_quote":
                return f'End the text with " before "{self.name}".'
            case "escape":
                return "Write a backslash inside text as \\\\."
            case "control":
                return f"Write it as {self.name}."
            case "markup" | "markup_in_text":
                return "Leave the tool-call markup out of the JSON."
            case "trailing_comma":
                return "Remove that comma."
            case _:
                return ""


def json_break(text: str, error: json.JSONDecodeError) -> JsonBreak:
    """Return where and why ``text`` stops being JSON, from its decoding ``error``."""
    position = error.pos
    message = error.msg
    if message.startswith("Unterminated string"):
        markup = _TOOL_CALL_MARKUP.search(text, position)
        if markup is not None:
            return _break(text, "markup_in_text", markup.start())
        return _break(text, "incomplete", len(text))
    if position >= len(text.rstrip()):
        markup = _TOOL_CALL_MARKUP.search(text)
        if markup is not None:
            return _break(text, "markup", markup.start())
        return _break(text, "incomplete", len(text))
    markup = _TOOL_CALL_MARKUP.search(text, position, position + _MARKUP_REACH)
    if markup is not None:
        return _break(text, "markup", markup.start())
    if message == "Expecting value":
        return _value_break(text, position)
    if message.startswith("Invalid \\escape"):
        return _break(text, "escape", position, value=text[position : position + 2])
    if message.startswith("Invalid control character"):
        character = text[position]
        escape = json.dumps(character)[1:-1]  # \n, \t or \u0001
        return _break(text, "control", position, value=_control_name(character), name=escape)
    if message == "Extra data":
        return _break(text, "extra", position)
    if message.startswith("Illegal trailing comma"):
        return _break(text, "trailing_comma", position, value=text[position])
    quote = _closing_quote_before(text, position)
    if quote is not None and message != "Expecting ':' delimiter":
        name = _FIELD_NAME_AFTER_QUOTE.match(text, quote)
        if name is not None:
            return _break(text, "missing_quote", quote, name=name.group(1))
        if text[position] not in "]}":
            return _break(text, "quote", quote)
    return _break(text, "other", position, value=_expected(message, text, position))


def _value_break(text: str, position: int) -> JsonBreak:
    if not text[:position].strip():
        return _break(text, "plain_text", position)
    shown = _SHOWN_VALUE.match(text, position)
    if shown is None:
        return _break(text, "other", position, value="a value")
    value = shown.group()
    if value.casefold() in _VALUE_WORDS:
        return _break(text, "value_word", position, value=value)
    whole = _SHOWN_VALUE_END.match(text, shown.end()) is not None
    if not whole or "'" in value or len(value) > _SHOWN_VALUE_CHARS:
        # Only part of the value, too long, or in other quotes: no ready fix to copy.
        value = ""
    return _break(text, "bare_value", position, value=value)


def _break(text: str, kind: str, position: int, *, value: str = "", name: str = "") -> JsonBreak:
    return JsonBreak(
        kind=kind,
        position=position,
        path=_path_at(text, position),
        excerpt=_excerpt(text, position),
        value=value,
        name=name,
    )


def _previous_character(text: str, position: int) -> str:
    index = position - 1
    while index >= 0 and text[index] in " \t\r\n":
        index -= 1
    return text[index] if index >= 0 else ""


def _closing_quote_before(text: str, position: int) -> int | None:
    """Return the quote that closed a value string right before ``position``, if one did."""
    index = position - 1
    while index >= 0 and text[index] in " \t\r\n":
        index -= 1
    if index < 0 or text[index] != '"':
        return None
    path, in_key = _scan(text, index)
    return None if in_key or path is None else index


def _path_at(text: str, position: int) -> JsonPath:
    path, _in_key = _scan(text, position)
    return path or ()


def _scan(text: str, stop: int) -> tuple[JsonPath | None, bool]:
    """Return the containers and fields open at ``stop``, and whether it is inside a key.

    The scan reads JSON structure up to ``stop`` the way the decoder did; the
    path is ``None`` when ``stop`` is not inside a string.
    """
    stack: list[list[object]] = []  # [kind, key or index, expecting a key]
    position = 0
    while position < stop:
        character = text[position]
        if character == '"':
            end = _string_end(text, position)
            is_key = bool(stack) and stack[-1][0] == "{" and stack[-1][2] is True
            if end > stop:
                return _stack_path(stack), is_key
            if is_key:
                stack[-1][1] = _string_value(text[position:end])
                stack[-1][2] = False
            position = end
            continue
        if character in "{[":
            stack.append([character, None if character == "{" else 0, character == "{"])
        elif character in "}]":
            if stack:
                stack.pop()
        elif character == "," and stack:
            if stack[-1][0] == "{":
                stack[-1][1] = None
                stack[-1][2] = True
            elif isinstance(stack[-1][1], int):
                stack[-1][1] += 1
        position += 1
    return _stack_path(stack), False


def _stack_path(stack: list[list[object]]) -> JsonPath:
    return tuple(entry[1] for entry in stack if isinstance(entry[1], str | int))


def _string_end(text: str, start: int) -> int:
    position = start + 1
    while position < len(text):
        character = text[position]
        if character == "\\":
            position += 2
            continue
        if character == '"':
            return position + 1
        position += 1
    return len(text) + 1


def _string_value(literal: str) -> str:
    try:
        value = json.loads(literal)
    except ValueError:
        return literal.strip('"')
    return value if isinstance(value, str) else literal.strip('"')


def _excerpt(text: str, position: int) -> str:
    start = max(0, position - _EXCERPT_BEFORE)
    end = min(len(text), position + _EXCERPT_AFTER)
    shown = _CONTROL_CHARACTERS.sub(" ", text[start:end])
    return f"{'…' if start else ''}{shown}{'…' if end < len(text) else ''}"


def _control_name(character: str) -> str:
    names = {"\n": "a line break", "\r": "a carriage return", "\t": "a tab"}
    return names.get(character, f"the control character U+{ord(character):04X}")


def _expected(message: str, text: str, position: int) -> str:
    if message == "Expecting ':' delimiter":
        return ":"
    if message == "Expecting ',' delimiter":
        return ", or }" if _innermost_container(text, position) == "{" else ", or ]"
    if message.startswith("Expecting property name"):
        return "a field name in double quotes"
    if message.startswith("Illegal trailing comma"):
        return "another value after the comma"
    return message[:1].lower() + message[1:] if message else "valid JSON"


def _innermost_container(text: str, position: int) -> str:
    depth: list[str] = []
    index = 0
    while index < position:
        character = text[index]
        if character == '"':
            index = _string_end(text, index)
            continue
        if character in "{[":
            depth.append(character)
        elif character in "}]" and depth:
            depth.pop()
        index += 1
    return depth[-1] if depth else ""


def field_label(path: JsonPath) -> str | None:
    """Return ``path`` as a quoted field name such as ``"edits[0].new_string"``."""
    if not path:
        return None
    name = ""
    for part in path:
        if isinstance(part, int):
            name += f"[{part}]"
        else:
            name += f".{part}" if name else part
    return json.dumps(name, ensure_ascii=False)


def explain_json_break(
    text: str,
    error: json.JSONDecodeError,
    *,
    label: Callable[[JsonPath], str | None] = field_label,
) -> tuple[JsonBreak, str]:
    """Return the break in ``text`` and its explanation, naming fields with ``label``."""
    found = json_break(text, error)
    return found, found.explain(label(found.path))


__all__ = [
    "JsonBreak",
    "JsonPath",
    "escape_inner_quotes",
    "explain_json_break",
    "field_label",
    "json_break",
    "quote_bare_values",
]
