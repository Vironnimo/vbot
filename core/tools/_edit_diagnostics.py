"""Bounded matching failures, candidate excerpts, and ambiguity diagnostics."""

from __future__ import annotations

import re
from bisect import bisect_right
from itertools import pairwise
from typing import Literal

from core.tools._edit_locations import _BREAK, _loose, file_excerpt
from core.tools._fuzzy_differences import first_difference
from core.tools.arguments import (
    split_text_lines,
)
from core.tools.fuzzy_match import (
    AmbiguousFuzzyMatch,
    find_closest_candidates,
    find_closest_passages,
    rewrapped_place,
)
from core.tools.tools import JsonObject

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
    split = _split_copy(content, old) if source == "old_string" else None
    if split is not None:
        return split
    place = rewrapped_place(content, old) if source == "old_string" else None
    # Every word of old_string is there, in order: only whitespace differs.
    places = [place] if place is not None else []
    details: JsonObject = (
        {"candidates": _place_excerpts(content, places)} if places else _candidates(content, old)
    )
    if source == "old_string" and not details["candidates"]:
        # No whole lines are alike: the closest text can be part of a line, or
        # lines that old_string breaks in other places.
        places = find_closest_passages(content, old)
        details["candidates"] = _place_excerpts(content, places)
    if part is not None and not details["candidates"]:
        details["part_of"] = part
    if details["candidates"]:
        if places:
            line, character = _place_start(content, places[0][0])
        else:
            line, character = int(details["candidates"][0]["line"]), 1
        difference = first_difference(
            content,
            old,
            line,
            placed=bool(places),
            whole_lines=source == "patch",
            character=character,
        )
        if difference is not None:
            details["difference"] = {**difference, "source": source}
    return details


def _place_start(content: str, start: int) -> tuple[int, int]:
    """Return the line and character at offset ``start``; character 1 after indentation."""
    starts = [0, *(found.end() for found in _BREAK.finditer(content))]
    number = bisect_right(starts, start)
    line_start = starts[number - 1]
    if not content[line_start:start].strip():
        return number, 1
    return number, start - line_start + 1


def _place_excerpts(content: str, places: list[tuple[int, int]]) -> list[JsonObject]:
    """Show the lines of each (start, end) place, at most as many as a closest-text candidate.

    A place on a line longer than 240 characters shows the 240-character part
    of that line around it, numbered ``line:character``; a place within a part
    shown already is not shown again.
    """
    lines = split_text_lines(content)
    starts = [0, *(found.end() for found in _BREAK.finditer(content))]
    excerpts: list[JsonObject] = []
    # The parts of each line an excerpt shows, as [start, end) character offsets.
    shown: dict[int, list[tuple[int, int]]] = {}
    for start, end in places:
        first = bisect_right(starts, start)
        last = bisect_right(starts, max(start, end - 1))
        index = start - starts[first - 1]
        if any(left <= index < right for left, right in shown.get(first, [])):
            continue
        visible = min(last, first + _PLACE_MAX_LINES - 1)
        excerpt = file_excerpt(lines, first, visible)
        line = lines[first - 1]
        if len(line) > 240:
            left, right = _window(line, index, 0)
            rest = excerpt["text"].split("\n")[1:]
            excerpt["text"] = "\n".join([line[left:right], *rest])
            if left:
                excerpt["character"] = left + 1
            excerpt["truncated"] = right < len(line) or any(
                len(other) > 240 for other in lines[first:visible]
            )
            excerpt["continuations"] = [
                *([{"offset": f"{first}:{right + 1}", "limit": 1}] if right < len(line) else []),
                *excerpt["continuations"][1:],
            ]
            shown.setdefault(first, []).append((left, right))
        else:
            shown[first] = [(0, len(line))]
        if visible < last:
            excerpt["truncated"] = True
            excerpt["continuations"].append(
                {"offset": f"{visible + 1}:1", "limit": min(_PLACE_MAX_LINES, last - visible)}
            )
        excerpts.append(excerpt)
    return excerpts


_PLACE_MAX_LINES = 8
_SPLIT_MAX_LINES = 200
# Parts in order with at most this many non-blank lines between them are one passage.
_SPLIT_MAX_LEFT_OUT = 3


def _split_copy(content: str, old: str) -> JsonObject | None:
    """Name the separate places whose whole lines ``old`` joins, each found once.

    Lines compare with runs of whitespace collapsed; blank lines only separate
    parts. Each part is the longest run of copied lines the file holds together,
    and needs 2 lines with a word of 3 or more characters. ``None`` when a copied line is
    missing, a part occurs more than once, or the parts are one passage that left
    out a few lines.
    """
    wanted = [_loose(line) for line in split_text_lines(old)]
    if len(wanted) > _SPLIT_MAX_LINES:
        return None
    held = [_loose(line) for line in split_text_lines(content)]
    places: dict[str, list[int]] = {}
    for number, line in enumerate(held):
        places.setdefault(line, []).append(number)
    parts: list[JsonObject] = []
    index = 0
    while index < len(wanted):
        if not wanted[index]:
            index += 1
            continue
        runs: dict[int, int] = {}
        for start in places.get(wanted[index], []):
            length = 0
            while (
                index + length < len(wanted)
                and start + length < len(held)
                and wanted[index + length] == held[start + length]
            ):
                length += 1
            # Blank lines at the end of a run belong to no part.
            while not wanted[index + length - 1]:
                length -= 1
            runs[start] = length
        longest = max(runs.values(), default=0)
        starts = [start for start, length in runs.items() if length == longest]
        if len(starts) != 1:
            return None
        # A single line, like a closing line or a common call, occurs by chance.
        if sum(bool(re.search(r"\w{3}", line)) for line in wanted[index : index + longest]) < 2:
            return None
        parts.append(
            {"copy_first": index + 1, "copy_last": index + longest, "first": starts[0] + 1}
        )
        index += longest
    if len(parts) < 2:
        return None
    for part in parts:
        part["last"] = int(part["first"]) + int(part["copy_last"]) - int(part["copy_first"])
    # Parts in order with a few lines between them are one passage that left out
    # lines; the first difference names the first of them.
    if all(
        int(later["first"]) > int(earlier["last"])
        and sum(map(bool, held[int(earlier["last"]) : int(later["first"]) - 1]))
        <= _SPLIT_MAX_LEFT_OUT
        for earlier, later in pairwise(parts)
    ):
        return None
    file_lines = split_text_lines(content)
    candidates = []
    for part in parts:
        first, last = int(part["first"]), int(part["last"])
        candidates.append(
            {"line": first, "text": "\n".join(file_lines[first - 1 : last]), "truncated": False}
        )
    return {"candidates": candidates, "split": parts}


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
    """Return excerpts around each occurrence, and the values an error message names.

    ``match`` was found in ``content[offset:]``. An occurrence on a line of at
    most 240 characters shows that line and its neighbors; one on a longer line
    shows a window of that line around it, numbered ``line:character``, so
    occurrences far apart on one line each show their own text.
    """
    lines = split_text_lines(content)
    if match.spans:
        starts = [0, *(found.end() for found in _BREAK.finditer(content))]
        places = []
        for start, _ in match.spans:
            number = bisect_right(starts, offset + start)
            places.append((number, offset + start - starts[number - 1] + 1))
    else:
        shift = len(_BREAK.findall(content[:offset]))
        places = [(number + shift, 0) for number in match.line_numbers]
    candidates: list[JsonObject] = []
    # The parts of each line an excerpt shows, as [start, end) character offsets.
    shown: dict[int, list[tuple[int, int]]] = {}
    for number, character in places:
        if len(candidates) == 3:
            break
        line = lines[number - 1] if number <= len(lines) else ""
        whole = len(line) <= 240 or not character
        covered = shown.get(number, [])
        if (covered and whole) or any(start < character <= end for start, end in covered):
            continue
        if whole:
            shown[number] = [(0, len(line))]
            candidates.append(file_excerpt(lines, max(1, number - 1), min(len(lines), number + 1)))
            continue
        start, end = _window(line, character - 1, max((end for _, end in covered), default=0))
        shown.setdefault(number, []).append((start, end))
        candidates.append(
            {
                "line": number,
                "character": start + 1,
                "text": line[start:end],
                "truncated": end < len(line),
                "continuations": (
                    [{"offset": f"{number}:{end + 1}", "limit": 1}] if end < len(line) else []
                ),
            }
        )
    long = {number for number, _ in places if number <= len(lines) and len(lines[number - 1]) > 240}
    details: JsonObject = {"occurrences": match.occurrences, "candidates": candidates}
    return details, {"occurrences": match.occurrences, "lines": _place_list(places, long)}


def _window(line: str, index: int, previous: int) -> tuple[int, int]:
    """Return the 240-character part of ``line`` that shows the text at ``index``.

    It starts 80 characters before ``index``, or at the line's start for an
    index in its first 160 characters, but not before ``previous``, the end of
    a part of this line shown already.
    """
    start = 0 if index < 160 else index - 80
    start = max(start, previous)
    end = min(len(line), start + 240)
    return max(previous, end - 240), end


def _place_list(places: list[tuple[int, int]], long: set[int]) -> str:
    """Name the lines of ``places``, as (line, character) pairs, in a message.

    A line holding several places, or longer than an excerpt shows, also names
    the characters where they start, such as ``line 1, characters 57 and 3021``.
    """
    by_line: dict[int, list[int]] = {}
    for number, character in places:
        by_line.setdefault(number, []).append(character)
    named = {
        number
        for number, characters in by_line.items()
        if all(characters) and (len(characters) > 1 or number in long)
    }
    if not named:
        return _line_list(list(by_line))
    parts = []
    for number, characters in list(by_line.items())[:6]:
        if number not in named:
            parts.append(f"line {number}")
            continue
        listed = [str(character) for character in characters[:6]]
        if len(characters) > len(listed):
            text = ", ".join(listed) + ", ..."
        else:
            text = " and ".join([", ".join(listed[:-1]), listed[-1]] if len(listed) > 1 else listed)
        parts.append(f"line {number}, character{'s' if len(characters) > 1 else ''} {text}")
    if len(by_line) > len(parts):
        parts.append("...")
    return "; ".join(parts)
