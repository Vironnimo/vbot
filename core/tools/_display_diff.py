"""Bounded line diffs of changed files for Tool detail presentation.

A file change is presentation-only display metadata, like display facts: it
never enters the Model-facing result. Accessors render it as a diff with line
numbers. Every change in one call shares one line budget, so a large rewrite
cannot bloat the persisted Tool display.
"""

from __future__ import annotations

from difflib import SequenceMatcher

from core.tools.arguments import split_text_lines
from core.tools.contracts import JsonObject

MAX_DISPLAY_DIFF_LINES = 2000
MAX_DISPLAY_DIFF_LINE_LENGTH = 1000
DISPLAY_DIFF_CONTEXT_LINES = 3
DISPLAY_FILE_CHANGE_KINDS = frozenset({"created", "updated", "replaced", "deleted", "moved"})
# Matching lines costs up to the product of both line counts; larger pairs
# read as a complete removal plus a complete addition instead.
_MAX_MATCHED_LINE_PAIRS = 25_000_000


def display_file_diff(
    path: str,
    change: str,
    before: str | None,
    after: str | None,
    *,
    destination: str | None = None,
    line_budget: int = MAX_DISPLAY_DIFF_LINES,
) -> JsonObject:
    """Return one file's diff as display metadata.

    ``None`` text means the file is absent on that side (``created`` or
    ``deleted``) or is not text; a non-text side yields ``binary`` without
    lines. ``added``/``removed`` count every changed line, while ``hunks``
    carry at most ``line_budget`` lines, each prefixed with ``+``, ``-`` or a
    space; ``omitted_lines`` counts the diff lines left out.
    """
    if change not in DISPLAY_FILE_CHANGE_KINDS:
        raise ValueError(f"Unsupported display file change: {change}")
    payload: JsonObject = {"path": path, "change": change}
    if destination:
        payload["destination"] = destination
    if change == "moved" and before == after:
        payload.update(added=0, removed=0, hunks=[])
        return payload
    absent_before = change == "created"
    absent_after = change == "deleted"
    if (before is None and not absent_before) or (after is None and not absent_after):
        payload.update(added=0, removed=0, hunks=[], binary=True)
        return payload

    old = split_text_lines(before or "", keepends=True)
    new = split_text_lines(after or "", keepends=True)
    groups = _grouped_changes(old, new)
    added = removed = shown = omitted = 0
    hunks: list[JsonObject] = []
    for group in groups:
        lines: list[str] = []
        for tag, i1, i2, j1, j2 in group:
            removed += 0 if tag == "equal" else i2 - i1
            added += 0 if tag == "equal" else j2 - j1
            if tag == "equal":
                marked = [(" ", line) for line in old[i1:i2]]
            else:
                marked = [("-", line) for line in old[i1:i2]]
                marked += [("+", line) for line in new[j1:j2]]
            for marker, line in marked:
                if shown >= line_budget:
                    omitted += 1
                    continue
                lines.append(marker + _display_line(line))
                shown += 1
        if lines:
            first = group[0]
            hunks.append({"old_start": first[1] + 1, "new_start": first[3] + 1, "lines": lines})
    payload.update(added=added, removed=removed, hunks=hunks)
    if omitted:
        payload["omitted_lines"] = omitted
    return payload


def display_diff_line_count(change: JsonObject) -> int:
    """Return how many diff lines one display file change carries."""
    hunks = change.get("hunks")
    if not isinstance(hunks, list):
        return 0
    return sum(len(hunk.get("lines", ())) for hunk in hunks if isinstance(hunk, dict))


def _grouped_changes(old: list[str], new: list[str]) -> list[list[tuple[str, int, int, int, int]]]:
    if old == new:
        return []
    if len(old) * len(new) > _MAX_MATCHED_LINE_PAIRS:
        return [[("replace", 0, len(old), 0, len(new))]]
    matcher = SequenceMatcher(None, old, new, autojunk=False)
    return list(matcher.get_grouped_opcodes(DISPLAY_DIFF_CONTEXT_LINES))


def _display_line(line: str) -> str:
    text = line.rstrip("\r\n")
    if len(text) <= MAX_DISPLAY_DIFF_LINE_LENGTH:
        return text
    return f"{text[: MAX_DISPLAY_DIFF_LINE_LENGTH - 1]}…"
