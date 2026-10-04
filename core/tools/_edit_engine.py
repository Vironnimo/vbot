"""Locate one parsed change in current text and splice it in.

Each kind of change runs through a short, fixed sequence of matching steps.
The first step that places the change wins; several matches at that step never
fall through to a later, looser one.

Text replacements (``old_string``/``new_string``) match as substrings:

1. ``fuzzy_match.replace_fuzzy``: exact, then its precise normalized strategies.
2. Read-output gutters stripped: a unique whole-line match.
3. ``copy_match.replace_copied``: old text copied with errors.

V4A line hunks (`` ``/``-``/``+`` lines, ``@@`` hints, ``*** End of File``):

1. Gutter-shaped added lines are cleaned (``clean_additions``).
2. The ``@@`` hints are located one after another as whole lines.
3. An addition-only hunk goes after the hint, or at the end of the file.
4. The old lines match precisely, as whole lines, from the hint on.
5. Read gutters stripped from the hunk; then surplus blank context at its edges.
6. A change already in the file is a no-op.
7. ``copy_match.match_copied_edit``: old lines copied with errors.
8. Several precise matches: the first after the hint or the previous change.

``_splice`` writes every placed hunk: unchanged lines keep the file's bytes,
changed lines take the file's line endings, indentation and typography.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from os.path import commonprefix
from typing import Literal

from core.tools._patch_syntax import _Hunk, _PatchError
from core.tools.arguments import (
    TEXT_LINE_BREAK,
    line_number_gutter_candidates,
    split_text_lines,
    strip_line_number_gutters,
)
from core.tools.copy_match import copy_warnings, match_copied_edit, replace_copied
from core.tools.fuzzy_match import (
    AmbiguousFuzzyMatch,
    FuzzyReplacement,
    find_closest_candidates,
    first_difference,
    preserve_typography,
    replace_fuzzy,
)
from core.tools.tools import JsonObject

type _Found = FuzzyReplacement | AmbiguousFuzzyMatch | None

_GUTTER_WARNING = "Removed read-output line-number prefixes before applying the hunk."
_GUTTER_KEPT_NOTE = (
    "{count} added lines start with a number and | like read output, such as {example!r}; "
    "they were written as sent. If they are copied line numbers, remove them with another patch."
)
_FIRST_AFTER_HINT_NOTE = (
    "The lines to replace occur {occurrences} times after {hint!r}; the first, at line "
    "{line}, was changed."
)
_FIRST_AFTER_PREVIOUS_NOTE = (
    "The lines to replace occur {occurrences} times; the first after the previous change "
    "in this file, at line {line}, was changed."
)
_BREAK = TEXT_LINE_BREAK
_GUTTER = re.compile(r"^\s*[1-9][0-9]*(?::[1-9][0-9]*)?\|")


def apply_hunks(
    content: str, hunks: list[_Hunk], path: object, previous: int | None = None
) -> tuple[str, list[str], int | None]:
    """Apply one file's hunks in order and return the text, the notes and the change end.

    Each hunk sees the text the hunks before it produced. ``previous``, and the
    returned change end, is where the line holding the file's last change starts;
    it orders the occurrences of lines a later hunk matches.
    """
    notes: list[str] = []
    for hunk in hunks:
        if hunk.replacement is not None:
            edited, placed = _apply_replacement(content, hunk, path)
        else:
            edited, placed = _apply_line_hunk(content, hunk, path, previous)
        if edited != content:
            previous = _change_end_line(content, edited)
        content = edited
        notes.extend(placed)
    return content, notes, previous


def line_ending(content: str) -> str:
    """Return the first line ending ``content`` uses, else LF."""
    for ending in ("\r\n", "\n", "\r"):
        if ending in content:
            return ending
    return "\n"


def file_excerpt(lines: list[str], first: int, last: int) -> JsonObject:
    """Return file lines ``first``-``last`` (1-based) as a report candidate.

    Each line is cut at 240 characters; a cut line names where ``read`` continues it.
    """
    shown = lines[first - 1 : last]
    return {
        "line": first,
        "text": "\n".join(line[:240] for line in shown),
        "truncated": any(len(line) > 240 for line in shown),
        "continuations": [
            {"offset": f"{number}:241", "limit": 1}
            for number, line in enumerate(shown, first)
            if len(line) > 240
        ],
    }


def clean_additions(hunk: _Hunk, path: object) -> tuple[_Hunk, list[str]]:
    """Strip read gutters from runs of added lines that all carry one.

    Existing literal gutter-shaped context is authoritative. A run of added
    lines is copied read output only when every line carries a gutter; a few
    gutter-shaped lines among others are content, such as N|value data rows.
    """
    if _located_by_gutters(hunk):
        return hunk, []
    lines = list(hunk.lines)
    warnings = []
    start = 0
    while start < len(lines):
        if lines[start][0] != "+":
            start += 1
            continue
        end = start + 1
        while end < len(lines) and lines[end][0] == "+":
            end += 1
        texts = [text for _, text in lines[start:end]]
        shaped = [text for text in texts if _GUTTER.match(text)]
        if len(shaped) >= 2 and len(shaped) == len(texts):
            candidates = line_number_gutter_candidates("\n".join(texts), allow_continuations=False)
            if not candidates:
                raise _PatchError("line_numbered_content", path=path, label=hunk.label)
            lines[start:end] = [("+", text) for text in candidates[0].split("\n")]
            warnings.append(_GUTTER_WARNING)
        elif len(shaped) >= 2:
            warnings.append(_GUTTER_KEPT_NOTE.format(count=len(shaped), example=shaped[0]))
        start = end
    return replace(hunk, lines=lines), warnings


# --- Text replacements -----------------------------------------------------------------


def _apply_replacement(content: str, hunk: _Hunk, path: object) -> tuple[str, list[str]]:
    """Replace ``old_string`` text, located precisely, else as a copy with errors."""
    replacement = hunk.replacement
    assert replacement is not None
    old, new, replace_all = replacement.old, replacement.new, replacement.replace_all
    notes: list[str] = []
    found = replace_fuzzy(content, old, new, replace_all=replace_all, typographic=True)
    stripped = strip_line_number_gutters(old) if found is None else None
    if stripped is not None:
        # Text copied from read output: its gutters cover whole lines.
        stripped_new = strip_line_number_gutters(new)
        found = replace_fuzzy(
            content,
            stripped,
            new if stripped_new is None else stripped_new,
            replace_all=replace_all,
            whole_lines=True,
            typographic=True,
        )
        if isinstance(found, FuzzyReplacement):
            notes.append(_GUTTER_WARNING)
    if isinstance(found, AmbiguousFuzzyMatch):
        details, values = _ambiguity(content, found)
        raise _PatchError(
            "ambiguous_match",
            template="ambiguous_replacement",
            path=path,
            label=hunk.label,
            details=details,
            **values,
        )
    if found is not None:
        return found.new_content, notes
    details = _not_found(content, old, source="old_string")
    present = None
    if new.strip() and new != old:
        present = replace_fuzzy(content, new, new, replace_all=True, typographic=True)
        # Text the old text already holds proves nothing about an earlier edit.
        if isinstance(present, FuzzyReplacement) and new.strip() not in old:
            details["already_present"] = present.first_changed_line
    if not replace_all and present is None and not hunk.precise_only:
        copied = replace_copied(content, old, new)
        if isinstance(copied, AmbiguousFuzzyMatch):
            details, values = _ambiguity(content, copied)
            raise _PatchError(
                "ambiguous_match",
                template="ambiguous_copy",
                path=path,
                label=hunk.label,
                details=details,
                **values,
            )
        if copied is not None:
            return copied.new_content, copy_warnings(copied)
    raise _PatchError(
        "text_not_found",
        template="old_text_not_found",
        path=path,
        label=hunk.label,
        details=details,
    )


# --- V4A line hunks --------------------------------------------------------------------


@dataclass(frozen=True)
class _Section:
    """Where a hunk's ``@@`` hints put it.

    ``start`` is where the last hint's line starts (the hunk may repeat that
    line), ``end`` where the line after it starts; both are 0 without hints.
    ``repeated`` is the error for a hint that occurs several times: read from
    its first occurrence, the hunk's lines must then match once, precisely.
    """

    start: int = 0
    end: int = 0
    repeated: _PatchError | None = None


def _apply_line_hunk(
    content: str, hunk: _Hunk, path: object, previous: int | None
) -> tuple[str, list[str]]:
    """Apply one V4A hunk; ``previous`` is where the line of the file's last change starts."""
    if not hunk.changes_text():
        return content, []
    hunk, notes = clean_additions(hunk, path)
    section = _locate_hints(content, hunk, path)
    if not any(prefix in " -" for prefix, _ in hunk.lines):
        if section.repeated is not None:
            raise section.repeated
        position = section.end if hunk.hints else len(content)
        return _insert(content, hunk, position, path), notes
    start = section.start
    hunk, found = _match_old_lines(content, start, hunk, path, notes)
    window = content[start:]
    if found is None and _already_applied(window, hunk, section):
        return content, notes
    copied = False
    if found is None and _may_copy(window, hunk):
        found, copied = match_copied_edit(window, hunk.lines, at_eof=hunk.eof), True
    if found is None:
        raise _hunk_not_found(content, hunk, section, path)
    if section.repeated is not None and (copied or isinstance(found, AmbiguousFuzzyMatch)):
        raise section.repeated
    if isinstance(found, AmbiguousFuzzyMatch):
        occurrences = found.occurrences
        start, found = _first_occurrence(content, hunk, found, section, previous, copied, path)
        notes.append(_ordering_note(content, hunk, occurrences, found, start))
    return _splice(content, start, hunk, found, path, notes)


def _locate_hints(content: str, hunk: _Hunk, path: object) -> _Section:
    """Step 2: find each ``@@`` hint as whole lines after the one before it."""
    start = offset = 0
    repeated: _PatchError | None = None
    for hint in hunk.hints:
        found = _match(content[offset:], hint, hint)
        if isinstance(found, AmbiguousFuzzyMatch):
            # As in Codex, a repeated hint is read from its first occurrence.
            details, values = _ambiguity(content, found, offset)
            repeated = repeated or _PatchError(
                "ambiguous_context",
                path=path,
                label=hunk.label,
                hint=_hint_text(hint),
                details=details,
                **values,
            )
            found = _match(content[offset:], hint, hint, first=True)
        if not isinstance(found, FuzzyReplacement):
            raise _context_not_found(content, hint, hunk, path)
        start = offset + found.before_spans[0][0]
        offset += found.before_spans[0][1]
        ending = _BREAK.match(content, offset)
        if ending:
            offset = ending.end()
    return _Section(start, offset, repeated)


def _context_not_found(content: str, hint: str, hunk: _Hunk, path: object) -> _PatchError:
    details = _candidates(content, hint)
    # A block of unchanged lines before this one locates it; its report names
    # the first line that differs, like the report for the lines to replace.
    block = "\n" in hint
    if block and details["candidates"]:
        difference = first_difference(content, hint, details["candidates"][0]["line"])
        if difference is not None:
            details["difference"] = {**difference, "source": "patch"}
    return _PatchError(
        "context_not_found",
        template="context_block_not_found" if block else None,
        path=path,
        label=hunk.label,
        hint=_hint_text(hint),
        details=details,
    )


def _insert(content: str, hunk: _Hunk, position: int, path: object) -> str:
    """Step 3: insert an addition-only hunk at ``position``."""
    if hunk.no_newline and position != len(content):
        raise _PatchError(
            "invalid_patch", template="no_newline_position", path=path, label=hunk.label
        )
    new = _hunk_text(hunk, " +")
    if hunk.eof and position != len(content):
        raise _PatchError(
            "text_not_found",
            template="eof_not_found",
            path=path,
            label=hunk.label,
            details=_candidates(content, new),
        )
    ending = line_ending(content)
    inserted = new.replace("\n", ending) + ("" if hunk.no_newline else ending)
    # A hint ties the post-state to an insertion point. A suffix alone cannot
    # distinguish a retry from an intentional repeated append.
    if hunk.hints and inserted.strip() and content[position:].startswith(inserted):
        return content
    separator = ending if position and not _BREAK.search(content[position - 1 : position]) else ""
    return content[:position] + separator + inserted + content[position:]


def _match_old_lines(
    content: str, start: int, hunk: _Hunk, path: object, notes: list[str]
) -> tuple[_Hunk, _Found]:
    """Steps 4 and 5: match the old lines precisely, then without gutters or blank edges.

    Returns the hunk as it matched and the match.
    """
    window = content[start:]
    found = _match_hunk(window, hunk)
    if found is None:
        normalized = _normalize_gutters(hunk)
        if normalized is not None:
            hunk = normalized
            notes.append(_GUTTER_WARNING)
            found = _match_hunk(window, hunk)
        if found is None and _located_by_gutters(hunk):
            raise _PatchError(
                "line_numbered_content",
                path=path,
                label=hunk.label,
                details=_candidates(content, _hunk_text(hunk, " -")),
            )
    if found is None:
        # Surplus blank context at the edges counts only after the whole hunk
        # missed. Removed blank lines remain part of the change.
        lines = list(hunk.lines)
        while lines and lines[0][0] == " " and not lines[0][1].strip():
            lines.pop(0)
        while lines and lines[-1][0] == " " and not lines[-1][1].strip():
            lines.pop()
        if lines != hunk.lines and any(prefix in " -" for prefix, _ in lines):
            trimmed = replace(hunk, lines=lines)
            candidate = _match_hunk(window, trimmed)
            if candidate is not None:
                hunk, found = trimmed, candidate
    return hunk, found


def _already_applied(window: str, hunk: _Hunk, section: _Section) -> bool:
    """Step 6: whether the hunk's new lines are already in the file at one place.

    The post-state needs an independent identification: at least four
    non-blank characters of unchanged lines, or, for a single-line
    replacement, unique text kept on both sides of the change. A repeated hint
    never confirms it.
    """
    old, new = _hunk_text(hunk, " -"), _hunk_text(hunk, " +")
    if not new or section.repeated is not None:
        return False
    context = "".join(text.strip() for prefix, text in hunk.lines if prefix == " ")
    if len(context) < 4 and not _inline_context_identifies(window, old, new):
        return False
    poststate = _match(window, new, new, eof=hunk.eof or hunk.no_newline)
    return isinstance(poststate, FuzzyReplacement) and (
        not hunk.no_newline or poststate.before_spans[0][1] == len(window)
    )


def _may_copy(window: str, hunk: _Hunk) -> bool:
    """Whether step 7 may look for old lines copied with errors.

    Not after an earlier failure in this file, and not while the new text is
    already present: a precise post-state elsewhere does not prove the change
    was made, and a similar passage must not stand in for it.
    """
    new = _hunk_text(hunk, " +")
    return not hunk.precise_only and (not new or _match(window, new, new) is None)


def _first_occurrence(
    content: str,
    hunk: _Hunk,
    found: AmbiguousFuzzyMatch,
    section: _Section,
    previous: int | None,
    copied: bool,
    path: object,
) -> tuple[int, FuzzyReplacement]:
    """Step 8: take the first precise match after the hint or the previous change.

    As in Codex, an ``@@`` line or the same Update's previous change orders the
    occurrences (Sessions: the Agent meant that one in 60 of 61 such hunks). A
    hunk without either, or one matched as a copy with errors, must match once.
    Returns where the window of the match starts and the match.
    """
    start = section.start if hunk.hints else previous
    first = None
    if start is not None and not copied:
        first = _match_hunk(content[start:], hunk, first=True)
    if start is None or not isinstance(first, FuzzyReplacement):
        details, values = _ambiguity(content, found, section.start)
        raise _PatchError(
            "ambiguous_match",
            template="ambiguous_patch_copy" if copied else None,
            path=path,
            label=hunk.label,
            details=details,
            **values,
        )
    return start, first


def _ordering_note(
    content: str, hunk: _Hunk, occurrences: int, found: FuzzyReplacement, start: int
) -> str:
    """Name the occurrence step 8 changed; ``found`` matched in ``content[start:]``."""
    line = found.first_changed_line + len(_BREAK.findall(content, 0, start))
    if hunk.hints:
        hint = _hint_text(hunk.hints[-1]).strip()
        return _FIRST_AFTER_HINT_NOTE.format(occurrences=occurrences, hint=hint, line=line)
    return _FIRST_AFTER_PREVIOUS_NOTE.format(occurrences=occurrences, line=line)


def _hunk_not_found(content: str, hunk: _Hunk, section: _Section, path: object) -> _PatchError:
    """Describe why a hunk's old lines were not found, naming the line to fix."""
    old = _hunk_text(hunk, " -")
    # Lines the file holds exactly fail only through the hunk's place: the end
    # of the file it names, or its @@ lines.
    if hunk.eof and _match(content[section.start :], old, old) is not None:
        return _PatchError(
            "text_not_found",
            template="eof_not_found",
            path=path,
            label=hunk.label,
            details=_candidates(content, old),
        )
    elsewhere = _match(content, old, old) if hunk.hints else None
    if elsewhere is not None:
        numbers = (
            elsewhere.line_numbers
            if isinstance(elsewhere, AmbiguousFuzzyMatch)
            else [elsewhere.first_changed_line]
        )
        return _PatchError(
            "text_not_found",
            template="not_after_hint",
            path=path,
            label=hunk.label,
            hint=_hint_text(hunk.hints[-1]),
            lines=_line_list(numbers),
            details=_candidates(content, old),
        )
    present = {_loose(line) for line in split_text_lines(content)}
    absent = next(
        (
            i
            for i, (prefix, text) in enumerate(hunk.lines)
            if prefix in " -" and text.strip() and _loose(text) not in present
        ),
        None,
    )
    # An unchanged line next to + lines that the file lacks is most often an
    # added line missing its +, not a piece copied out of a longer line.
    unmarked = (
        absent is not None
        and hunk.lines[absent][0] == " "
        and _beside_additions(hunk.lines, absent)
    )
    details = _not_found(content, old, source="patch", part_of=not unmarked)
    difference = details.get("difference")
    if difference:
        # Number the hunk's unchanged and removed lines as the report counts them.
        numbered = [i for i, (prefix, _) in enumerate(hunk.lines) if prefix in " -"]
        position = difference["copy_line"] - 1
        if position < len(numbered) and hunk.lines[numbered[position]][0] == " ":
            difference["unprefixed"] = _beside_additions(hunk.lines, numbered[position])
    elif not details["candidates"] and "part_of" not in details and absent is not None:
        # Nothing resembles the lines as a whole; a line the file has nowhere
        # is then the one to fix, often a new line written without +.
        prefix, text = hunk.lines[absent]
        details["absent"] = {
            "text": text.strip(),
            "removed": prefix == "-",
            "beside_additions": prefix == " " and _beside_additions(hunk.lines, absent),
        }
    return _PatchError("text_not_found", path=path, label=hunk.label, details=details)


def _splice(
    content: str,
    offset: int,
    hunk: _Hunk,
    found: FuzzyReplacement,
    path: object,
    notes: list[str],
) -> tuple[str, list[str]]:
    """Write a placed hunk into ``content``; the match is in ``content[offset:]``.

    Unchanged lines keep their actual bytes; added lines take the file's line
    ending, and the indentation and typography the match carried over.
    """
    if [t for p, t in hunk.lines if p in " -"] == [t for p, t in hunk.lines if p in " +"]:
        return content, notes
    window = content[offset:]
    old, new = _hunk_text(hunk, " -"), _hunk_text(hunk, " +")
    start, end = found.before_spans[0]
    after_start, after_end = found.after_spans[0]
    actual = _line_parts(window[start:end])
    if found.strategy != "exact" and any(
        token in old and token in new and token not in window[start:end]
        for token in ('\\"', "\\'", "\\\\")
    ):
        # An approximate match cannot introduce escapes the target lacks.
        raise _PatchError(
            "text_not_found",
            path=path,
            label=hunk.label,
            details=_not_found(content, old, source="patch"),
        )
    prepared = _line_parts(found.new_content[after_start:after_end])
    # Line splitting omits the final empty line; the hunk still gives it a position.
    old_count = sum(p in " -" for p, _ in hunk.lines)
    new_count = sum(p in " +" for p, _ in hunk.lines)
    padded = len(actual) < old_count
    if padded:
        actual.append(("", ""))
    if len(prepared) < new_count:
        prepared.append(("", ""))
    if len(actual) != old_count or len(prepared) != new_count:
        raise _PatchError(
            "text_not_found",
            path=path,
            label=hunk.label,
            details=_not_found(content, old, source="patch"),
        )
    trailing = _BREAK.match(window, end)
    final_ending = trailing.group() if trailing else ""
    if trailing:
        end = trailing.end()
    if hunk.no_newline and end != len(window):
        raise _PatchError(
            "invalid_patch", template="no_newline_position", path=path, label=hunk.label
        )
    if actual:
        actual[-1] = (actual[-1][0], final_ending)
    added_ending = final_ending
    if padded and not final_ending and len(actual) > 1:
        # Past the file's final line break, the final empty line has no ending of
        # its own; lines added after it end the file with that line break.
        added_ending = actual[-2][1]
    ending = line_ending(content)
    output: list[tuple[str, str]] = []
    last_output_prefix = ""
    old_index = new_index = 0
    removed_lines: list[tuple[str, str]] = []
    for prefix, locator_line in hunk.lines:
        if prefix == " ":
            removed_lines.clear()
            output.append(actual[old_index])  # Preserve every context byte.
            last_output_prefix = prefix
        elif prefix == "+":
            replacement_line = prepared[new_index][0]
            if removed_lines:
                actual_line, old_line = removed_lines.pop(0)
                if found.strategy != "exact":
                    replacement_line = preserve_typography(actual_line, old_line, replacement_line)
            output.append((replacement_line, ending))
            last_output_prefix = prefix
        elif prefix == "-":
            removed_lines.append((actual[old_index][0], locator_line))
        if prefix in " -":
            old_index += 1
        if prefix in " +":
            new_index += 1
    if output:
        output = [(text, line_end or ending) for text, line_end in output[:-1]] + output[-1:]
        if last_output_prefix == "+" or hunk.no_newline:
            output[-1] = (output[-1][0], "" if hunk.no_newline else added_ending)
    replacement_text = "".join(text + line_end for text, line_end in output)
    notes.extend(copy_warnings(found, line_shift=len(_BREAK.findall(content, 0, offset))))
    return content[: offset + start] + replacement_text + window[end:], notes


# --- Matching helpers ------------------------------------------------------------------


def _match(
    content: str,
    old: str,
    new: str,
    *,
    eof: bool = False,
    first: bool = False,
) -> _Found:
    """Match ``old`` as whole lines precisely; an empty ``old`` is one blank line."""
    if old == "":
        positions = []
        offset = 0
        for line, (text, ending) in enumerate(_line_parts(content), 1):
            if text == "" and (not eof or offset + len(ending) == len(content)):
                positions.append((offset, line))
            offset += len(text) + len(ending)
        if not positions:
            return None
        if len(positions) > 1 and not first:
            return AmbiguousFuzzyMatch(
                len(positions), [line for _, line in positions], [1] * len(positions)
            )
        start, line = positions[0]
        return FuzzyReplacement(
            content[:start] + new + content[start:],
            line,
            line,
            1,
            "exact",
            ((start, start),),
            ((start, start + len(new)),),
        )
    return replace_fuzzy(
        content,
        old,
        new,
        replace_all=False,
        whole_lines=True,
        at_eof=eof,
        typographic=True,
        first=first,
    )


def _match_hunk(window: str, hunk: _Hunk, *, first: bool = False) -> _Found:
    return _match(window, _hunk_text(hunk, " -"), _hunk_text(hunk, " +"), eof=hunk.eof, first=first)


def _normalize_gutters(hunk: _Hunk) -> _Hunk | None:
    """Return the hunk with read gutters stripped, or ``None`` when they do not fit.

    Locators may mix raw lines with copied gutters. Only a unique whole-line
    match against the current file authorizes this recovery.
    """
    old = "\n".join(text for prefix, text in hunk.lines if prefix in " -")
    if strip_line_number_gutters(old) is None:
        return None
    lines = []
    for prefix, text in hunk.lines:
        stripped = strip_line_number_gutters(text)
        if stripped is not None and re.match(r"^\s*\d+:\d+\|", text):
            return None
        lines.append((prefix, text if stripped is None else stripped))
    return replace(hunk, lines=lines)


def _located_by_gutters(hunk: _Hunk) -> bool:
    """Whether unchanged or removed lines carry read gutters; added lines never locate."""
    return any(_GUTTER.match(text) for prefix, text in hunk.lines if prefix in " -")


def _inline_context_identifies(content: str, old: str, new: str) -> bool:
    """Identify a single-line replacement by two retained, unique text anchors."""
    if "\n" in old or "\n" in new:
        return False
    prefix = commonprefix((old, new))
    suffix = commonprefix((old[len(prefix) :][::-1], new[len(prefix) :][::-1]))[::-1]
    # Syntax/indentation alone cannot identify a target. Both sides must retain
    # substantive text, and together they must select exactly one current line.
    if any(
        len(anchor.strip()) < 4 or not re.search(r"\w{3}", anchor) for anchor in (prefix, suffix)
    ):
        return False
    candidates = [
        line
        for line in split_text_lines(content)
        if line.startswith(prefix) and line.endswith(suffix)
    ]
    return candidates == [new]


def _beside_additions(lines: list[tuple[str, str]], index: int) -> bool:
    """Tell whether the unchanged line at ``index`` can be an added line missing its +.

    It can when unchanged lines alone separate it from + lines on both sides,
    or when a + line is right next to it.
    """
    before = index - 1
    while before >= 0 and lines[before][0] == " ":
        before -= 1
    after = index + 1
    while after < len(lines) and lines[after][0] == " ":
        after += 1
    if before >= 0 and after < len(lines) and lines[before][0] == lines[after][0] == "+":
        return True
    return any(0 <= i < len(lines) and lines[i][0] == "+" for i in (index - 1, index + 1))


def _change_end_line(before: str, after: str) -> int:
    """Return where the line holding the end of the change from ``before`` starts."""
    prefix = len(commonprefix((before, after)))
    limit = min(len(before), len(after)) - prefix
    suffix = len(commonprefix((before[::-1][:limit], after[::-1][:limit])))
    end = max(prefix, len(after) - suffix - 1)
    return max(after.rfind("\n", 0, end), after.rfind("\r", 0, end)) + 1


def _line_parts(content: str) -> list[tuple[str, str]]:
    parts: list[tuple[str, str]] = []
    start = 0
    for match in _BREAK.finditer(content):
        parts.append((content[start : match.start()], match.group()))
        start = match.end()
    if start < len(content):
        parts.append((content[start:], ""))
    return parts


def _hunk_text(hunk: _Hunk, prefixes: str) -> str:
    return "\n".join(text for prefix, text in hunk.lines if prefix in prefixes)


def _hint_text(hint: str) -> str:
    """Show a context hint in one line; a multi-line context block by its first line."""
    first, _, rest = hint.partition("\n")
    return first[:120] + (" ..." if rest or len(first) > 120 else "")


def _loose(text: str) -> str:
    return " ".join(text.split())


# --- Failure details -------------------------------------------------------------------


def _candidates(content: str, pattern: str) -> JsonObject:
    file_lines = split_text_lines(content)
    candidates = []
    for candidate in find_closest_candidates(content, pattern):
        details: JsonObject = {
            "line": candidate.line_number,
            "text": candidate.text,
            "truncated": candidate.truncated,
        }
        if candidate.truncated:
            shown = candidate.text.split("\n")
            last = candidate.line_number + len(shown) - 1
            character = len(shown[-1]) + 1
            # A cut between whole lines resumes at the next line, not beyond
            # the end of the one just shown. Coordinates count characters,
            # like read, regardless of UTF-8 bytes or the file's line endings.
            if len(shown[-1]) == len(file_lines[last - 1]):
                last, character = last + 1, 1
            remaining = max(1, len(split_text_lines(pattern)) - (last - candidate.line_number))
            details["continuations"] = [
                {"offset": f"{last}:{character}", "limit": min(8, remaining)}
            ]
        candidates.append(details)
    return {"candidates": candidates}


def _not_found(
    content: str, old: str, *, source: Literal["patch", "old_string"], part_of: bool = True
) -> JsonObject:
    """Return the closest current text and the first line where it differs.

    ``source`` names the argument that holds ``old``, so the report can name it.
    ``part_of`` allows naming longer lines that hold the first missing line.
    """
    part = _part_of_lines(content, old) if source == "patch" and part_of else None
    # A missing line found inside a few longer lines explains the failure better
    # than the closest text; inside many lines, the closest text is more useful.
    if part is not None and part["count"] <= 3:
        return {"candidates": [], "part_of": part}
    details = _candidates(content, old)
    if part is not None and not details["candidates"]:
        details["part_of"] = part
    if details["candidates"]:
        difference = first_difference(content, old, details["candidates"][0]["line"])
        if difference is not None:
            details["difference"] = {**difference, "source": source}
    return details


def _part_of_lines(content: str, old: str) -> JsonObject | None:
    """Name the lines that hold the first missing patch line only as part of their text."""
    file_lines = split_text_lines(content)
    whole = {_loose(line) for line in file_lines}
    for wanted in split_text_lines(old):
        text = wanted.strip()
        if not text or _loose(wanted) in whole:
            continue
        numbers = [number for number, line in enumerate(file_lines, 1) if _cut_from(text, line)]
        if not numbers:
            return None
        excerpts = [
            {
                "line": number,
                "text": file_lines[number - 1][:240],
                "truncated": len(file_lines[number - 1]) > 240,
                "continuations": (
                    [{"offset": f"{number}:241", "limit": 1}]
                    if len(file_lines[number - 1]) > 240
                    else []
                ),
            }
            for number in numbers[:3]
        ]
        return {
            "text": text,
            "count": len(numbers),
            "lines": _line_list(numbers),
            "excerpts": excerpts,
        }
    return None


def _cut_from(text: str, line: str) -> bool:
    """Tell whether ``text`` reads as a piece copied out of ``line``, not a chance match.

    The piece holds at least 3 letters or digits, does not split a word of the
    line, and starts or ends the line or is at least 8 characters long. Short
    text such as ``}``, ``1,`` or ``pass`` occurs by chance inside unrelated lines.
    """
    if sum(character.isalnum() for character in text) < 3:
        return False
    stripped = line.strip()
    if len(text) < 8 and not (stripped.startswith(text) or stripped.endswith(text)):
        return False
    start = line.find(text)
    while start >= 0:
        end = start + len(text)
        if not (
            _joins_word(line[start - 1 : start], text[0])
            or _joins_word(text[-1], line[end : end + 1])
        ):
            return True
        start = line.find(text, start + 1)
    return False


def _joins_word(left: str, right: str) -> bool:
    """Tell whether the characters ``left`` and ``right`` belong to one word."""
    return bool(left and right) and all(ch.isalnum() or ch == "_" for ch in left + right)


def _line_list(numbers: list[int]) -> str:
    shown = [str(number) for number in numbers[:6]]
    if len(numbers) > len(shown):
        shown.append("...")
    return ("line " if len(numbers) == 1 else "lines ") + ", ".join(shown)


def _ambiguity(
    content: str, match: AmbiguousFuzzyMatch, offset: int = 0
) -> tuple[JsonObject, JsonObject]:
    """Return excerpts around each occurrence, and the values an error message names."""
    shift = len(_BREAK.findall(content[:offset]))
    numbers = [number + shift for number in dict.fromkeys(match.line_numbers)]
    lines = split_text_lines(content)
    candidates = [
        file_excerpt(lines, max(1, number - 1), min(len(lines), number + 1))
        for number in numbers[:3]
    ]
    details: JsonObject = {"occurrences": match.occurrences, "candidates": candidates}
    return details, {"occurrences": match.occurrences, "lines": _line_list(numbers)}
