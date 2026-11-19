"""Ordering, pagination and readable text of search_files results.

Pages count results: matching lines (or matches with ``-o``) for line output,
files for file and count output, entries for listings. A page ends at its
``limit`` or at the output byte budget, never inside a result, and the next
page starts exactly after the last result shown.

Line output names each file once, like ripgrep's ``--heading``: the file's
label on its own line, then its lines as ``N:text`` (matches) and ``N-text``
(context), and an empty line before the next file's label.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.tools._search_execution import decode_event_text
from core.tools._search_query import LIST_MODES, MAX_OFFSET, SearchQuery
from core.tools.search import MAX_OUTPUT_BYTES, display_search_path

MAX_LINE_CHARS = 1000
# Room kept free below the byte budget for the result's other fields.
_RESERVED_BYTES = 2048


@dataclass
class Entry:
    """One file or directory a search found."""

    path: Path
    shown_path: str
    scope: int = 0
    native: bytes = b""
    count: int = 0
    times: tuple[float, float, float] = (0.0, 0.0, 0.0)

    @property
    def label(self) -> str:
        return _quote_path(self.shown_path)


def _quote_path(value: str) -> str:
    if re.search(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\ud800-\udfff]", value):
        return json.dumps(value)
    return value


def path_label(path: Path, cwd: Path, *, directory: bool = False) -> str:
    """Render a path as the Agent passes it back: relative to cwd, absolute outside."""
    value = display_search_path(path, cwd=cwd) + ("/" if directory else "")
    return _quote_path(value)


def _path_key(entry: Entry) -> tuple[tuple[str, str], ...]:
    return tuple((part.casefold(), part) for part in entry.shown_path.split("/"))


def order(entries: list[Entry], query: SearchQuery) -> None:
    """Sort entries in place: by path for content results, newest first for listings."""
    key, reverse = query.sort or (
        ("path", False) if query.searches_contents else ("modified", True)
    )
    if key in {"path", "none"}:
        entries.sort(key=_path_key, reverse=reverse)
        return
    _stat_times(entries)
    index = {"modified": 0, "accessed": 1, "created": 2}[key]
    entries.sort(key=_path_key)
    entries.sort(key=lambda entry: entry.times[index], reverse=reverse)


def _stat_times(entries: list[Entry]) -> None:
    def times(entry: Entry) -> tuple[float, float, float]:
        try:
            status = os.stat(entry.path)
        except OSError:
            return (0.0, 0.0, 0.0)
        created = getattr(status, "st_birthtime", status.st_ctime)
        return (status.st_mtime, status.st_atime, created)

    with ThreadPoolExecutor(max_workers=8) as pool:
        for entry, value in zip(entries, pool.map(times, entries, chunksize=256), strict=True):
            entry.times = value


@dataclass
class Cut:
    """A page's first result, trimmed at the byte budget: shown from ``first`` to
    ``shown`` of its lines ``first`` to ``last``."""

    label: str
    first: int
    shown: int
    last: int


@dataclass
class Page:
    """The results of one page and what the Agent needs to continue."""

    offset: int
    limit: int
    lines: list[str] = field(default_factory=list)
    returned: int = 0
    size: int = 0
    byte_limited: bool = False
    cut: Cut | None = None
    # Label of the file whose heading the page's line output showed last.
    heading_label: str | None = None

    @property
    def room(self) -> int:
        return MAX_OUTPUT_BYTES - _RESERVED_BYTES - self.size

    def fits(self, lines: list[str]) -> bool:
        return sum(len(line.encode("utf-8")) + 1 for line in lines) <= self.room

    def heading(self, label: str) -> list[str]:
        """The lines that name a file before its first line on this page."""
        if self.lines and self.heading_label == label:
            return []
        if label == "--" or re.match(r"^\d+[:-]", label):
            label = "./" + label
        return ["", label] if self.lines else [label]

    def add(self, lines: list[str], units: int = 1, *, label: str | None = None) -> None:
        self.lines.extend(lines)
        self.size += sum(len(line.encode("utf-8")) + 1 for line in lines)
        self.returned += units
        if label is not None:
            self.heading_label = label


def entry_page(entries: list[Entry], query: SearchQuery) -> Page:
    """Page listings, file lists and per-file counts: one entry per result."""
    page = Page(query.offset, query.limit)
    for entry in entries[query.offset :]:
        if page.returned >= page.limit:
            break
        line = f"{entry.label}:{entry.count}" if query.mode == "count" else entry.label
        if not page.fits([line]):
            page.byte_limited = True
            break
        page.add([line])
    return page


def content_window(entries: list[Entry], offset: int, limit: int) -> list[tuple[Entry, int]]:
    """Return the files holding results ``offset`` to ``offset + limit``, each with the
    number of its results that earlier pages showed."""
    window: list[tuple[Entry, int]] = []
    before = 0
    for entry in entries:
        if before >= offset + limit:
            break
        if before + entry.count > offset:
            window.append((entry, max(offset - before, 0)))
        before += entry.count
    return window


@dataclass
class _Line:
    number: int
    match: bool
    text: str


def _utf8_boundary(raw: bytes, offset: int, *, end: bool) -> int:
    """Include the whole character if a byte-mode regex cuts through it."""
    if offset == len(raw) or raw[offset] & 0xC0 != 0x80:
        return offset
    start = offset - 1
    while start >= 0 and offset - start < 4 and raw[start] & 0xC0 == 0x80:
        start -= 1
    if start < 0 or offset - start >= 4:
        return offset
    char = raw[start : start + 4].decode("utf-8", errors="surrogateescape")[0]
    width = len(char.encode("utf-8", errors="surrogateescape"))
    if start + width <= offset:
        return offset
    return start + width if end else start


def _excerpt(raw: bytes, spans: list[tuple[int, int]]) -> str:
    """Share one line's character budget among its matches, in source order."""
    text = raw.decode("utf-8", errors="backslashreplace").replace("\r", "␍")
    if len(text) <= MAX_LINE_CHARS:
        return text
    if not spans:
        return f"{text[:MAX_LINE_CHARS]} [{len(text) - MAX_LINE_CHARS} characters omitted]"

    # Omission markers can be as long as a short window. Keep enough text
    # per window for context, even on lines with thousands of matches.
    width = max(32, MAX_LINE_CHARS // len(spans))
    windows: list[tuple[int, int]] = []
    remaining = MAX_LINE_CHARS
    hidden = 0
    byte_start = char_start = 0
    for left, right in spans:
        left = _utf8_boundary(raw, left, end=False)
        right = _utf8_boundary(raw, right, end=True)
        start = char_start + len(raw[byte_start:left].decode("utf-8", errors="backslashreplace"))
        end = start + len(raw[left:right].decode("utf-8", errors="backslashreplace"))
        byte_start, char_start = left, start
        if windows and start >= windows[-1][0] and end <= windows[-1][1]:
            continue
        if remaining == 0:
            hidden += 1
            continue
        size = min(width, remaining)
        if end - start > size:
            # Keep both ends of a long match, including where a multiline
            # match ends on a long line.
            half = size // 2
            pieces = [(start, start + half), (end - (size - half), end)]
        else:
            begin = max(0, min(start - min(size // 5, size - (end - start)), len(text) - size))
            pieces = [(begin, begin + size)]
        for begin, finish in pieces:
            if windows and begin <= windows[-1][1]:
                old_start, old_end = windows[-1]
                windows[-1] = (old_start, max(old_end, finish))
                remaining -= max(0, finish - old_end)
            else:
                windows.append((begin, finish))
                remaining -= finish - begin
    parts = []
    previous = 0
    for begin, finish in windows:
        if begin > previous:
            parts.append(f"[{begin - previous} characters omitted]")
        parts.append(text[begin:finish])
        previous = finish
    if previous < len(text):
        parts.append(f"[{len(text) - previous} characters omitted]")
    if hidden:
        parts.append(f"[{_count(hidden, 'match', 'matches')} omitted]")
    return " ".join(parts)


def _physical_lines(raw: bytes) -> Iterator[tuple[int, bytes]]:
    """Yield the byte start and text of each LF line, preserving lone CRs."""
    start = 0
    while True:
        lf = raw.find(b"\n", start)
        if lf < 0:
            if start < len(raw) or not raw:
                yield start, raw[start:]
            return
        yield start, raw[start:lf].removesuffix(b"\r")
        start = lf + 1


def _fragments(data: dict[str, Any], raw: bytes) -> Iterator[tuple[int, int, int]]:
    """Locate each fragment by source line and byte range."""
    number = data.get("line_number") or 1
    previous = 0
    for sub in data["submatches"]:
        start, end = sub["start"], sub["end"]
        number += raw.count(b"\n", previous, start)
        previous = start
        yield number, start, end


def _event_lines(
    event: dict[str, Any], *, only_matching: bool, focus: slice = slice(None)
) -> list[_Line]:
    data = event["data"]
    raw = decode_event_text(data["lines"])
    first = data.get("line_number") or 1
    match = event["type"] == "match"
    submatches = (data.get("submatches") or [])[focus]
    if only_matching and not match and submatches:
        # With -vo the result is a non-matching line. A context event can
        # contain several matched fragments on that same line.
        return [
            _Line(number + index, False, _excerpt(line, [(0, len(line))]))
            for number, left, right in _fragments(data, raw)
            for index, (_, line) in enumerate(_physical_lines(raw[left:right]))
        ]
    lines = []
    cursor = 0
    for index, (start, piece) in enumerate(_physical_lines(raw)):
        end = start + len(piece)
        spans = []
        while (
            cursor < len(submatches)
            and submatches[cursor]["end"] <= start
            and submatches[cursor]["start"] < start
        ):
            cursor += 1
        while cursor < len(submatches) and submatches[cursor]["start"] <= end:
            sub = submatches[cursor]
            spans.append((max(sub["start"] - start, 0), min(sub["end"], end) - start))
            if sub["end"] > end:
                break
            cursor += 1
        lines.append(_Line(first + index, match, _excerpt(piece, spans)))
    return lines


def content_page(
    window: list[tuple[Entry, int]],
    events: dict[tuple[int, bytes], list[dict[str, Any]]],
    query: SearchQuery,
) -> Page:
    """Render matching lines with their context for the files of one page.

    Context never shows the lines of a match this page leaves out, so no
    unshown match is mistaken for context.
    """
    page = Page(query.offset, query.limit)
    separate = bool(query.before or query.after)
    for entry, skip in window:
        if page.returned >= page.limit or page.byte_limited:
            break
        file_events = events.get((entry.scope, entry.native), [])
        if any(
            b"\r" in decode_event_text(event["data"]["lines"]).replace(b"\r\n", b"")
            for event in file_events
        ):
            query.notes.append(
                f"{entry.label} contains lone CR line endings, shown as ␍. Line numbers "
                "count LF boundaries and can differ from read."
            )
        if query.only_matching and not query.inverted:
            _only_matching(page, entry, file_events, skip)
            continue
        _file_lines(page, entry, file_events, skip, query, separate)
    return page


def _only_matching(page: Page, entry: Entry, file_events: list[dict[str, Any]], skip: int) -> None:
    unit = 0
    for event in file_events:
        if event["type"] != "match":
            continue
        data = event["data"]
        raw = decode_event_text(data["lines"])
        for number, start, end in _fragments(data, raw):
            if unit >= entry.count or page.returned >= page.limit:
                return
            unit += 1
            if unit <= skip:
                continue
            if not _add_fragment(page, entry.label, number, raw[start:end]):
                return


def _add_fragment(page: Page, label: str, first: int, fragment: bytes) -> bool:
    """Buffer at most a page, even for a match spanning millions of lines."""
    lines = page.heading(label)
    room = page.room - sum(len(line.encode("utf-8")) + 1 for line in lines)
    for index, (_, piece) in enumerate(_physical_lines(fragment)):
        line = f"{first + index}:{_excerpt(piece, [(0, len(piece))])}"
        room -= len(line.encode("utf-8")) + 1
        if room < 0:
            page.byte_limited = True
            if page.returned or not index:
                return False
            last = first + fragment.count(b"\n") - int(fragment.endswith(b"\n"))
            page.cut = Cut(label, first, first + index - 1, last)
            break
        lines.append(line)
    page.add(lines, label=label)
    return not page.byte_limited


def _file_lines(
    page: Page,
    entry: Entry,
    file_events: list[dict[str, Any]],
    skip: int,
    query: SearchQuery,
    separate: bool,
) -> None:
    lines: list[_Line] = []
    # First and last index in lines of each result: a matching line, or with -U one
    # match, which can span lines and share a line with the match before it.
    groups: list[tuple[int, int]] = []
    by_line = not query.multiline or query.inverted
    for event in file_events:
        start = len(lines)
        focus = slice(None)
        if not by_line and event["type"] == "match":
            # Several paged matches can share a long source line. Center this
            # page's excerpts on the matches this page actually counts.
            focus = slice(
                max(0, skip - len(groups)),
                max(0, min(entry.count, skip + page.limit - page.returned) - len(groups)),
            )
        lines.extend(_event_lines(event, only_matching=query.only_matching, focus=focus))
        if event["type"] != "match":
            continue
        if by_line:
            groups.append((start, len(lines) - 1))
            continue
        raw = decode_event_text(event["data"]["lines"])
        last_line = start
        previous = 0
        for submatch in event["data"].get("submatches") or []:
            begin = submatch["start"]
            end = max(submatch["end"] - 1, begin)
            first_line = last_line + raw.count(b"\n", previous, begin)
            last_line = first_line + raw.count(b"\n", begin, end)
            groups.append((first_line, last_line))
            previous = end
    block = _Block(page, entry, separate)
    # rg can emit matching lines as match events inside trailing context even
    # after --max-count. They are context here, not further counted results.
    if len(groups) > entry.count:
        last_counted = groups[entry.count - 1][1] if entry.count else -1
        for first, last in groups[entry.count :]:
            for line in lines[max(first, last_counted + 1) : last + 1]:
                line.match = False
        groups = groups[: entry.count]
    unit = 0
    lower = 0  # leading context never reaches a match this page leaves out
    shown = -1  # index in lines of the last line shown
    following = len(groups)
    for index, (first, last) in enumerate(groups):
        if unit >= entry.count:
            following = index
            break
        if unit < skip:
            unit += 1
            lower = last + 1
            continue
        if page.returned >= page.limit or page.byte_limited:
            following = index
            break
        if shown >= 0:
            start = shown + 1
        else:
            start = min(first, max(lower, _context_start(lines, first, query.before)))
        if not block.add(lines[start : last + 1], lines[first : last + 1], 1):
            return
        shown = max(shown, last)
        unit += 1
    if shown < 0 or page.byte_limited:
        return
    upper = groups[following][0] if following < len(groups) else len(lines)
    for line in lines[shown + 1 : upper]:
        if line.number - lines[shown].number > query.after or not block.add([line], [line], 0):
            return


@dataclass
class _Block:
    """Adds one file's lines to a page under its heading, separating gaps with ``--``
    when context is shown."""

    page: Page
    entry: Entry
    separate: bool
    previous: int | None = None

    def _body(self, lines: list[_Line]) -> list[str]:
        rendered: list[str] = []
        previous = self.previous
        for line in lines:
            if (
                self.separate
                and previous is not None
                and line.number not in (previous, previous + 1)
            ):
                rendered.append("--")
            rendered.append(f"{line.number}{':' if line.match else '-'}{line.text}")
            previous = line.number
        return rendered

    def add(self, lines: list[_Line], result: list[_Line], units: int) -> bool:
        """Add lines, or only the result lines for a page's first result; False when full."""
        heading = self.page.heading(self.entry.label)
        body = self._body(lines)
        if not self.page.fits(heading + body):
            self.page.byte_limited = True
            if self.page.returned or not units:
                return False
            # A page's first result always appears, without its context if need be.
            lines = result
            body = self._body(lines)
            room = self.page.room - sum(len(line.encode("utf-8")) + 1 for line in heading)
            for index, line in enumerate(body):
                room -= len(line.encode("utf-8")) + 1
                if room < 0 and index:
                    del body[index:]
                    break
            shown = sum(1 for line in body if line != "--")
            if shown < len(lines):
                self.page.cut = Cut(
                    self.entry.label, lines[0].number, lines[shown - 1].number, lines[-1].number
                )
                lines = lines[:shown]
        self.page.add(heading + body, units, label=self.entry.label)
        if lines:
            self.previous = lines[-1].number
        return not self.page.byte_limited


def _context_start(lines: list[_Line], first: int, before: int) -> int:
    start = first
    while (
        start > 0
        and not lines[start - 1].match
        and lines[first].number - lines[start - 1].number <= before
    ):
        start -= 1
    return start


def _count(number: int, singular: str, plural: str) -> str:
    return f"{number} {singular if number == 1 else plural}"


def summary(
    page: Page,
    query: SearchQuery,
    *,
    total: int,
    files: int,
    lines_total: int,
    files_searched: int | None,
    complete: bool,
) -> str:
    """Say what was found, which part this page shows, and how to continue."""
    if total == 0:
        unmatched = _unmatched_globs(query, files_searched)
        if unmatched and complete:
            return unmatched
        if query.mode == "list_dirs":
            found = "No directories found"
        elif query.mode == "list_files":
            found = "No files found"
        elif query.mode == "files_without_match":
            found = "Every searched file contains a match"
        elif files_searched is not None:
            found = f"No matches in {_count(files_searched, 'searched file', 'searched files')}"
        else:
            found = "No matches"
        return f"{found}." if complete else f"{found} so far; the search is incomplete."
    matched = (
        ("match", "matches")
        if query.count_matches or query.multiline
        else ("matching line", "matching lines")
    )
    if query.inverted:
        matched = ("non-matching line", "non-matching lines")
    newest = ", newest first" if query.sort is None else ""
    shown = "files"
    if query.mode == "content":
        whole = f"{_count(total, *matched)} in {_count(files, 'file', 'files')}"
        shown = matched[1]
    elif query.mode == "files_with_matches":
        whole = f"{_count(files, 'file', 'files')} with {_count(lines_total, *matched)}"
    elif query.mode == "count":
        whole = f"{_count(lines_total, *matched)} in {_count(files, 'file', 'files')}"
    elif query.mode == "files_without_match":
        whole = f"{_count(files, 'file', 'files')} without a match"
    elif query.mode == "list_files":
        whole = _count(total, "file", "files") + newest
    else:
        whole = _count(total, "directory", "directories") + newest
        shown = "directories"
    end = page.offset + page.returned
    whole_page = page.offset == 0 and end >= total
    if not page.returned:
        text = (
            f"Found {whole}, but no result lines could be returned. Narrow path or glob and retry."
        )
    elif whole_page:
        text = f"Found {whole}."
    else:
        text = f"Showing {shown} {page.offset + 1}-{end} of {whole}."
    cut = page.cut
    if cut is not None:
        # Paging cannot show the rest of one result, so name where it continues.
        text += (
            f" The {matched[0]} at {cut.label}:{cut.first} spans lines {cut.first}-{cut.last};"
            f" the 50 KiB output limit cut it after line {cut.shown}."
            f" Read {cut.label} from line {cut.shown + 1} to see the rest."
        )
    elif page.byte_limited and whole_page:
        text += " Some context lines were left out at the 50 KiB output limit."
    elif page.byte_limited and page.returned:
        text += " This page stopped at the 50 KiB output limit."
    if page.returned and end < total:
        if end <= MAX_OFFSET:
            text += f" Continue with offset {end}."
        else:
            text += (
                f" Paging ends at offset {MAX_OFFSET}. "
                "Narrow path, glob or pattern and restart with offset 0."
            )
    if not complete:
        text += " The search is incomplete; see warnings."
    return text


# Arguments besides globs that narrow which files the walk selects.
_SELECTION_FLAGS = ("--type=", "--type-not=", "--max-depth=", "--max-filesize=", "--no-hidden")


def _unmatched_globs(query: SearchQuery, files_searched: int | None) -> str | None:
    """Name the selecting globs when no file matched them and nothing else narrowed the walk."""
    selecting = [glob for glob in query.globs if not glob.startswith("!")]
    listing = query.mode in LIST_MODES
    if not selecting or query.name_patterns or (not listing and files_searched != 0):
        return None
    if any(argument.startswith(_SELECTION_FLAGS) for argument in query.rg_args):
        return None
    noun = "directory" if query.mode == "list_dirs" else "file"
    names = ", ".join(json.dumps(glob, ensure_ascii=False) for glob in selecting)
    return f"No {noun} matches the glob{'s' if len(selecting) > 1 else ''} {names}."
