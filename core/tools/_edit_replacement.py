"""Ordered interpretation of old/new text replacements."""

from __future__ import annotations

import json
import re
from dataclasses import replace

from core.tools._decorated_insert import place_before_decorators
from core.tools._edit_diagnostics import _ambiguity, _not_found
from core.tools._edit_locations import (
    _BREAK,
    _GUTTER_WARNING,
    AlreadyApplied,
    DeviationNote,
    _covers_lines,
    _deviations,
    _Found,
    _new_text_line,
    _poststate,
)
from core.tools._fuzzy_text import _inside_crlf
from core.tools._patch_syntax import _Hunk, _PatchError, _Replacement, quoted
from core.tools.arguments import (
    TEXT_LINE_BREAK,
    split_text_lines,
    strip_line_number_gutters,
)
from core.tools.copy_match import copy_warnings, replace_copied
from core.tools.fuzzy_match import (
    AmbiguousFuzzyMatch,
    FuzzyReplacement,
    _replaced,
    dominant_line_ending,
    replace_fuzzy,
)
from core.tools.tools import JsonObject

# --- Text replacements -----------------------------------------------------------------


def _apply_replacement(content: str, hunk: _Hunk, path: object) -> tuple[str, list[str]]:
    """Replace ``old_string`` text, located precisely, else as a copy with errors."""
    replacement = hunk.replacement
    assert replacement is not None
    old, new, replace_all = replacement.old, replacement.new, replacement.replace_all
    notes: list[str] = []
    found = replace_fuzzy(
        content, old, new, replace_all=replace_all, typographic=True, rewrapped=True
    )
    if isinstance(found, FuzzyReplacement) and found.strategy == "rewrapped":
        return _rewrapped(content, found, hunk, path)
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
    if found is None:
        found, interpreted, note = _replace_unicode_escapes(content, replacement)
        if isinstance(found, FuzzyReplacement):
            hunk = replace(hunk, replacement=interpreted)
            notes.append(DeviationNote(note))
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
        _refuse_join(content, found, hunk, path)
        found, kept = _keep_final_break(content, found, hunk)
        edited, placed = _keep_decorated(content, found, path)
        return edited, notes + kept + _joined_lines(content, found) + placed
    if not replace_all:
        applied = _new_text_in_place(content, old, new)
        if applied is not None:
            return content, [applied]
    details = _not_found(content, old, source="old_string")
    present = None
    if new.strip() and new != old:
        # New text that is only part of a line proves no earlier edit (_covers_lines).
        present = _new_text_line(content, new)
        # Text the old text already holds proves nothing about an earlier edit.
        if present is not None and new.strip() not in old:
            details["already_present"] = present
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
            _refuse_join(content, copied, hunk, path)
            copied, kept = _keep_final_break(content, copied, hunk)
            edited, placed = _keep_decorated(content, copied, path)
            return edited, [
                *_deviations(copy_warnings(copied)),
                *kept,
                *_joined_lines(content, copied),
                *placed,
            ]
    raise _PatchError(
        "text_not_found",
        template="old_text_not_found",
        path=path,
        label=hunk.label,
        details=details,
    )


# A run of \uXXXX escapes after an even number of backslashes: a backslash
# written as \\ ends no escape.
_ESCAPE_RUN = re.compile(r"(?<!\\)((?:\\\\)*)((?:\\u[0-9a-fA-F]{4})+)")
# Keep a surrogate pair together: neither half is a Unicode character by itself.
_ESCAPE_CHARACTER = re.compile(
    r"\\u[dD][89aAbB][0-9a-fA-F]{2}\\u[dD][c-fC-F][0-9a-fA-F]{2}|\\u[0-9a-fA-F]{4}"
)
# Only escapes of characters beyond ASCII and its controls are read as those
# characters: an Agent writes ü as ü, not a quote or a line break.
_FIRST_ESCAPED = 0xA0
_ESCAPES_SHOWN = 3


def _escaped_characters(run: str) -> str | None:
    """Return the characters a run of \\u escapes stands for, ``None`` if it is no such run."""
    try:
        characters = str(json.loads(f'"{run}"'))
    except ValueError:
        return None
    if any(
        ord(character) < _FIRST_ESCAPED or 0xD800 <= ord(character) <= 0xDFFF
        for character in characters
    ):
        return None
    return characters


def _unicode_escapes(text: str) -> list[tuple[int, int, str, str]]:
    """Return each non-ASCII escape's span, spelling and character."""
    found = []
    for run in _ESCAPE_RUN.finditer(text):
        for match in _ESCAPE_CHARACTER.finditer(run[2]):
            characters = _escaped_characters(match[0])
            if characters is not None:
                start = run.start(2) + match.start()
                found.append((start, start + len(match[0]), match[0], characters))
    return found


def _with_characters(text: str, escaped: dict[str, str]) -> str:
    """Read only escape spellings whose character form the matched old text proves."""
    for start, end, spelling, _ in reversed(_unicode_escapes(text)):
        if spelling in escaped:
            text = text[:start] + escaped[spelling] + text[end:]
    return text


def _replace_unicode_escapes(
    content: str, replacement: _Replacement
) -> tuple[_Found, _Replacement, str]:
    """Match literal and character escape spellings without choosing between targets."""
    parts = _unicode_escapes(replacement.old)
    if not parts:
        return None, replacement, ""
    pattern: list[str] = []
    end = 0
    for start, stop, spelling, characters in parts:
        pattern.extend(
            (
                re.escape(replacement.old[end:start]),
                f"({re.escape(spelling)}|{re.escape(characters)})",
            )
        )
        end = stop
    pattern.append(re.escape(replacement.old[end:]))
    # The alternatives begin with different characters (backslash or non-ASCII),
    # so a passage has one reading even when it contains many escape spellings.
    candidates: dict[tuple[int, int], tuple[str, dict[str, str], dict[str, str]]] = {}
    for match in re.finditer("(?=(" + "".join(pattern) + "))", content):
        if _inside_crlf(content, match.start(1)) or _inside_crlf(content, match.end(1)):
            continue
        changed = {}
        literal = set()
        for actual, (_, _, spelling, characters) in zip(match.groups()[1:], parts, strict=True):
            if actual == spelling:
                literal.add(spelling)
            else:
                changed[spelling] = characters
        # The same escape can be literal at one position and a character at
        # another. That proves no single reinterpretation of new_string.
        evidence = {key: value for key, value in changed.items() if key not in literal}
        candidates[match.span(1)] = (match[1], evidence, changed)
    # Retain the established precise whitespace/typography tolerance for a copy
    # whose escape spellings all stand for characters.
    decoded = {spelling: characters for _, _, spelling, characters in parts}
    old = _with_characters(replacement.old, decoded)
    found = replace_fuzzy(content, old, old, replace_all=True, typographic=True)
    if isinstance(found, FuzzyReplacement):
        for span in found.before_spans:
            candidates.setdefault(span, (old, decoded, decoded))
    spans = sorted(candidates)
    if not spans:
        return None, replacement, ""
    if len(spans) > 1 and not replacement.replace_all:
        return (
            AmbiguousFuzzyMatch(
                len(spans),
                [len(TEXT_LINE_BREAK.findall(content[:start])) + 1 for start, _ in spans],
                tuple(spans),
            ),
            replacement,
            "",
        )
    selected: list[tuple[int, int]] = []
    for span in spans:
        if not selected or span[0] >= selected[-1][1]:
            selected.append(span)
    # A new escape is read as a character only if every changed place proves
    # that spelling. New escape types and matching literal spellings stay literal.
    escaped = dict(candidates[selected[0]][1])
    matched = {}
    for span in selected:
        matched.update(candidates[span][2])
    for span in selected[1:]:
        escaped = {key: value for key, value in escaped.items() if key in candidates[span][1]}
    interpreted = replace(
        replacement,
        old=candidates[selected[0]][0],
        new=_with_characters(replacement.new, escaped),
    )
    written = {
        spelling: escaped[spelling]
        for _, _, spelling, _ in _unicode_escapes(replacement.new)
        if spelling in escaped
    }
    return (
        _replaced(
            content,
            selected,
            TEXT_LINE_BREAK.sub("\n", interpreted.new),
            "unicode_escapes",
            reindent=True,
            old_string_lf=TEXT_LINE_BREAK.sub("\n", interpreted.old),
            file_ending=dominant_line_ending(content),
        ),
        interpreted,
        _escapes_note(matched, written),
    )


def _escapes_note(escaped: dict[str, str], written: dict[str, str]) -> str:
    """Say which escapes of old_string the file has as characters, and how new_string was read."""
    shown = list(escaped.items())[:_ESCAPES_SHOWN]
    escapes = ", ".join(escape for escape, _ in shown)
    characters = ", ".join(characters for _, characters in shown)
    more = " and others" if len(escaped) > len(shown) else ""
    note = f"old_string has {escapes}{more} where the file has {characters}{more}. "
    if not written:
        return note + "new_string was kept as sent."
    names = ", ".join(list(written)[:_ESCAPES_SHOWN])
    extra = " and others" if len(written) > _ESCAPES_SHOWN else ""
    return note + f"Only {names}{extra} in new_string were written as characters."


def _new_text_in_place(content: str, old: str, new: str) -> AlreadyApplied | None:
    """Say where ``new`` stands when the file already holds this edit's result.

    ``old`` is not in the file (the precise steps found nothing). The result
    holds when ``new`` occurs exactly once, as whole lines (``_covers_lines``),
    and not inside ``old``, both share a whole non-blank line at their start or
    end, so they name one passage, and no copy of ``old`` with errors stands
    anywhere else. Then the edit has nothing left to do; otherwise ``None``.
    """
    if not new.strip() or new.strip() in old or not _share_an_edge_line(old, new):
        return None
    present = _poststate(content, new)
    if not isinstance(present, FuzzyReplacement):
        return None
    start, end = present.before_spans[0]
    if not _covers_lines(content, new, start, end):
        return None
    copied = replace_copied(content, old, new)
    if isinstance(copied, AmbiguousFuzzyMatch):
        return None
    if copied is not None and any(
        copy_end <= start or copy_start >= end for copy_start, copy_end in copied.before_spans
    ):
        return None
    first, last = present.first_changed_line, present.last_changed_line
    lines = f"line {first}" if first == last else f"lines {first}-{last}"
    return AlreadyApplied(_ALREADY_APPLIED_NOTE.format(lines=lines))


_ALREADY_APPLIED_NOTE = (
    "new_string already stands at {lines} and old_string is not in the file; nothing to change."
)


def _share_an_edge_line(old: str, new: str) -> bool:
    """Whether ``old`` and ``new`` start or end with the same line that holds a word.

    A line of punctuation alone, such as ``}``, ends many passages.
    """
    old_lines = [line.strip() for line in split_text_lines(old) if line.strip()]
    new_lines = [line.strip() for line in split_text_lines(new) if line.strip()]
    if len(old_lines) < 2 or len(new_lines) < 2:
        return False
    return any(
        old_line == new_line and _WORD.search(old_line)
        for old_line, new_line in ((old_lines[0], new_lines[0]), (old_lines[-1], new_lines[-1]))
    )


_WORD = re.compile(r"\w")


def _keep_decorated(content: str, found: FuzzyReplacement, path: object) -> tuple[str, list[str]]:
    """Return the replaced text with added lines kept off a definition's decorators."""
    spans = list(zip(found.before_spans, found.after_spans, strict=True))
    edited, notes = place_before_decorators(content, found.new_content, spans, path)
    return edited, _deviations(notes)


def _rewrapped(
    content: str, found: FuzzyReplacement, hunk: _Hunk, path: object
) -> tuple[str, list[str]]:
    """Write a replacement whose old text breaks its lines in other places than the file.

    The note names the lines that held the old text: the written text follows
    the call's line breaks, not the file's, so the result shows it.
    """
    places = []
    for start, end in found.before_spans:
        text = content[start:end]
        first = len(_BREAK.findall(content, 0, start + len(text) - len(text.lstrip()))) + 1
        last = len(_BREAK.findall(content, 0, start + len(text.rstrip()) - 1)) + 1
        places.append(str(first) if first == last else f"{first}-{last}")
    single = len(places) == 1 and "-" not in places[0]
    lines = ("line " if single else "lines ") + ", ".join(places)
    found, kept = _keep_final_break(content, found, hunk)
    edited, placed = _keep_decorated(content, found, path)
    note = DeviationNote(_REWRAPPED_NOTE.format(lines=lines))
    return edited, [note, *kept, *_joined_lines(content, found), *placed]


_REWRAPPED_NOTE = (
    "old_string matched {lines}, where the file breaks the text in other places; "
    "new_string was written with its own line breaks."
)


def _refuse_join(content: str, found: FuzzyReplacement, hunk: _Hunk, path: object) -> None:
    """Refuse a replacement whose only change drops the line break ending its old text.

    Its next line would continue the matched text's last line, or a blank line
    after it would go. In the recorded Sessions (2026-10), 9 such edits in 7
    calls joined code lines or deleted blank lines, none on purpose. At the end
    of the file it only removes the final line break, which can be meant.
    """
    replacement = hunk.replacement
    assert replacement is not None
    old, new = replacement.old, replacement.new
    ending = next((e for e in ("\r\n", "\n", "\r") if old.endswith(e)), None)
    # A new text that still ends a line, such as "\n" for "\n\n", deletes a blank line.
    if ending is None or not new or new.endswith(("\n", "\r")) or new != old[: -len(ending)]:
        return
    end = next(
        (
            end
            for _, end in found.before_spans
            if content[end - 1 : end] in ("\n", "\r") and end < len(content)
        ),
        None,
    )
    if end is None:
        return
    stop = end - 2 if content[end - 2 : end] == "\r\n" else end - 1
    start = max(content.rfind("\n", 0, stop), content.rfind("\r", 0, stop)) + 1
    after = _BREAK.search(content, end)
    line = content[start:stop].strip()
    following = content[end : after.start() if after else len(content)].strip()
    if len(line) > _JOINED_SIDE_CHARACTERS:
        line = "..." + line[-_JOINED_SIDE_CHARACTERS:]
    if len(following) > _JOINED_SIDE_CHARACTERS:
        following = following[:_JOINED_SIDE_CHARACTERS] + "..."
    details: JsonObject = {"line": line, "next": following}
    raise _PatchError(
        "line_join",
        template="line_join" if following else "blank_line_join",
        path=path,
        label=hunk.label,
        details=details,
        line=quoted(line),
        next=quoted(following),
    )


def _keep_final_break(
    content: str, found: FuzzyReplacement, hunk: _Hunk
) -> tuple[FuzzyReplacement, list[str]]:
    """Keep the line break a replacement dropped before more lines of the file.

    ``new_string`` lacks the line break that ends ``old_string``, and at one of
    the places its last line, holding text, would continue with the file's next
    line: a line of text joins it, a blank line disappears into it. Neither was
    meant in the recorded edits (2026-10): such a ``new_string`` ends where the
    line it replaces ends, the break was lost in the copy. It gets the file's
    line break then, at every place where the replaced text ended with one, and
    a note says so. At the end of the file nothing follows: it stays as sent.
    """
    replacement = hunk.replacement
    assert replacement is not None
    old, new = replacement.old, replacement.new
    if not old.endswith(("\n", "\r")) or not new or new.endswith(("\n", "\r")):
        return found, []
    text = found.new_content
    # Places whose replaced text ended a line while the written text does not.
    open_ends = [
        (old_end, end)
        for (_, old_end), (start, end) in zip(found.before_spans, found.after_spans, strict=True)
        if end > start
        and content[old_end - 1 : old_end] in ("\n", "\r")
        and text[end - 1] not in "\r\n"
    ]

    def joins_next_line(end: int) -> bool:
        first = max(text.rfind("\n", 0, end), text.rfind("\r", 0, end)) + 1
        return bool(text[first:end].strip()) and end < len(text)

    if not any(joins_next_line(end) for _, end in open_ends):
        return found, []
    pieces: list[str] = []
    spans: list[tuple[int, int]] = []
    shift = done = 0
    breaks = dict(open_ends)
    for (_, old_end), (start, end) in zip(found.before_spans, found.after_spans, strict=True):
        pieces.append(text[done:end])
        moved = start + shift
        if breaks.get(old_end) == end:
            ending = "\r\n" if content[old_end - 2 : old_end] == "\r\n" else content[old_end - 1]
            pieces.append(ending)
            shift += len(ending)
        spans.append((moved, end + shift))
        done = end
    pieces.append(text[done:])
    kept = replace(found, new_content="".join(pieces), after_spans=tuple(spans))
    return kept, [DeviationNote(_KEPT_BREAK_NOTE)]


_KEPT_BREAK_NOTE = (
    "new_string does not end with a line break as old_string does; it was written with one, "
    "so the next line stays a line of its own. To join or remove the next line, include it "
    "in old_string."
)


def _joined_lines(content: str, found: FuzzyReplacement) -> list[str]:
    """Note replacements that dropped the line break ending the text they replaced.

    The line after such a replacement now continues its last line; a line break
    or the end of the file there joins nothing.
    """
    text = found.new_content
    joined = [
        end
        for (_, old_end), (start, end) in zip(found.before_spans, found.after_spans, strict=True)
        if end > start
        and content[old_end - 1 : old_end] in ("\n", "\r")
        and text[end - 1] not in "\r\n"
        and text[end : end + 1] not in ("", "\n", "\r")
    ]
    if not joined:
        return []
    at = joined[0]
    first = max(text.rfind("\n", 0, at), text.rfind("\r", 0, at)) + 1
    after = _BREAK.search(text, at)
    # The joined line around the join: the end of the new text, then the next line.
    head = text[first:at].lstrip()
    tail = text[at : after.start() if after else len(text)].rstrip()
    if len(head) > _JOINED_SIDE_CHARACTERS:
        head = "..." + head[-_JOINED_SIDE_CHARACTERS:]
    if len(tail) > _JOINED_SIDE_CHARACTERS:
        tail = tail[:_JOINED_SIDE_CHARACTERS] + "..."
    places = f" at {len(joined)} places, such as" if len(joined) > 1 else ""
    return [_JOINED_NOTE.format(places=places, line=head + tail)]


_JOINED_SIDE_CHARACTERS = 100
_JOINED_NOTE = (
    "new_string does not end with a line break as old_string does, so the next line "
    "now continues its last line{places}: {line}"
)
