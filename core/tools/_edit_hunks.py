"""Ordered placement and splicing of V4A line hunks."""

from __future__ import annotations

from dataclasses import dataclass, replace
from difflib import SequenceMatcher

from core.tools._decorated_insert import place_before_decorators
from core.tools._edit_diagnostics import _ambiguity, _candidates, _line_list, _not_found
from core.tools._edit_locations import (
    _BREAK,
    _GUTTER,
    _GUTTER_WARNING,
    DeviationNote,
    _beside_additions,
    _between_additions,
    _deviations,
    _Found,
    _hint_text,
    _hunk_text,
    _inline_context_identifies,
    _line_parts,
    _located_by_gutters,
    _loose,
    _match,
    _match_hunk,
    _normalize_gutters,
    _poststate,
    line_ending,
)
from core.tools._fuzzy_differences import first_difference
from core.tools._patch_syntax import _amid_additions, _Hunk, _PatchError, quoted
from core.tools.arguments import (
    line_number_gutter_candidates,
    split_text_lines,
)
from core.tools.copy_match import copy_warnings, match_copied_edit
from core.tools.fuzzy_match import (
    AmbiguousFuzzyMatch,
    FuzzyReplacement,
    preserve_typography,
)

_GUTTER_KEPT_NOTE = (
    "{count} added lines start with a number and | like read output, such as {example}; "
    "they were written as sent. If they are copied line numbers, remove them in another call."
)
_FIRST_AFTER_HINT_NOTE = (
    "The lines to replace occur {occurrences} times after {hint}; the first, at line "
    "{line}, was changed."
)
_FIRST_AFTER_PREVIOUS_NOTE = (
    "The lines to replace occur {occurrences} times; the first after the previous change "
    "in this file, at line {line}, was changed."
)
_UNMARKED_NOTE = (
    "The line {example} between + lines has no + prefix and is not in the file; it was "
    "added as a new line."
)
_UNMARKED_LINES_NOTE = (
    "{count} lines between + lines have no + prefix and are not in the file, such as "
    "{example}; they were added as new lines."
)
_UNMARKED_BLANK_NOTE = (
    "The empty line between + lines has no prefix and, read as unchanged, does not match the "
    "file; it was added as a new line."
)
_UNMARKED_BLANKS_NOTE = (
    "{count} empty lines between + lines have no prefix and, read as unchanged, do not match the "
    "file; they were added as new lines."
)
_CLOSING_EOF_NOTE = (
    "The patch ends with *** End of File and has no *** End Patch, so that line was read as "
    "the end of the patch, not of the file: the change was made at line {line}."
)


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
            warnings.append(_GUTTER_KEPT_NOTE.format(count=len(shaped), example=quoted(shaped[0])))
        start = end
    return replace(hunk, lines=lines), warnings


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
    if hunk.closing_eof:
        # Its *** End of File places the hunk where it can; else it only closed the patch.
        try:
            return _apply_line_hunk(content, replace(hunk, closing_eof=False), path, previous)
        except _PatchError:
            hunk = replace(hunk, eof=False)
    hunk, notes = clean_additions(hunk, path)
    section = _locate_hints(content, hunk, path)
    if not any(prefix in " -" for prefix, _ in hunk.lines):
        if section.repeated is not None:
            raise section.repeated
        position = section.end if hunk.hints else len(content)
        if hunk.closing_eof and content[position:].strip():
            line = len(_BREAK.findall(content, 0, position)) + 1
            notes.append(DeviationNote(_CLOSING_EOF_NOTE.format(line=line)))
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
        unmarked = _unmarked_additions(content, start, hunk)
        if unmarked is None:
            raise _hunk_not_found(content, hunk, section, path)
        hunk, found, note = unmarked
        copied = False
        notes.append(note)
    if section.repeated is not None and (copied or isinstance(found, AmbiguousFuzzyMatch)):
        raise section.repeated
    if isinstance(found, AmbiguousFuzzyMatch):
        occurrences = found.occurrences
        start, found = _first_occurrence(content, hunk, found, section, previous, copied, path)
        notes.append(DeviationNote(_ordering_note(content, hunk, occurrences, found, start)))
    if hunk.closing_eof and content[start + found.before_spans[0][1] :].strip():
        line = found.first_changed_line + len(_BREAK.findall(content, 0, start))
        notes.append(DeviationNote(_CLOSING_EOF_NOTE.format(line=line)))
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
        difference = first_difference(
            content, hint, details["candidates"][0]["line"], whole_lines=True
        )
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
        # Only an @@ line places added lines before the end.
        raise _PatchError(
            "text_not_found",
            template="eof_after_hint",
            path=path,
            label=hunk.label,
            hint=_hint_text(hunk.hints[-1]),
            details=_candidates(content, hunk.hints[-1]),
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
    poststate = _poststate(window, new, whole_lines=True, eof=hunk.eof or hunk.no_newline)
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
    return not hunk.precise_only and (not new or _poststate(window, new, whole_lines=True) is None)


def _unmarked_additions(
    content: str, start: int, hunk: _Hunk
) -> tuple[_Hunk, FuzzyReplacement, str] | None:
    """Step 8: read unchanged lines between + lines that the file lacks as added lines.

    Such a line is an added line written without its + (Sessions 2026-10: about
    60 hunks, mostly continuation and comment lines of an added block). It is
    added as written, with the leading space of a space-prefixed line, since the
    + was dropped rather than replaced. Empty lines written without a prefix
    between + lines are then added too. The other unchanged and removed lines
    must match once, precisely, from ``start``. A line so read that borders
    no unchanged or removed line on one side must not resemble the file lines on
    that side of the match: there it could be a copy of one of them with errors.
    Not after an earlier failure in the file (``precise_only``): a line the file
    lacks can then be one the failed change would have written.
    Returns the hunk as read, the match in ``content[start:]`` and the note.
    """
    if hunk.precise_only:
        return None
    lines = hunk.lines
    present = {_loose(line) for line in split_text_lines(content)}
    unmarked = [
        i
        for i, (prefix, text) in enumerate(lines)
        if prefix == " "
        and text.strip()
        and _loose(text) not in present
        and _between_additions(lines, i)
    ]
    read = [
        ("+", text if i in hunk.bare else " " + text) if i in unmarked else (prefix, text)
        for i, (prefix, text) in enumerate(lines)
    ]
    blank = [i for i in hunk.bare if not lines[i][1].strip() and _amid_additions(read, i)]
    if not unmarked and not blank:
        return None
    for i in blank:
        read[i] = ("+", lines[i][1])
    old = [i for i, (prefix, _) in enumerate(read) if prefix in " -"]
    if len("".join(read[i][1].strip() for i in old)) < 4:
        return None
    reread = replace(hunk, lines=read, bare=[])
    window = content[start:]
    found = _match_hunk(window, reread)
    if not isinstance(found, FuzzyReplacement):
        return None
    begin, end = found.before_spans[0]
    above = [read[i][1] for i in unmarked if i < old[0]]
    below = [read[i][1] for i in unmarked if i > old[-1]]
    previous = split_text_lines(content[: start + begin])[-len(above) - 2 :] if above else []
    rest = window[end:]
    ending = _BREAK.match(rest)
    following = split_text_lines(rest[ending.end() if ending else 0 :])[: len(below) + 2]
    if any(
        _resembles(text, line)
        for texts, near in ((above, previous), (below, following))
        for text in texts
        for line in near
    ):
        return None
    if not unmarked:
        note = _UNMARKED_BLANK_NOTE if len(blank) == 1 else _UNMARKED_BLANKS_NOTE
        return reread, found, DeviationNote(note.format(count=len(blank)))
    example = lines[unmarked[0]][1].strip()
    example = example if len(example) <= 80 else example[:77] + "..."
    if len(unmarked) == 1:
        note = _UNMARKED_NOTE.format(example=quoted(example))
    else:
        note = _UNMARKED_LINES_NOTE.format(count=len(unmarked), example=quoted(example))
    return reread, found, DeviationNote(note)


def _resembles(text: str, line: str) -> bool:
    """Whether ``text`` can be ``line`` copied with errors."""
    left, right = _loose(text), _loose(line)
    return bool(left and right) and SequenceMatcher(None, left, right).ratio() >= 0.6


def _first_occurrence(
    content: str,
    hunk: _Hunk,
    found: AmbiguousFuzzyMatch,
    section: _Section,
    previous: int | None,
    copied: bool,
    path: object,
) -> tuple[int, FuzzyReplacement]:
    """Step 9: take the first precise match after the hint or the previous change.

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
    """Name the occurrence step 9 changed; ``found`` matched in ``content[start:]``."""
    line = found.first_changed_line + len(_BREAK.findall(content, 0, start))
    if hunk.hints:
        hint = _hint_text(hunk.hints[-1]).strip()
        return _FIRST_AFTER_HINT_NOTE.format(occurrences=occurrences, hint=quoted(hint), line=line)
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
    # A block that only adds lines replaces none; its context lines place it.
    adds_only = not any(prefix == "-" for prefix, _ in hunk.lines)
    return _PatchError(
        "text_not_found",
        template="context_lines_not_found" if adds_only else None,
        path=path,
        label=hunk.label,
        details=details,
    )


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
    line_shift = len(_BREAK.findall(content, 0, offset))
    notes.extend(_deviations(copy_warnings(found, line_shift=line_shift)))
    edited = content[: offset + start] + replacement_text + window[end:]
    written = (offset + start, offset + start + len(replacement_text))
    edited, placed = place_before_decorators(
        content, edited, [((offset + start, offset + end), written)], path
    )
    return edited, notes + _deviations(placed)
