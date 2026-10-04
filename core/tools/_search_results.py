"""Ordering, pagination and readable text of search_files results.

Pages count results: matching lines (or matches with ``-o``) for line output,
files for file and count output, entries for listings. A page ends at its
``limit`` or at the output byte budget, never inside a result, and the next
page starts exactly after the last result shown.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.tools._search_execution import decode_event_text
from core.tools._search_query import SearchQuery
from core.tools.search import MAX_OUTPUT_BYTES, display_search_path

MAX_LINE_CHARS = 1000
# Room kept free below the byte budget for the result's other fields.
_RESERVED_BYTES = 2048


@dataclass
class Entry:
    """One file or directory a search found."""

    path: Path
    label: str
    scope: int = 0
    native: bytes = b""
    count: int = 0
    directory: bool = False
    times: tuple[float, float, float] = (0.0, 0.0, 0.0)


def path_label(path: Path, cwd: Path) -> str:
    """Render a path as the Agent passes it back: relative to cwd, absolute outside."""
    value = display_search_path(path, cwd=cwd)
    return json.dumps(value, ensure_ascii=False) if any(c in value for c in "\n\r\t") else value


def _path_key(entry: Entry) -> tuple[tuple[str, str], ...]:
    return tuple((part.casefold(), part) for part in entry.label.split("/"))


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
class Page:
    """The results of one page and what the Agent needs to continue."""

    offset: int
    limit: int
    lines: list[str] = field(default_factory=list)
    returned: int = 0
    size: int = 0
    byte_limited: bool = False

    @property
    def room(self) -> int:
        return MAX_OUTPUT_BYTES - _RESERVED_BYTES - self.size

    def fits(self, lines: list[str]) -> bool:
        return sum(len(line.encode("utf-8")) + 1 for line in lines) <= self.room

    def add(self, lines: list[str], units: int = 1) -> None:
        self.lines.extend(lines)
        self.size += sum(len(line.encode("utf-8")) + 1 for line in lines)
        self.returned += units


def entry_page(entries: list[Entry], query: SearchQuery) -> Page:
    """Page listings, file lists and per-file counts: one entry per result."""
    page = Page(query.offset, query.limit)
    for entry in entries[query.offset :]:
        if page.returned >= page.limit:
            break
        if query.mode == "count":
            line = f"{entry.label}:{entry.count}"
        elif entry.directory:
            line = entry.label + "/"
        else:
            line = entry.label
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


def _excerpt(raw: bytes, position: int | None) -> str:
    text = raw.decode("utf-8", errors="backslashreplace").rstrip("\r\n")
    if len(text) <= MAX_LINE_CHARS:
        return text
    center = len(raw[:position].decode("utf-8", errors="backslashreplace")) if position else 0
    start = max(min(center - 200, len(text) - MAX_LINE_CHARS), 0)
    end = start + MAX_LINE_CHARS
    prefix = f"[{start} characters omitted] " if start else ""
    suffix = f" [{len(text) - end} characters omitted]" if end < len(text) else ""
    return prefix + text[start:end] + suffix


def _event_lines(event: dict[str, Any]) -> list[_Line]:
    data = event["data"]
    raw = decode_event_text(data["lines"])
    first = data.get("line_number") or 1
    match = event["type"] == "match"
    submatches = data.get("submatches") or []
    position = submatches[0]["start"] if submatches else None
    pieces = raw.splitlines(keepends=True) or [b""]
    lines = []
    consumed = 0
    for index, piece in enumerate(pieces):
        within = position - consumed if position is not None and consumed <= position else None
        if within is not None and within >= len(piece):
            within = None
        lines.append(_Line(first + index, match, _excerpt(piece, within)))
        consumed += len(piece)
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
        if query.only_matching:
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
        for submatch in data.get("submatches") or []:
            if unit >= entry.count or page.returned >= page.limit:
                return
            unit += 1
            if unit <= skip:
                continue
            number = (data.get("line_number") or 1) + raw[: submatch["start"]].count(b"\n")
            text = _excerpt(decode_event_text(submatch["match"]), None)
            line = f"{entry.label}:{number}:{text}"
            if not page.fits([line]):
                page.byte_limited = True
                return
            page.add([line])


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
    for event in file_events:
        start = len(lines)
        lines.extend(_event_lines(event))
        if event["type"] != "match":
            continue
        if not query.multiline:
            groups.append((start, len(lines) - 1))
            continue
        raw = decode_event_text(event["data"]["lines"])
        for submatch in event["data"].get("submatches") or []:
            end = max(submatch["end"] - 1, submatch["start"])
            groups.append(
                (start + raw[: submatch["start"]].count(b"\n"), start + raw[:end].count(b"\n"))
            )
    block = _Block(page, entry, separate)
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
    """Adds one file's lines to a page, separating gaps with ``--`` when context is shown."""

    page: Page
    entry: Entry
    separate: bool
    previous: int | None = None

    def _rendered(self, lines: list[_Line]) -> list[str]:
        rendered: list[str] = []
        previous = self.previous
        for line in lines:
            gap = (previous is None and bool(self.page.lines)) or (
                previous is not None and line.number != previous + 1
            )
            if self.separate and gap:
                rendered.append("--")
            match = ":" if line.match else "-"
            rendered.append(f"{self.entry.label}{match}{line.number}{match}{line.text}")
            previous = line.number
        return rendered

    def add(self, lines: list[_Line], result: list[_Line], units: int) -> bool:
        """Add lines, or only the result lines for a page's first result; False when full."""
        rendered = self._rendered(lines)
        if not self.page.fits(rendered):
            self.page.byte_limited = True
            if self.page.returned or not units:
                return False
            # A page's first result always appears, without its context if need be.
            lines = result
            rendered = self._rendered(lines)
            while len(rendered) > 1 and not self.page.fits(rendered):
                rendered.pop()
        self.page.add(rendered, units)
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
    if page.offset == 0 and end >= total:
        text = f"Found {whole}."
    elif page.returned == 0:
        text = f"No results at offset {page.offset}; the search found {whole}."
    else:
        text = f"Showing {shown} {page.offset + 1}-{end} of {whole}."
        if page.byte_limited:
            text += " This page stopped at the 50 KB output limit."
        if end < total:
            text += f" Continue with offset {end}."
    if not complete:
        text += " The search is incomplete; see warnings."
    return text
