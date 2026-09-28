"""Match one parsed hunk against current text and apply it.

Line hunks (V4A, unified diffs, SEARCH/REPLACE blocks) delegate to
``fuzzy_match.replace_fuzzy`` with patch-only options (whole lines, EOF
anchoring) and add patch recoveries: read-output gutters, escaped text, surplus
blank context, and already-applied post-states; old text copied with other
errors goes to ``copy_match``. Context lines keep their actual bytes; only
changed lines come from the patch. Text replacements (``old_string``/
``new_string``) match within lines precisely, else through ``copy_match``, and
line insertions go after a line number.
"""

from __future__ import annotations

import re
from dataclasses import replace
from difflib import SequenceMatcher
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

_GUTTER_WARNING = "Removed read-output line-number prefixes before applying the hunk."
_GUTTER_KEPT_NOTE = (
    "{count} added lines start with a number and | like read output, such as {example!r}; "
    "they were written as sent. If they are copied line numbers, remove them with another patch."
)
_ESCAPE_WARNING = "Normalized escaped patch text after the literal text did not match."
_EOF_WARNING = (
    "The lines before *** End of File are not at the end of the file; the hunk was applied "
    "where they are."
)
_WITHIN_LINE_NOTE = "The - line is part of line {line}; only that part of the line was replaced."
_UNMARKED_ADVICE = "in later patches, start every added line with +."
_FIRST_AFTER_HINT_NOTE = (
    "The lines to replace occur {occurrences} times after {hint!r}; the first, at line "
    "{line}, was changed."
)
_FIRST_AFTER_PREVIOUS_NOTE = (
    "The lines to replace occur {occurrences} times; the first after the previous change "
    "in this file, at line {line}, was changed."
)
# A line this similar to the file's line is a copy of it with a typo, not new text.
_NEAR_COPY = 0.8
_BREAK = TEXT_LINE_BREAK
_GUTTER = re.compile(r"^\s*[1-9][0-9]*(?::[1-9][0-9]*)?\|")
_ESCAPE = re.compile(r"\\(n|r|t|\\|\"|')")


def _line_parts(content: str) -> list[tuple[str, str]]:
    parts: list[tuple[str, str]] = []
    start = 0
    for match in _BREAK.finditer(content):
        parts.append((content[start : match.start()], match.group()))
        start = match.end()
    if start < len(content):
        parts.append((content[start:], ""))
    return parts


def _ending(content: str) -> str:
    for ending in ("\r\n", "\n", "\r"):
        if ending in content:
            return ending
    return "\n"


def _hunk_text(hunk: _Hunk, prefixes: str) -> str:
    return "\n".join(text for prefix, text in hunk.lines if prefix in prefixes)


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


def _loose(text: str) -> str:
    return " ".join(text.split())


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


def _not_found(content: str, old: str, *, source: Literal["patch", "old_string"]) -> JsonObject:
    """Return the closest current text and the first line where it differs.

    ``source`` names the argument that holds ``old``, so the report can name it.
    """
    part = _part_of_lines(content, old) if source == "patch" else None
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


def _line_list(numbers: list[int]) -> str:
    shown = [str(number) for number in numbers[:6]]
    if len(numbers) > len(shown):
        shown.append("...")
    return ("line " if len(numbers) == 1 else "lines ") + ", ".join(shown)


def _excerpt(lines: list[str], first: int, last: int) -> JsonObject:
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


def _ambiguity(
    content: str, match: AmbiguousFuzzyMatch, offset: int = 0
) -> tuple[JsonObject, JsonObject]:
    """Return excerpts around each occurrence, and the values an error message names."""
    shift = len(_BREAK.findall(content[:offset]))
    numbers = [number + shift for number in dict.fromkeys(match.line_numbers)]
    lines = split_text_lines(content)
    candidates = [
        _excerpt(lines, max(1, number - 1), min(len(lines), number + 1)) for number in numbers[:3]
    ]
    details: JsonObject = {"occurrences": match.occurrences, "candidates": candidates}
    return details, {"occurrences": match.occurrences, "lines": _line_list(numbers)}


def _match(
    content: str,
    old: str,
    new: str,
    *,
    eof: bool = False,
    first: bool = False,
) -> FuzzyReplacement | AmbiguousFuzzyMatch | None:
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


def _normalize_gutters(hunk: _Hunk) -> _Hunk | None:
    old_lines = [text for prefix, text in hunk.lines if prefix in " -"]
    old = "\n".join(old_lines)
    if strip_line_number_gutters(old) is None:
        return None
    # Locators may mix raw lines with copied gutters. Only a unique whole-line
    # match against the current file authorizes this recovery.
    lines = []
    for prefix, text in hunk.lines:
        stripped = strip_line_number_gutters(text)
        if stripped is not None and re.match(r"^\s*\d+:\d+\|", text):
            return None
        lines.append((prefix, text if stripped is None else stripped))
    return replace(hunk, lines=lines)


def _unescape(text: str, *, replacement_for: str | None = None) -> str:
    values = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"', "'": "'"}

    def decode(match: re.Match[str]) -> str:
        kind = match[1]
        if replacement_for is not None:
            if kind == "n" or match[0] not in replacement_for:
                return match[0]
            if kind in "tr" and not re.fullmatch(r"(?:[ \t]|\\[tr])*", text[: match.start()]):
                return match[0]
        return values[kind]

    return _ESCAPE.sub(decode, text)


def _located_by_gutters(hunk: _Hunk) -> bool:
    """Whether unchanged or removed lines carry read gutters; added lines never locate."""
    return any(_GUTTER.match(text) for prefix, text in hunk.lines if prefix in " -")


def _clean_additions(hunk: _Hunk, path: object) -> tuple[_Hunk, list[str]]:
    # Existing literal gutter-shaped context is authoritative. A run of added
    # lines is copied read output only when every line carries a gutter; a few
    # gutter-shaped lines among others are content, such as N|value data rows.
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


def _replace_within_line(window: str, hunk: _Hunk) -> tuple[str, int] | None:
    """Replace one - line found only inside a longer line, as in-line text.

    Models send part of a line after - and its new text after +. With exactly one
    - line, one + line and no unchanged lines, a single exact occurrence says what
    to replace; anything else keeps whole-line semantics. Returns the new window
    and the 1-based line of the change within it.
    """
    if hunk.eof or hunk.no_newline or [prefix for prefix, _ in hunk.lines] != ["-", "+"]:
        return None
    old, new = hunk.lines[0][1], hunk.lines[1][1]
    start = window.find(old)
    # Overlapping occurrences count too: "aa" occurs twice in "aaa".
    if not old.strip() or start < 0 or window.find(old, start + 1) >= 0:
        return None
    return window[:start] + new + window[start + len(old) :], len(
        _BREAK.findall(window, 0, start)
    ) + 1


def _unmarked_runs(lines: list[tuple[str, str]]) -> list[range]:
    """Return the runs of unchanged lines that sit between two + lines."""
    runs: list[range] = []
    start = 0
    while start < len(lines):
        end = start
        while end < len(lines) and lines[end][0] == " ":
            end += 1
        if 0 < start < end < len(lines) and lines[start - 1][0] == lines[end][0] == "+":
            runs.append(range(start, end))
        start = end + 1
    return runs


def _unmarked_readings(content: str, hunk: _Hunk) -> list[tuple[_Hunk, list[int]]]:
    """Return readings of a parsed hunk that add its unprefixed lines between + lines.

    Models leave the + off some added lines, often a statement's continuation
    lines or a blank line; such a line parses as unchanged. Each reading is the
    hunk with the lines of some such runs added (``_unmarked_texts``), paired
    with their positions: first the runs holding a line the file lacks, then
    also the blank runs, then all runs. A run whose lines the file has stays
    unchanged while a reading without it places the hunk. A blank run stays
    unchanged only between unchanged or removed lines that stay: beyond them,
    the file's own blank line there would place it and move into the added
    block. A reading keeps at least one unchanged or removed line to place it.
    """
    lines = hunk.lines
    if len(hunk.written) != len(lines):
        return []
    runs = _unmarked_runs(lines)
    present = {_loose(line) for line in split_text_lines(content)}

    def rank(run: range) -> int:
        texts = [lines[i][1] for i in run if lines[i][1].strip()]
        if not texts:
            return 1
        return 0 if any(_loose(text) not in present for text in texts) else 2

    ranks = {run: rank(run) for run in runs}
    choices = (
        _with_edge_blanks(lines, runs, [run for run in runs if ranks[run] <= most], ranks)
        for most in range(3)
    )
    readings = []
    for chosen in dict.fromkeys(choices):
        if not chosen:
            continue
        texts = {i: text for run in chosen for i, text in _unmarked_texts(hunk, run).items()}
        read = [("+", texts[i]) if i in texts else line for i, line in enumerate(lines)]
        if any(prefix in " -" for prefix, _ in read):
            reading = replace(hunk, lines=read, written=[], precise_only=True)
            readings.append((reading, list(texts)))
    return readings


def _with_edge_blanks(
    lines: list[tuple[str, str]], runs: list[range], chosen: list[range], ranks: dict[range, int]
) -> tuple[range, ...]:
    """Add to ``chosen`` each blank run that staying unchanged lines do not enclose."""
    if not chosen:
        return ()
    chosen = list(chosen)
    while True:
        taken = {i for run in chosen for i in run}
        edge = [
            run
            for run in runs
            if ranks[run] == 1 and run not in chosen and not _enclosed(lines, run, taken)
        ]
        if not edge:
            return tuple(sorted(chosen, key=lambda run: run.start))
        chosen.extend(edge)


def _enclosed(lines: list[tuple[str, str]], run: range, taken: set[int]) -> bool:
    """Tell whether unchanged or removed lines outside ``run`` and ``taken`` surround it."""
    kept = [
        i
        for i, (prefix, _) in enumerate(lines)
        if prefix in " -" and i not in taken and i not in run
    ]
    return any(i < run.start for i in kept) and any(i >= run.stop for i in kept)


def _unmarked_texts(hunk: _Hunk, run: range) -> dict[int, str]:
    """Return the text each line of an unprefixed run adds, by position in ``hunk``.

    A line the patch wrote without a prefix adds itself as written; one with only
    whitespace adds a blank line. A Model that wrote the space prefix of unchanged
    lines instead of + indents such a line one space deeper than meant. Each line
    keeps its written indentation when that is the indentation of the nearest
    lines around it or a column the line above aligns continuations to.
    Otherwise it loses its first space when the indentation then fits them, or
    when that space makes the number of indenting spaces odd, both counted from
    the line start and beyond the line above's indentation.
    """
    new_text = [(i, text) for i, (prefix, text) in enumerate(hunk.lines) if prefix in " +"]
    after = next((text for i, text in new_text if i >= run.stop and text.strip()), None)
    above = next((text for i, text in reversed(new_text) if i < run.start and text.strip()), None)
    texts: dict[int, str] = {}
    for i in run:
        text = hunk.written[i]
        if not text.strip():
            texts[i] = ""
            continue
        texts[i] = text = _meant_line(text, above, after)
        above = text
    return texts


def _meant_line(text: str, above: str | None, after: str | None) -> str:
    """Return ``text`` as written, or without its first space when that is a stray prefix."""
    if not text.startswith(" "):
        return text
    stripped = text[1:]
    indents = {_leading(line) for line in (above, after) if line is not None}
    columns = _alignment_columns(above) if above is not None else set()

    def fits(line: str) -> bool:
        indent = _leading(line)
        return indent in indents or (not indent.strip(" ") and len(indent) in columns)

    if fits(text):
        return text
    if fits(stripped):
        return stripped
    written, base = _leading(text), _leading(above or "")
    if not (written + base).strip(" ") and len(written) % 2 and (len(written) - len(base)) % 2:
        return stripped
    return text


def _alignment_columns(line: str) -> set[int]:
    """Return the columns a continuation of ``line`` aligns to.

    For each bracket ``line`` leaves open, outside quotes: the column just after
    it, and where the last item inside it starts.
    """
    open_items: list[list[int]] = []  # [column after the bracket, current item start]
    quote = ""
    escaped = False
    for column, character in enumerate(line):
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
            continue
        if open_items and open_items[-1][1] < 0 and not character.isspace():
            open_items[-1][1] = column
        if character in "\"'":
            quote = character
        elif character in "([{":
            open_items.append([column + 1, -1])
        elif character in ")]}" and open_items:
            open_items.pop()
        elif character == "," and open_items:
            open_items[-1][1] = -1
    return {column for item in open_items for column in item if column >= 0}


def _leading(text: str) -> str:
    """Return the whitespace ``text`` starts with."""
    return text[: len(text) - len(text.lstrip())]


def _repeats_neighbors(content: str, edited: str, reading: _Hunk, added: list[int]) -> bool:
    """Tell whether a reading adds lines the file already has beside the change.

    Unchanged or removed lines on both sides of an added run place it, so the
    file cannot hold the run there. With no such line after the run (or before
    it), one side stays open. When the file continues on that side with the
    run's lines or near copies of them, the run was mistyped unchanged text, and
    adding it would repeat those lines.
    """
    kept = [i for i, (prefix, _) in enumerate(reading.lines) if prefix in " -"]
    after = [reading.lines[i][1] for i in added if i > kept[-1]]
    before = [reading.lines[i][1] for i in added if i < kept[0]]
    if not after and not before:
        return False
    old_lines, new_lines = split_text_lines(content), split_text_lines(edited)
    limit = min(len(old_lines), len(new_lines))
    start = 0
    while start < limit and old_lines[start] == new_lines[start]:
        start += 1
    end = 0
    while end < limit - start and old_lines[-1 - end] == new_lines[-1 - end]:
        end += 1
    following = old_lines[len(old_lines) - end :][: len(after) + 1] if after else []
    preceding = old_lines[:start][-len(before) - 1 :] if before else []
    return any(
        _resembles(text, line)
        for texts, lines in ((after, following), (before, preceding))
        for text in texts
        for line in lines
    )


def _resembles(text: str, line: str) -> bool:
    """Tell whether ``text`` is the file ``line`` or a near copy, ignoring spacing."""
    text, line = _loose(text), _loose(line)
    if len(text) < 4 or not re.search(r"\w", text):
        return False
    return text == line or SequenceMatcher(None, text, line).ratio() >= _NEAR_COPY


def _unmarked_note(texts: list[str]) -> str:
    """Name the unprefixed lines a reading added, by the first that is not blank."""
    shown = next((text.strip() for text in texts if text.strip()), "")
    quoted = repr(shown if len(shown) <= 80 else shown[:77] + "...")
    if len(texts) == 1:
        subject = f"The patch line {quoted}" if shown else "A blank patch line"
        return (
            f"{subject} between + lines has no + prefix, but the file does not have it "
            "there, so it was added as a + line. Nothing more is needed for that line; "
            f"{_UNMARKED_ADVICE}"
        )
    subject = f"{len(texts)} patch lines" if shown else f"{len(texts)} blank patch lines"
    example = f"; for example {quoted}" if shown else ""
    return (
        f"{subject} between + lines have no + prefix, but the file does not have them "
        f"there, so they were added as + lines{example}. Nothing more is needed for those "
        f"lines; {_UNMARKED_ADVICE}"
    )


def _beside_additions(lines: list[tuple[str, str]], index: int) -> bool:
    """Tell whether the unchanged line at ``index`` can be an added line missing its +.

    It can when it sits between + lines or right next to one.
    """
    if any(index in run for run in _unmarked_runs(lines)):
        return True
    return any(0 <= i < len(lines) and lines[i][0] == "+" for i in (index - 1, index + 1))


def _hint_text(hint: str) -> str:
    """Show a context hint in one line; a multi-line context block by its first line."""
    first, _, rest = hint.partition("\n")
    return first[:120] + (" ..." if rest or len(first) > 120 else "")


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


def _apply_replacement(content: str, hunk: _Hunk, path: object) -> tuple[str, list[str]]:
    """Replace ``old_string`` text, located precisely, else as a copy with errors."""
    replacement = hunk.replacement
    assert replacement is not None
    replace_all = replacement.replace_all or (replacement.expected or 1) > 1
    attempts: list[tuple[str, str, bool, str | None]] = [
        (replacement.old, replacement.new, False, None)
    ]
    stripped = strip_line_number_gutters(replacement.old)
    if stripped is not None:
        new = strip_line_number_gutters(replacement.new)
        attempts.append((stripped, replacement.new if new is None else new, True, _GUTTER_WARNING))
    if _unescape(replacement.old) != replacement.old:
        attempts.append(
            (
                _unescape(replacement.old),
                _unescape(replacement.new, replacement_for=replacement.old),
                False,
                _ESCAPE_WARNING,
            )
        )
    for old, new, whole_lines, note in attempts:
        found = replace_fuzzy(
            content,
            old,
            new,
            replace_all=replace_all,
            whole_lines=whole_lines,
            typographic=True,
        )
        if found is None:
            continue
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
        if replacement.expected is not None and found.replacements != replacement.expected:
            numbers = [len(_BREAK.findall(content[:start])) + 1 for start, _ in found.before_spans]
            raise _PatchError(
                "occurrence_mismatch",
                template="replacement_count",
                path=path,
                label=hunk.label,
                occurrences=found.replacements,
                lines=_line_list(numbers),
                expected=replacement.expected,
            )
        return found.new_content, [note] if note else []
    details = _not_found(content, replacement.old, source="old_string")
    present = None
    if replacement.new.strip() and replacement.new != replacement.old:
        present = replace_fuzzy(
            content,
            replacement.new,
            replacement.new,
            replace_all=True,
            typographic=True,
        )
        # Text the old text already holds proves nothing about an earlier edit.
        if isinstance(present, FuzzyReplacement) and replacement.new.strip() not in (
            replacement.old
        ):
            details["already_present"] = present.first_changed_line
    if not replace_all and present is None and not hunk.precise_only:
        copied = replace_copied(content, replacement.old, replacement.new)
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


def _insert_at_line(content: str, hunk: _Hunk, path: object) -> str:
    """Insert the hunk's lines after 1-based line ``insert_line`` (0 = file start)."""
    assert hunk.insert_line is not None
    parts = _line_parts(content)
    if hunk.insert_line > len(parts):
        raise _PatchError(
            "text_not_found",
            template="insert_past_end",
            path=path,
            label=hunk.label,
            line=hunk.insert_line,
            count=len(parts),
        )
    ending = _ending(content)
    texts = [text for _, text in hunk.lines]
    if hunk.insert_line == len(parts) and parts and parts[-1][1] == "":
        # After a last line without a line break: the file keeps ending without one.
        return content + ending + ending.join(texts)
    position = sum(len(text) + len(end) for text, end in parts[: hunk.insert_line])
    return content[:position] + "".join(text + ending for text in texts) + content[position:]


def _apply_hunks(
    content: str, hunks: list[_Hunk], path: object, previous: int | None = None
) -> tuple[str, list[str], int | None]:
    """Apply one file's hunks in order and return the text, the notes and the change end.

    Each hunk sees the text the hunks before it produced. ``previous``, and the
    returned change end, is where the line holding the file's last change starts;
    it orders the occurrences of lines a later hunk matches.
    """
    notes: list[str] = []
    for hunk in hunks:
        edited, placed = _apply_hunk(content, hunk, path, previous)
        if edited != content:
            previous = _change_end_line(content, edited)
        content = edited
        notes.extend(placed)
    return content, notes, previous


def _change_end_line(before: str, after: str) -> int:
    """Return where the line holding the end of the change from ``before`` starts."""
    prefix = len(commonprefix((before, after)))
    limit = min(len(before), len(after)) - prefix
    suffix = len(commonprefix((before[::-1][:limit], after[::-1][:limit])))
    end = max(prefix, len(after) - suffix - 1)
    return max(after.rfind("\n", 0, end), after.rfind("\r", 0, end)) + 1


def _apply_hunk(
    content: str, hunk: _Hunk, path: object, previous: int | None = None
) -> tuple[str, list[str]]:
    """Apply one hunk; ``previous`` is where the line of the file's last change starts."""
    if hunk.replacement is not None:
        return _apply_replacement(content, hunk, path)
    if hunk.insert_line is not None:
        return _insert_at_line(content, hunk, path), []
    if not any(prefix in "+-" for prefix, _ in hunk.lines):
        return content, []
    parsed = hunk
    hunk, warnings = _clean_additions(hunk, path)
    copied = False
    offset = 0
    hint_start = 0
    # A repeated @@ line is read from its first occurrence, as Codex does. The
    # lines to replace must then occur once after it: that one place is right
    # whichever occurrence was meant. Otherwise this error is raised.
    repeated_hint: _PatchError | None = None
    for hint in hunk.hints:
        found = _match(content[offset:], hint, hint)
        if isinstance(found, AmbiguousFuzzyMatch):
            details, values = _ambiguity(content, found, offset)
            repeated_hint = repeated_hint or _PatchError(
                "ambiguous_context",
                path=path,
                label=hunk.label,
                hint=_hint_text(hint),
                details=details,
                **values,
            )
            found = _match(content[offset:], hint, hint, first=True)
        if not isinstance(found, FuzzyReplacement):
            raise _PatchError(
                "context_not_found",
                path=path,
                label=hunk.label,
                hint=_hint_text(hint),
                details=_candidates(content, hint),
            )
        hint_start = offset + found.before_spans[0][0]
        offset += found.before_spans[0][1]
        ending = _BREAK.match(content, offset)
        if ending:
            offset = ending.end()
    window = content[offset:]
    old, new = _hunk_text(hunk, " -"), _hunk_text(hunk, " +")
    if not any(prefix in " -" for prefix, _ in hunk.lines):
        if repeated_hint is not None:
            raise repeated_hint
        position = offset if hunk.hints else len(content)
        if hunk.no_newline and position != len(content):
            raise _PatchError(
                "invalid_patch", template="no_newline_position", path=path, label=hunk.label
            )
        if hunk.eof and position != len(content):
            raise _PatchError(
                "text_not_found",
                template="eof_not_found",
                path=path,
                label=hunk.label,
                details=_candidates(content, new),
            )
        inserted = new.replace("\n", _ending(content))
        if not hunk.no_newline:
            inserted += _ending(content)
        # A hint ties the post-state to an insertion point. A suffix alone
        # cannot distinguish a retry from an intentional repeated append.
        if hunk.hints and inserted.strip() and content[position:].startswith(inserted):
            return content, warnings
        separator = (
            _ending(content)
            if position and not _BREAK.search(content[position - 1 : position])
            else ""
        )
        return content[:position] + separator + inserted + content[position:], warnings
    if hunk.hints:
        # Models sometimes repeat the final hint as the first context/removal
        # line. Include that line in the search without weakening earlier hints.
        offset = hint_start
        window = content[offset:]
    found = _match(window, old, new, eof=hunk.eof)
    normalized = _normalize_gutters(hunk)
    if found is None and normalized is not None:
        candidate_old, candidate_new = _hunk_text(normalized, " -"), _hunk_text(normalized, " +")
        candidate_match = _match(window, candidate_old, candidate_new, eof=hunk.eof)
        hunk, old, new, found = normalized, candidate_old, candidate_new, candidate_match
        warnings.append(_GUTTER_WARNING)
    if found is None and _located_by_gutters(hunk):
        raise _PatchError(
            "line_numbered_content",
            path=path,
            label=hunk.label,
            details=_candidates(content, old),
        )
    if found is None and _unescape(old) != old:
        escaped = replace(
            hunk,
            lines=[
                (p, _unescape(t, replacement_for=old if p == "+" else None)) for p, t in hunk.lines
            ],
        )
        # Unescaping line separators changes the hunk's physical line structure.
        escaped.lines = [(p, line) for p, text in escaped.lines for line in text.split("\n")]
        candidate_old, candidate_new = _hunk_text(escaped, " -"), _hunk_text(escaped, " +")
        candidate_match = _match(window, candidate_old, candidate_new, eof=hunk.eof)
        if candidate_match is not None:
            hunk, old, new, found = escaped, candidate_old, candidate_new, candidate_match
            warnings.append(_ESCAPE_WARNING)
    if found is None:
        # Ignore surplus blank context at a hunk boundary only after the full
        # locator misses. Removed blank lines remain part of the operation.
        lines = list(hunk.lines)
        while lines and lines[0][0] == " " and not lines[0][1].strip():
            lines.pop(0)
        while lines and lines[-1][0] == " " and not lines[-1][1].strip():
            lines.pop()
        if lines != hunk.lines and any(p in " -" for p, _ in lines):
            trimmed = replace(hunk, lines=lines)
            candidate_old, candidate_new = _hunk_text(trimmed, " -"), _hunk_text(trimmed, " +")
            candidate_match = _match(window, candidate_old, candidate_new, eof=hunk.eof)
            if candidate_match is not None:
                hunk, old, new, found = trimmed, candidate_old, candidate_new, candidate_match
    if found is None:
        context = "".join(text.strip() for prefix, text in hunk.lines if prefix == " ")
        poststate = _match(window, new, new, eof=hunk.eof or hunk.no_newline) if new else None
        if (
            repeated_hint is None
            and (len(context) >= 4 or _inline_context_identifies(window, old, new))
            and isinstance(poststate, FuzzyReplacement)
            and (not hunk.no_newline or poststate.before_spans[0][1] == len(window))
        ):
            return content, warnings
        within = _replace_within_line(window, hunk)
        if within is not None:
            edited, line = within
            line += len(_BREAK.findall(content, 0, offset))
            return content[:offset] + edited, [*warnings, _WITHIN_LINE_NOTE.format(line=line)]
        # An exact post-state elsewhere does not prove this target is satisfied.
        # Do not let approximate matching choose it (or a similar other target)
        # after the independent locator above failed to establish that fact.
        # Old text copied with errors is placed and merged by ``copy_match``.
        if not hunk.precise_only and (not new or _match(window, new, new) is None):
            found, copied = match_copied_edit(window, hunk.lines, at_eof=hunk.eof), True
    if repeated_hint is not None and (
        isinstance(found, AmbiguousFuzzyMatch) or (copied and found is not None)
    ):
        raise repeated_hint
    if isinstance(found, AmbiguousFuzzyMatch):
        # As in Codex, an @@ line or the file's previous change orders the
        # occurrences, and the first after it is meant (Sessions: 60 of 61 such
        # hunks). A hunk with neither, or a copy with errors, must match once.
        start = offset if hunk.hints else previous
        first = (
            _match(content[start:], old, new, eof=hunk.eof, first=True)
            if start is not None and not copied
            else None
        )
        if start is None or not isinstance(first, FuzzyReplacement):
            details, values = _ambiguity(content, found, offset)
            raise _PatchError(
                "ambiguous_match",
                template="ambiguous_patch_copy" if copied else None,
                path=path,
                label=hunk.label,
                details=details,
                **values,
            )
        line = first.first_changed_line + len(_BREAK.findall(content, 0, start))
        warnings.append(
            _FIRST_AFTER_HINT_NOTE.format(
                occurrences=found.occurrences,
                hint=_hint_text(hunk.hints[-1]).strip(),
                line=line,
            )
            if hunk.hints
            else _FIRST_AFTER_PREVIOUS_NOTE.format(occurrences=found.occurrences, line=line)
        )
        offset, window, found = start, content[start:], first
    if found is None:
        # Only the lines between + lines are re-read, and only a precise match
        # places the reading: the file must hold the lines around them adjacent.
        for reading, added in _unmarked_readings(content, parsed):
            try:
                edited, notes = _apply_hunk(content, reading, path, previous)
            except _PatchError:
                continue
            if edited != content and not _repeats_neighbors(content, edited, reading, added):
                return edited, [*notes, _unmarked_note([reading.lines[i][1] for i in added])]
    if found is None and hunk.eof:
        # The marker only claims where the lines are; the lines alone may still place
        # them. Text found elsewhere never proves the change was made earlier.
        try:
            edited, placed = _apply_hunk(content, replace(hunk, eof=False), path, previous)
        except _PatchError:
            pass
        else:
            if edited != content:
                return edited, [*warnings, _EOF_WARNING, *placed]
    if found is None:
        if _located_by_gutters(hunk):
            raise _PatchError(
                "line_numbered_content",
                path=path,
                label=hunk.label,
                details=_candidates(content, old),
            )
        details = _not_found(content, old, source="patch")
        difference = details.get("difference")
        if difference:
            # Number the hunk's unchanged and removed lines as the report counts them.
            numbered = [i for i, (prefix, _) in enumerate(hunk.lines) if prefix in " -"]
            position = difference["copy_line"] - 1
            if position < len(numbered) and hunk.lines[numbered[position]][0] == " ":
                difference["unprefixed"] = _beside_additions(hunk.lines, numbered[position])
        elif not details["candidates"] and "part_of" not in details:
            # Nothing resembles the lines as a whole; a line the file has nowhere
            # is then the one to fix, often a new line written without +.
            present = {_loose(line) for line in split_text_lines(content)}
            absent = next(
                (
                    i
                    for i, (prefix, text) in enumerate(hunk.lines)
                    if prefix in " -" and text.strip() and _loose(text) not in present
                ),
                None,
            )
            if absent is not None:
                prefix, text = hunk.lines[absent]
                details["absent"] = {
                    "text": text.strip(),
                    "removed": prefix == "-",
                    "beside_additions": prefix == " " and _beside_additions(hunk.lines, absent),
                }
        raise _PatchError("text_not_found", path=path, label=hunk.label, details=details)
    if [t for p, t in hunk.lines if p in " -"] == [t for p, t in hunk.lines if p in " +"]:
        return content, warnings
    start, end = found.before_spans[0]
    after_start, after_end = found.after_spans[0]
    actual = _line_parts(window[start:end])
    if found.strategy != "exact" and any(
        token in old and token in new and token not in window[start:end]
        for token in ('\\"', "\\'", "\\\\")
    ):
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
            output.append((replacement_line, _ending(content)))
            last_output_prefix = prefix
        elif prefix == "-":
            removed_lines.append((actual[old_index][0], locator_line))
        if prefix in " -":
            old_index += 1
        if prefix in " +":
            new_index += 1
    if output:
        output = [(text, ending or _ending(content)) for text, ending in output[:-1]] + output[-1:]
        if last_output_prefix == "+" or hunk.no_newline:
            output[-1] = (output[-1][0], "" if hunk.no_newline else added_ending)
    replacement_text = "".join(text + ending for text, ending in output)
    warnings.extend(copy_warnings(found, line_shift=len(_BREAK.findall(content, 0, offset))))
    return content[: offset + start] + replacement_text + window[end:], warnings
