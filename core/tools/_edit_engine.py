"""Locate one parsed change in current text and splice it in.

Each kind of change runs through a short, fixed sequence of matching steps.
The first step that places the change wins; several matches at that step never
fall through to a later, looser one.

Text replacements (``old_string``/``new_string``) match as substrings:

1. ``fuzzy_match.replace_fuzzy``: exact, then its precise normalized strategies.
2. Read-output gutters stripped: a unique whole-line match.
3. Escapes such as ``\\u00fc`` of characters beyond ASCII read as those characters.
4. An edit whose new text already stands in place changes nothing.
5. ``copy_match.replace_copied``: old text copied with errors.

V4A line hunks (`` ``/``-``/``+`` lines, ``@@`` hints, ``*** End of File``):

1. Gutter-shaped added lines are cleaned (``clean_additions``).
2. The ``@@`` hints are located one after another as whole lines.
3. An addition-only hunk goes after the hint, or at the end of the file.
4. The old lines match precisely, as whole lines, from the hint on.
5. Read gutters stripped from the hunk; then surplus blank context at its edges.
6. A change already in the file is a no-op.
7. ``copy_match.match_copied_edit``: old lines copied with errors.
8. Unchanged lines between + lines that the file lacks are read as added lines.
9. Several precise matches: the first after the hint or the previous change.

``_splice`` writes every placed hunk: unchanged lines keep the file's bytes,
changed lines take the file's line endings, indentation and typography.

A hunk whose text held Secret references carries a literal reading
(``with_literal_readings``): the same change with every substituted value read
back as its reference text. It is tried, precisely only, when the substituted
old text has no precise match; when it matches, the file holds the reference
text itself and the change keeps it as text in the new text too.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from os.path import commonprefix

from core.tools._edit_hunks import _apply_line_hunk
from core.tools._edit_hunks import clean_additions as clean_additions
from core.tools._edit_locations import _BREAK, _change_end_line
from core.tools._edit_locations import AlreadyApplied as AlreadyApplied
from core.tools._edit_locations import DeviationNote as DeviationNote
from core.tools._edit_locations import file_excerpt as file_excerpt
from core.tools._edit_locations import line_ending as line_ending
from core.tools._edit_replacement import _apply_replacement
from core.tools._patch_syntax import _Hunk, _PatchError, _Replacement
from core.tools.fuzzy_match import (
    replace_fuzzy,
)


def apply_hunks(
    content: str,
    hunks: list[_Hunk],
    path: object,
    previous: int | None = None,
    *,
    failures: list[_PatchError] | None = None,
    unchanged: dict[str, str] | None = None,
) -> tuple[str, list[str], int | None]:
    """Apply one file's hunks in order and return the text, the notes and the change end.

    Each hunk sees the text the hunks before it produced. ``previous``, and the
    returned change end, is where the line holding the file's last change starts;
    it orders the occurrences of lines a later hunk matches.

    A hunk that fails raises its error. With ``failures``, it is skipped instead
    and its error appended there; a hunk that would write a NUL character fails
    too. Once a hunk failed, the later ones match precisely only (no copy with
    errors): the text they expect can be text the failed one would have written.
    A hunk that fails after others applied carries ``written`` in its error
    details: the lines of the text it saw that each earlier hunk wrote, as
    ``{label, first, last}``, and ``replaced_by``, the label of the earlier hunk
    that replaced its old text. With ``unchanged``, the labels of text
    replacements that changed nothing go there, each with the reason when its
    new text already stood in the file (``AlreadyApplied``), else "".
    """
    notes: list[str] = []
    written: list[tuple[str, int, int]] = []
    # The text each hunk that changed it saw, in order.
    seen: list[tuple[str, str]] = []
    for hunk in hunks:
        if failures:
            hunk = replace(hunk, precise_only=True)
        try:
            edited, placed = _place(content, hunk, path, previous)
            if failures is not None and "\x00" in edited:
                # Only binary files hold it; the call's other changes still apply.
                raise _PatchError("binary_file", template="nul_text", path=path, label=hunk.label)
        except _PatchError as error:
            if written:
                error.details["written"] = [
                    {"label": label, "first": first, "last": last} for label, first, last in written
                ]
            replaced_by = _replaced_by(seen, hunk) if error.code == "text_not_found" else None
            if replaced_by is not None:
                error.details["replaced_by"] = replaced_by
            if failures is None:
                raise
            failures.append(error)
            continue
        applied = [note for note in placed if isinstance(note, AlreadyApplied)]
        placed = [note for note in placed if not isinstance(note, AlreadyApplied)]
        if edited == content and unchanged is not None and hunk.replacement is not None:
            unchanged[hunk.label] = applied[0] if applied else ""
        if edited != content:
            seen.append((hunk.label, content))
            previous = _change_end_line(content, edited)
            written = _track_written(written, content, edited, hunk.label)
        content = edited
        notes.extend(placed)
    return content, notes, previous


# Errors after which a literal reading of the hunk is tried.
_NOT_FOUND = frozenset({"text_not_found", "context_not_found"})


def _place(content: str, hunk: _Hunk, path: object, previous: int | None) -> tuple[str, list[str]]:
    """Place one hunk; with a literal reading, as the reading the file holds."""
    literal = hunk.literal
    if literal is None:
        return _place_reading(content, hunk, path, previous)
    hunk = replace(hunk, literal=None)
    try:
        return _place_reading(content, replace(hunk, precise_only=True), path, previous)
    except _PatchError as error:
        if error.code not in _NOT_FOUND:
            raise
    try:
        edited, placed = _place_reading(
            content, replace(literal, precise_only=True), path, previous
        )
    except _PatchError:
        return _place_reading(content, hunk, path, previous)
    kept = " and ".join(literal.kept_references)
    return edited, [
        *placed,
        f"Kept {kept} as text: the file holds that text, not the Secret's value.",
    ]


def _place_reading(
    content: str, hunk: _Hunk, path: object, previous: int | None
) -> tuple[str, list[str]]:
    if hunk.replacement is not None:
        return _apply_replacement(content, hunk, path)
    return _apply_line_hunk(content, hunk, path, previous)


def with_literal_readings(hunks: list[_Hunk], literals: Mapping[str, str]) -> list[_Hunk]:
    """``hunks``, each whose text holds a value of ``literals`` with its literal reading.

    ``literals`` maps each Secret value dispatch substituted to the reference
    text it replaced (``ToolContext.secret_literals``).
    """
    if not literals:
        return hunks
    ordered = sorted(literals.items(), key=lambda item: len(item[0]), reverse=True)

    def read_back(text: str, kept: list[str]) -> str:
        for value, reference in ordered:
            if value in text:
                text = text.replace(value, reference)
                if reference not in kept:
                    kept.append(reference)
        return text

    changed: list[_Hunk] = []
    for hunk in hunks:
        kept: list[str] = []
        replacement = hunk.replacement
        literal = replace(
            hunk,
            hints=[read_back(hint, kept) for hint in hunk.hints],
            lines=[(prefix, read_back(text, kept)) for prefix, text in hunk.lines],
            replacement=None
            if replacement is None
            else _Replacement(
                read_back(replacement.old, kept),
                read_back(replacement.new, kept),
                replacement.replace_all,
            ),
        )
        # Old text without a reference cannot tell the readings apart.
        old_kept: list[str] = []
        if replacement is not None:
            read_back(replacement.old, old_kept)
        else:
            for prefix, text in hunk.lines:
                if prefix in " -":
                    read_back(text, old_kept)
        if not old_kept:
            changed.append(hunk)
            continue
        changed.append(replace(hunk, literal=replace(literal, kept_references=tuple(kept))))
    return changed


def _replaced_by(seen: list[tuple[str, str]], hunk: _Hunk) -> str | None:
    """Name the earlier hunk that replaced the old text ``hunk`` replaces, if one did.

    That is the last hunk whose text held it, matched precisely: the hunks after
    it never saw it.
    """
    old = hunk.replacement.old if hunk.replacement is not None else ""
    if not old:
        return None
    for label, before in reversed(seen):
        if replace_fuzzy(before, old, old, replace_all=False, typographic=True) is not None:
            return label
    return None


def _track_written(
    written: list[tuple[str, int, int]], before: str, after: str, label: str
) -> list[tuple[str, int, int]]:
    """Move earlier hunks' written lines past the change from ``before`` to ``after``.

    The change's own lines are added under ``label``; lines it replaced drop out
    of the earlier ranges.
    """
    prefix = len(commonprefix((before, after)))
    limit = min(len(before), len(after)) - prefix
    suffix = len(commonprefix((before[::-1][:limit], after[::-1][:limit])))
    old_end, new_end = len(before) - suffix, len(after) - suffix
    first = len(_BREAK.findall(before, 0, prefix)) + 1
    if old_end > prefix:
        last = len(_BREAK.findall(before, 0, old_end - 1)) + 1
    elif prefix == 0 or before[prefix - 1] in "\r\n":
        last = first - 1  # whole lines inserted: no earlier line changes
    else:
        last = first
    delta = len(_BREAK.findall(after)) - len(_BREAK.findall(before))
    moved: list[tuple[str, int, int]] = []
    for name, start, end in written:
        if start < first:
            moved.append((name, start, min(end, first - 1)))
        if end > last:
            moved.append((name, max(start, last + 1) + delta, end + delta))
    if new_end > prefix:
        start = len(_BREAK.findall(after, 0, prefix)) + 1
        moved.append((label, start, len(_BREAK.findall(after, 0, new_end - 1)) + 1))
    return moved
