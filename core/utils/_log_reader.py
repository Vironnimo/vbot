"""Bounded blocking log discovery, parsing, paging, tail reads and cursor verification."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from core.utils.file_status import is_link_status
from core.utils.ids import is_reserved_name
from core.utils.logging import (
    extract_websocket_path_from_message,
    is_routine_websocket_lifecycle_message,
)

JsonObject = dict[str, Any]

APPEND_EVENT = "append"

RESET_EVENT = "reset"

UNKNOWN_LEVEL = "unknown"

UNKNOWN_LOGGER_NAME = ""

UNKNOWN_TIMESTAMP = ""

# Newest entries per page (``log.read`` and every reset). A page stays a few
# hundred KiB of JSON and a few milliseconds of parsing, covers several screens
# of the Logs view, and bounds what one reset or append event carries.
LOG_PAGE_ENTRIES = 500

# No single read of a log file reads more bytes than this, whatever the file
# holds (a page of huge tracebacks, a long burst between two watcher ticks).
LOG_READ_MAX_BYTES = 4 * 1024 * 1024

# A page read starts with this much of the file and doubles it until it holds a
# page or reaches the file start or LOG_READ_MAX_BYTES.
_PAGE_CHUNK_BYTES = 64 * 1024

# A cursor's digest covers the file name and at most this many bytes before the
# cursor's end. A truncated, replaced or rotated file (or another file's cursor)
# fails that check, while issuing and checking a cursor stays one small read
# however long the file grows; up to this size the digest covers the whole read.
_CURSOR_WINDOW_BYTES = 64 * 1024

# Daily files start with their date; the catalog lists them newest first and the
# directory's other files, such as the crash log, after them by name.
_DAILY_FILE_PREFIX = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

# ``v1.<byte length>.<sha256 hex>``; both fields are bounded, so a forged cursor
# costs one anchored match.
_CURSOR_PATTERN = re.compile(r"v1\.(0|[1-9][0-9]{0,15})\.([0-9a-f]{64})")

LOG_LINE_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) "
    r"\[(?P<level>[A-Z]+)\] "
    r"(?P<logger_name>.+?) - (?P<message>.*)$"
)


@dataclass(slots=True)
class _ParsedEntry:
    """One entry of a parsed byte range; hidden entries are routine websocket noise."""

    offset: int
    visible: bool
    fields: JsonObject


@dataclass(slots=True)
class _Page:
    entries: list[JsonObject]
    next_before: int | None
    # The last entry up to the page end, visible or not: where a tail starts.
    last_start: int
    last_entry: JsonObject | None


@dataclass(frozen=True, slots=True)
class _LogTail:
    """How far one reader of a log file got.

    Everything before ``open_start`` is final. The entry starting there may
    still grow (a traceback being written, a partial last line), so the next
    read parses the file again from that offset; ``open_entry`` is that entry
    as the reader last saw it, or ``None`` when it is hidden or absent.
    """

    exists: bool
    size: int = 0
    modified_ns: int | None = None
    identity: tuple[int, int] | None = None
    open_start: int = 0
    open_entry: JsonObject | None = None


_MISSING_TAIL = _LogTail(exists=False)


@dataclass(frozen=True, slots=True)
class _ReadCursor:
    """What one ``read_file`` covered: the file's first ``byte_length`` bytes, by digest."""

    byte_length: int
    digest: str


@dataclass(slots=True)
class _WatchRefresh:
    """What one worker read found for a watched file; ``None`` means not read."""

    catalog: tuple[str, ...] | None = None
    directory_modified_ns: tuple[int | None, ...] | None = None
    tail: _LogTail | None = None
    event: JsonObject | None = None


def parse_log_entries(text: str) -> list[JsonObject]:
    """Parse log text into its visible structured entries."""

    parsed = _parse_lines(text.encode("utf-8"), 0, skip_leading_fragment=False)
    return [entry.fields for entry in parsed if entry.visible]


def _parse_lines(data: bytes, base: int, *, skip_leading_fragment: bool) -> list[_ParsedEntry]:
    """Group the lines of ``data``, which starts at file offset ``base``, into entries.

    ``data`` starts at a line start. A last line without its newline yet is
    parsed like any other; the tail re-parses it once it grows. With
    ``skip_leading_fragment`` the lines before the first header line are dropped:
    they continue an entry whose header lies before ``base``. Undecodable bytes
    (a torn or foreign write) show as U+FFFD instead of failing the read.
    """

    parsed: list[_ParsedEntry] = []
    current_lines: list[str] = []
    current_match: re.Match[str] | None = None
    current_offset = 0
    in_fragment = skip_leading_fragment

    def finish() -> None:
        if not current_lines:
            return
        raw = "\n".join(current_lines)
        continuation = "\n".join(current_lines[1:])
        if current_match is None:
            fields: JsonObject = {
                "timestamp": UNKNOWN_TIMESTAMP,
                "level": UNKNOWN_LEVEL,
                "logger_name": UNKNOWN_LOGGER_NAME,
                "message": current_lines[0],
            }
        else:
            fields = {
                "timestamp": current_match.group("timestamp"),
                "level": current_match.group("level").lower(),
                "logger_name": current_match.group("logger_name"),
                "message": current_match.group("message"),
            }
        # ``raw`` keeps the source lines verbatim so the UI can copy an entry
        # exactly as it appears in the file.
        fields.update(continuation=continuation, raw=raw, offset=current_offset)
        visible = current_match is None or _should_include_entry(fields)
        parsed.append(_ParsedEntry(offset=current_offset, visible=visible, fields=fields))

    position = 0
    length = len(data)
    while position < length:
        newline = data.find(b"\n", position)
        end = length if newline < 0 else newline
        line_bytes = data[position:end]
        if line_bytes.endswith(b"\r"):
            line_bytes = line_bytes[:-1]
        line = line_bytes.decode("utf-8", errors="replace")
        match = LOG_LINE_PATTERN.match(line)
        if match is not None:
            finish()
            in_fragment = False
            current_lines = [line]
            current_match = match
            current_offset = base + position
        elif not in_fragment:
            if not current_lines:
                current_match = None
                current_offset = base + position
            current_lines.append(line)
        position = end + 1
    finish()
    return parsed


def _should_include_entry(entry: JsonObject) -> bool:
    message = str(entry["message"])
    return not is_routine_websocket_lifecycle_message(
        level=str(entry["level"]).upper(),
        logger_name=str(entry["logger_name"]),
        message=message,
        websocket_path=extract_websocket_path_from_message(message),
    )


def _read_range(handle: BinaryIO, start: int, end: int) -> bytes:
    handle.seek(start)
    return handle.read(end - start)


def _read_page(handle: BinaryIO, end: int, limit: int) -> _Page:
    """Parse the newest ``limit`` visible entries that start before byte ``end``.

    ``next_before`` is the offset to page on from, or ``None`` at the file start.
    With ``limit`` 0 only the last entry, where a tail starts, is looked for.
    """

    size = _PAGE_CHUNK_BYTES
    while True:
        read_start = max(0, end - size)
        capped = size >= LOG_READ_MAX_BYTES
        data = _read_range(handle, read_start, end)
        # A range that starts inside the file starts after its first newline, and
        # its lines up to the first header continue an entry that starts earlier.
        newline = data.find(b"\n") if read_start > 0 else -1
        region_start = read_start + newline + 1
        parsed = (
            []
            if read_start > 0 and newline < 0
            else _parse_lines(
                data[newline + 1 :], region_start, skip_leading_fragment=read_start > 0
            )
        )
        if not parsed and read_start > 0 and capped:
            # No entry starts within the read limit: show the range read as one
            # entry rather than nothing, so paging still moves on.
            region_start = read_start
            parsed = _parse_lines(data, read_start, skip_leading_fragment=False)
        visible = [entry.fields for entry in parsed if entry.visible]
        found = len(visible) > limit if limit > 0 else bool(parsed)
        if found or read_start == 0 or capped:
            break
        size = min(size * 2, LOG_READ_MAX_BYTES)

    if len(visible) > limit:
        entries = visible[len(visible) - limit :]
        next_before: int | None = entries[0]["offset"] if entries else parsed[-1].offset
    elif read_start == 0:
        entries, next_before = visible, None
    else:
        entries = visible
        next_before = parsed[0].offset if parsed else region_start
    last = parsed[-1] if parsed else None
    return _Page(
        entries=entries,
        next_before=next_before or None,
        last_start=last.offset if last is not None else end,
        last_entry=last.fields if last is not None and last.visible else None,
    )


def _tail_at(stat: os.stat_result, open_start: int, open_entry: JsonObject | None) -> _LogTail:
    return _LogTail(
        exists=True,
        size=stat.st_size,
        modified_ns=stat.st_mtime_ns,
        identity=(stat.st_dev, stat.st_ino),
        open_start=open_start,
        open_entry=open_entry,
    )


def _reset_event(file_name: str, entries: list[JsonObject], next_before: int | None) -> JsonObject:
    return {"type": RESET_EVENT, "file": file_name, "entries": entries, "next_before": next_before}


def _newest_page(
    handle: BinaryIO, stat: os.stat_result, file_name: str
) -> tuple[_LogTail, JsonObject]:
    page = _read_page(handle, stat.st_size, LOG_PAGE_ENTRIES)
    tail = _tail_at(stat, page.last_start, page.last_entry)
    return tail, _reset_event(file_name, page.entries, page.next_before)


def _advance_open(
    handle: BinaryIO, stat: os.stat_result, file_name: str, tail: _LogTail
) -> tuple[_LogTail, JsonObject | None]:
    """Bring ``tail`` to the open file's current state and describe the change.

    A shrunk, replaced or newly created file resets; so does a change too large
    for one bounded read. Otherwise only the open entry and the appended bytes
    are parsed, and the append event says which entries replace the reader's
    open entry: the reader drops its entries at or after ``from_offset``.
    """

    identity = (stat.st_dev, stat.st_ino)
    if (
        tail.exists
        and identity == tail.identity
        and stat.st_size == tail.size
        and stat.st_mtime_ns == tail.modified_ns
    ):
        return tail, None
    if (
        not tail.exists
        or identity != tail.identity
        or stat.st_size < tail.size
        or stat.st_size - tail.open_start > LOG_READ_MAX_BYTES
    ):
        return _newest_page(handle, stat, file_name)

    data = _read_range(handle, tail.open_start, stat.st_size)
    parsed = _parse_lines(data, tail.open_start, skip_leading_fragment=False)
    last = parsed[-1] if parsed else None
    next_tail = _tail_at(
        stat,
        last.offset if last is not None else tail.open_start,
        last.fields if last is not None and last.visible else None,
    )
    entries = [entry.fields for entry in parsed if entry.visible]
    from_offset: int | None
    if tail.open_entry is not None and entries and entries[0] == tail.open_entry:
        entries = entries[1:]
        from_offset = entries[0]["offset"] if entries else None
    elif tail.open_entry is not None:
        from_offset = tail.open_start
    else:
        from_offset = entries[0]["offset"] if entries else None
    if from_offset is None:
        return next_tail, None
    if len(entries) > LOG_PAGE_ENTRIES:
        # More than a page arrived at once: the newest page replaces the reader's
        # entries instead of an unbounded append.
        page = entries[len(entries) - LOG_PAGE_ENTRIES :]
        return next_tail, _reset_event(file_name, page, page[0]["offset"])
    return next_tail, {
        "type": APPEND_EVENT,
        "file": file_name,
        "from_offset": from_offset,
        "entries": entries,
    }


def _advance_tails(
    file_path: Path, file_name: str, tails: Sequence[_LogTail]
) -> list[tuple[_LogTail, JsonObject | None]]:
    """Advance several readers of one file to the same state of that file."""

    try:
        handle = file_path.open("rb")
    except FileNotFoundError:
        return [
            (_MISSING_TAIL, _reset_event(file_name, [], None) if tail.exists else None)
            for tail in tails
        ]
    with handle:
        stat = os.fstat(handle.fileno())
        return [_advance_open(handle, stat, file_name, tail) for tail in tails]


def _read_tail(file_path: Path) -> _LogTail:
    """Return a tail at the file's current end without reading a page of it."""

    _reject_log_links(file_path)
    try:
        handle = file_path.open("rb")
    except FileNotFoundError:
        return _MISSING_TAIL
    with handle:
        stat = os.fstat(handle.fileno())
        page = _read_page(handle, stat.st_size, 0)
    return _tail_at(stat, page.last_start, page.last_entry)


def _read_subscribe_start(
    file_path: Path, file_name: str, watcher_tail: _LogTail, cursor: _ReadCursor | None
) -> tuple[_LogTail, JsonObject | None, JsonObject | None]:
    """Advance the watcher's tail and, for a cursor, replay what its read missed.

    Returns the watcher's new tail, the catch-up event for the watcher's other
    subscribers and the new subscriber's replay. A file that no longer starts
    with the bytes the read covered (truncated, rotated, rewritten, or another
    file's cursor) replays as a reset with the newest page.
    """

    _reject_log_links(file_path)
    try:
        handle = file_path.open("rb")
    except FileNotFoundError:
        catch_up = _reset_event(file_name, [], None) if watcher_tail.exists else None
        replay = None if cursor is None else _reset_event(file_name, [], None)
        return _MISSING_TAIL, catch_up, replay
    with handle:
        stat = os.fstat(handle.fileno())
        tail, catch_up = _advance_open(handle, stat, file_name, watcher_tail)
        if cursor is None:
            return tail, catch_up, None
        read_tail = _cursor_tail(handle, stat, file_name, cursor)
        if read_tail is None:
            return tail, catch_up, _newest_page(handle, stat, file_name)[1]
        return tail, catch_up, _advance_open(handle, stat, file_name, read_tail)[1]


def _cursor_tail(
    handle: BinaryIO, stat: os.stat_result, file_name: str, cursor: _ReadCursor
) -> _LogTail | None:
    """Rebuild the tail a read ended at, or ``None`` when the file no longer matches it."""

    if cursor.byte_length > stat.st_size:
        return None
    if _cursor_digest(handle, file_name, cursor.byte_length) != cursor.digest:
        return None
    page = _read_page(handle, cursor.byte_length, 0)
    # The read saw this very file; its time is unknown, so the tail always re-parses.
    return _LogTail(
        exists=True,
        size=cursor.byte_length,
        identity=(stat.st_dev, stat.st_ino),
        open_start=page.last_start,
        open_entry=page.last_entry,
    )


def _encode_cursor(handle: BinaryIO, file_name: str, byte_length: int) -> str:
    return f"v1.{byte_length}.{_cursor_digest(handle, file_name, byte_length)}"


def _parse_cursor(cursor: object) -> _ReadCursor:
    match = _CURSOR_PATTERN.fullmatch(cursor) if isinstance(cursor, str) else None
    if match is None:
        raise ValueError("invalid log cursor")
    return _ReadCursor(byte_length=int(match.group(1)), digest=match.group(2))


def _cursor_digest(handle: BinaryIO, file_name: str, byte_length: int) -> str:
    """Digest of the bytes just before ``byte_length``, bound to the file name.

    Binding the name means a cursor never fits another file; bounding the bytes
    to ``_CURSOR_WINDOW_BYTES`` keeps issuing and checking a cursor independent
    of the file's size.
    """

    window = _read_range(handle, max(0, byte_length - _CURSOR_WINDOW_BYTES), byte_length)
    digest = hashlib.sha256(os.fsencode(file_name))
    digest.update(b"\0")
    digest.update(window)
    return digest.hexdigest()


def _resync_event(file_path: Path, file_name: str, tail: _LogTail) -> JsonObject:
    """Return the reset that brings a subscriber to ``tail``."""

    if not tail.exists:
        return _reset_event(file_name, [], None)
    _reject_log_links(file_path)
    try:
        handle = file_path.open("rb")
    except FileNotFoundError:
        return _reset_event(file_name, [], None)
    with handle:
        # A file that changed since is reset again by the watcher's next event.
        end = min(tail.size, os.fstat(handle.fileno()).st_size)
        page = _read_page(handle, end, LOG_PAGE_ENTRIES)
    return _reset_event(file_name, page.entries, page.next_before)


def _directory_stamp(directories: tuple[Path, ...]) -> tuple[int | None, ...]:
    return tuple(_path_modified_ns(directory) for directory in directories)


def _reject_log_links(path: Path) -> None:
    for candidate in (path.parent, path):
        try:
            if is_link_status(candidate.lstat()):
                raise ValueError("log files must not be links")
        except FileNotFoundError:
            continue


def _path_modified_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def _file_metadata_changed(file_path: Path, tail: _LogTail) -> bool:
    try:
        file_stat = file_path.stat()
    except FileNotFoundError:
        return tail.exists
    return (
        not tail.exists
        or file_stat.st_size != tail.size
        or file_stat.st_mtime_ns != tail.modified_ns
    )


class LogReader:
    """The configured log sources and their bounded blocking read operations."""

    def __init__(
        self, data_dir: str | Path, *, additional_directories: Mapping[str, Path] | None = None
    ) -> None:
        self._logs_dir = Path(data_dir).expanduser() / "logs"
        self._directories = {"": self._logs_dir}
        for namespace, directory in (additional_directories or {}).items():
            if not re.fullmatch(r"[a-z][a-z0-9_-]*", namespace):
                raise ValueError("invalid log namespace")
            self._directories[namespace] = directory

    def list_files(self) -> JsonObject:
        daily: list[str] = []
        other: list[str] = []
        for name in self._iter_log_files():
            group = daily if _DAILY_FILE_PREFIX.match(name.rpartition("/")[2]) else other
            group.append(name)
        daily.sort(key=lambda name: name.rpartition("/")[2], reverse=True)
        other.sort()
        files = daily + other
        return {"files": files, "default_file": files[0] if files else None}

    def read_newest(self, file_name: str) -> tuple[str, _Page, str]:
        file_path = self.resolve_existing_file(file_name)
        with file_path.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            page = _read_page(handle, size, LOG_PAGE_ENTRIES)
            cursor = _encode_cursor(handle, file_name, size)
        return file_name, page, cursor

    def read_older(self, file_name: str, before: int) -> tuple[str, _Page]:
        file_path = self.resolve_existing_file(file_name)
        with file_path.open("rb") as handle:
            if before > os.fstat(handle.fileno()).st_size:
                raise ValueError("log position is past the end of the file")
            page = _read_page(handle, before, LOG_PAGE_ENTRIES)
        return file_name, page

    def read_watch_start(
        self, file_path: Path
    ) -> tuple[tuple[int | None, ...], tuple[str, ...], _LogTail]:
        directory_modified_ns = _directory_stamp(tuple(self._directories.values()))
        catalog = tuple(self.list_files()["files"])
        return directory_modified_ns, catalog, _read_tail(file_path)

    def read_refresh(
        self,
        file_path: Path,
        file_name: str,
        tail: _LogTail,
        *,
        catalog: bool,
        content: bool,
    ) -> _WatchRefresh:
        _reject_log_links(file_path)
        refresh = _WatchRefresh()
        if catalog:
            refresh.directory_modified_ns = _directory_stamp(tuple(self._directories.values()))
            refresh.catalog = tuple(self.list_files()["files"])
        if content:
            refresh.tail, refresh.event = _advance_tails(file_path, file_name, [tail])[0]
        return refresh

    def watch_directories(self) -> tuple[Path, ...]:
        return tuple(directory for directory in self._directories.values() if directory.is_dir())

    def _iter_log_files(self) -> Iterable[str]:
        for namespace, directory in self._directories.items():
            try:
                if is_link_status(directory.lstat()):
                    continue
                for path in directory.iterdir():
                    if (
                        not is_link_status(path.lstat())
                        and path.is_file()
                        and not is_reserved_name(path.name)
                    ):
                        yield f"{namespace}/{path.name}" if namespace else path.name
            except FileNotFoundError:
                continue

    def file_path(self, file_name: str) -> Path:
        normalized = self._normalize_file_name(file_name)
        namespace, _, name = normalized.rpartition("/")
        return self._directories[namespace] / name

    def resolve_existing_file(self, file_name: str) -> Path:
        normalized_name = self._normalize_file_name(file_name)
        file_path = self.file_path(normalized_name)
        if is_link_status(file_path.parent.lstat()) or is_link_status(file_path.lstat()):
            raise ValueError("log files must not be links")
        if not file_path.is_file():
            raise FileNotFoundError(f"log file not found: {normalized_name}")
        return file_path

    def _normalize_file_name(self, file_name: str) -> str:
        if not isinstance(file_name, str) or not file_name:
            raise ValueError("log file name must be a non-empty string")
        namespace, _, name = file_name.rpartition("/")
        if (
            namespace not in self._directories
            or (not namespace and "/" in file_name)
            or not name
            or is_reserved_name(name)
            or Path(name).name != name
            or name in {".", ".."}
            or "\\" in file_name
        ):
            raise ValueError(f"invalid log file name: {file_name}")
        return file_name

    def metadata_changes(
        self, directory_modified_ns: tuple[int | None, ...] | None, file_path: Path, tail: _LogTail
    ) -> tuple[bool, bool]:
        return (
            _directory_stamp(tuple(self._directories.values())) != directory_modified_ns,
            _file_metadata_changed(file_path, tail),
        )
