"""Keep decorators on their definition when an edit adds lines before it.

An edit that starts at a definition, such as ``def run(self):``, and adds lines
before it writes them between the definition and the decorators, annotations or
attributes directly above, which the edit did not cover. The decorators then
apply to the added code, or the file no longer parses: an Agent that copies the
definition's line without them never sees what the lines above it hold. When
the added lines hold code and the definition follows them, they go above those
decorators instead, and a note says so. Decorators the added lines end with stay
directly on the definition, as written. Lines that only add decorators, comments
or blank lines stay where the edit put them.

Only file types whose decorator syntax is known take part. The definition the
edit places its lines before is found by its name, so a changed signature still
counts as the same definition; a renamed one, or a name the replacement holds
twice at that indentation, leaves the edit as written.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePath

from core.tools.arguments import TEXT_LINE_BREAK

type Spans = Sequence[tuple[tuple[int, int], tuple[int, int]]]


@dataclass(frozen=True)
class _Syntax:
    """How a file type writes decorators (``markers``) and line comments."""

    markers: tuple[str, ...]
    comments: tuple[str, ...]


_AT = _Syntax(("@",), ("//", "/*"))
_SYNTAX = {
    **dict.fromkeys((".py", ".pyi"), _Syntax(("@",), ("#",))),
    **dict.fromkeys(
        (
            ".ts",
            ".tsx",
            ".mts",
            ".cts",
            ".js",
            ".jsx",
            ".mjs",
            ".cjs",
            ".java",
            ".kt",
            ".kts",
            ".scala",
            ".groovy",
            ".dart",
            ".swift",
        ),
        _AT,
    ),
    ".rs": _Syntax(("#[",), ("//",)),
    ".php": _Syntax(("#[",), ("//", "/*", "#")),
    ".cs": _Syntax(("[",), ("//", "/*")),
}
# Lines that start like a decorator but declare or annotate something else.
_NOT_DECORATORS = ("@interface", "@file:", "#![", "[assembly:", "[module:")
_OPENING = "([{"
_CLOSING = ")]}"
_QUOTES = "\"'`"
_GENERIC = re.compile(r"<[^<>()]*>")
_NAME = re.compile(r"[A-Za-z_$][\w$]*")
_NAME_END = re.compile(r"[(:{=;]")
_EXCERPT_CHARACTERS = 60

_NOTE = "The added lines went above {decorators} ({excerpt}), not between {them} and {owner}."
_OWNER = (
    "the definition at line {line} it belongs to",
    "the definition at line {line} they belong to",
)


def place_before_decorators(
    before: str, after: str, spans: Spans, path: object
) -> tuple[str, list[str]]:
    """Move lines an edit added between a definition and its decorators above them.

    ``spans`` pairs each replaced region of ``before`` with the text that
    replaced it in ``after``, in file order. Returns the text and a note per move.
    """
    syntax = _SYNTAX.get(PurePath(str(path)).suffix.lower())
    if syntax is None:
        return after, []
    notes: list[str] = []
    for position in reversed(range(len(spans))):
        old, new = spans[position]
        floor = 0
        if position:
            end = spans[position - 1][1][1]
            floor = len(TEXT_LINE_BREAK.findall(after, 0, end))
            floor += bool(end and after[end - 1] not in "\r\n")
        moved = _place(syntax, before, after, old, new, floor)
        if moved is not None:
            after, note = moved
            notes.insert(0, note)
    return after, notes


def _place(
    syntax: _Syntax,
    before: str,
    after: str,
    old: tuple[int, int],
    new: tuple[int, int],
    floor: int,
) -> tuple[str, str] | None:
    """Move the lines one replacement added above the decorators of its definition.

    ``floor`` is the first line of ``after`` past the replacement before this one.
    """
    if old[1] <= old[0] or new[1] <= new[0]:
        return None
    old_texts = [text for text, _ in _lines(before)]
    old_first = len(TEXT_LINE_BREAK.findall(before, 0, old[0]))
    # The region starts a line, or follows only its indentation.
    if _line_head(before, old[0]).strip():
        return None
    indent = _indent(old_texts[old_first])
    old_last = len(TEXT_LINE_BREAK.findall(before, 0, old[1] - 1))
    definition = _skip_decorators(syntax, old_texts, old_first, old_last, indent)
    name = None if definition is None else _name(old_texts[definition])
    if name is None:
        return None
    lines = _lines(after)
    texts = [text for text, _ in lines]
    first = len(TEXT_LINE_BREAK.findall(after, 0, new[0]))
    last = len(TEXT_LINE_BREAK.findall(after, 0, new[1] - 1))
    top = _decorators_above(syntax, texts, first, floor, indent)
    if top == first:
        return None
    same = [
        index
        for index in range(first, last + 1)
        if _indent(texts[index]) == indent and _name(texts[index]) == name
    ]
    if len(same) != 1:
        return None
    target = same[0]
    kept_on = _decorators_above(syntax, texts, target, first, indent)
    if not all(_within(text, indent) for text in texts[first:target]) or _decoration_only(
        syntax, texts[first:kept_on], indent
    ):
        return None
    moved = lines[:top] + lines[first:kept_on] + lines[top:first] + lines[kept_on:]
    count, above = kept_on - first, first - top
    excerpt = texts[top].strip()
    if len(excerpt) > _EXCERPT_CHARACTERS:
        excerpt = excerpt[: _EXCERPT_CHARACTERS - 3] + "..."
    several = above > 1
    note = _NOTE.format(
        decorators=_span("line", top + count + 1, top + count + above),
        excerpt=excerpt + " ..." if several else excerpt,
        them="them" if several else "it",
        owner=_OWNER[several].format(line=target + 1),
    )
    return "".join(text + ending for text, ending in moved), note


def _span(noun: str, first: int, last: int) -> str:
    return f"{noun} {first}" if first == last else f"{noun}s {first}-{last}"


def _line_head(text: str, position: int) -> str:
    """Return the part of the line holding ``position`` that comes before it."""
    start = max(text.rfind("\n", 0, position), text.rfind("\r", 0, position)) + 1
    return text[start:position]


def _decoration_only(syntax: _Syntax, texts: Sequence[str], indent: str) -> bool:
    """Whether lines hold only decorators, comments and blank lines at ``indent``."""
    index = 0
    while index < len(texts):
        text = texts[index]
        index += 1
        if not text.strip() or (_indent(text) == indent and _comment(syntax, text)):
            continue
        if _indent(text) != indent or not _decorator(syntax, text):
            return False
        depth = _depth(syntax, text)
        while depth > 0 and index < len(texts):
            depth += _depth(syntax, texts[index])
            index += 1
        if depth != 0:
            return False
    return True


def _skip_decorators(
    syntax: _Syntax, texts: Sequence[str], start: int, last: int, indent: str
) -> int | None:
    """Return the first line from ``start`` to ``last`` past its decorators at ``indent``."""
    index = start
    while index <= last:
        text = texts[index]
        if _indent(text) != indent or not text.strip():
            return None
        if _comment(syntax, text):
            index += 1
            continue
        if not _decorator(syntax, text):
            return index
        depth = _depth(syntax, text)
        while depth > 0:
            index += 1
            if index > last or not _within(texts[index], indent):
                return None
            depth += _depth(syntax, texts[index])
        if depth < 0:
            return None
        index += 1
    return None


def _decorators_above(
    syntax: _Syntax, texts: Sequence[str], line: int, floor: int, indent: str
) -> int:
    """Return where the decorators directly above ``line`` start, ``line`` if none.

    Comments between decorators belong to them; comments above the topmost one
    do not. Lines from ``floor`` on are searched.
    """
    top = line
    pending: list[str] = []
    index = line - 1
    while index >= floor:
        text = texts[index]
        if not text.strip() or not _within(text, indent):
            break
        level = _indent(text) == indent
        if level and _decorator(syntax, text):
            depth = 0
            balanced = True
            for part in [text, *reversed(pending)]:
                depth += _depth(syntax, part)
                if depth < 0:
                    balanced = False
            if not balanced or depth != 0:
                break
            top, pending = index, []
        elif level and _comment(syntax, text):
            if pending:
                break
        elif level and not text.strip().startswith(tuple(_CLOSING)):
            break
        else:
            pending.append(text)
        index -= 1
    return top


def _decorator(syntax: _Syntax, text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith(syntax.markers) and not stripped.startswith(_NOT_DECORATORS)


def _comment(syntax: _Syntax, text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith(syntax.comments) and not _decorator(syntax, text)


def _depth(syntax: _Syntax, text: str) -> int:
    """Count the brackets ``text`` opens minus those it closes, outside strings."""
    depth = 0
    quote = ""
    escaped = False
    for position, character in enumerate(text):
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif quote:
            quote = "" if character == quote else quote
        elif character in _QUOTES:
            quote = character
        elif any(text.startswith(comment, position) for comment in syntax.comments) and not (
            text.startswith(syntax.markers, position)
        ):
            break
        elif character in _OPENING:
            depth += 1
        elif character in _CLOSING:
            depth -= 1
    return depth


def _name(text: str) -> str | None:
    """Return the name a definition's line declares, such as ``run`` for ``def run(self):``."""
    head = _GENERIC.sub("", text.strip())
    cut = _NAME_END.search(head)
    names = _NAME.findall(head[: cut.start()] if cut else head)
    return names[-1] if names else None


def _indent(text: str) -> str:
    return text[: len(text) - len(text.lstrip())]


def _within(text: str, indent: str) -> bool:
    """Whether a line is blank or indented at least as deeply as ``indent``."""
    return not text.strip() or _indent(text).startswith(indent)


def _lines(text: str) -> list[tuple[str, str]]:
    parts: list[tuple[str, str]] = []
    start = 0
    for match in TEXT_LINE_BREAK.finditer(text):
        parts.append((text[start : match.start()], match.group()))
        start = match.end()
    if start < len(text):
        parts.append((text[start:], ""))
    return parts
