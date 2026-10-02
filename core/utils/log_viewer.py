"""Read-only daily log access and live update watching.

Every log-directory scan, stat, file read, parse and cursor hash runs on the
``log-viewer`` worker pool, and every read is bounded. A page read walks
backwards from its end position only until it holds one page of entries. The
live tail remembers where the file's last entry starts, so each change re-reads
only that still-open entry plus the bytes appended since; it computes its event
on the worker too. Watcher state and event fan-out stay on the Event Loop and do
no per-entry work.

A read cursor holds no server state: it names the bytes the read covered (their
length and a digest of the file name and the bytes just before that length), so
any number of readers can each resume from their own read, in any order, as
often as they like.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from collections.abc import AsyncGenerator, Iterable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from watchfiles import awatch

from core.utils.log_conditions import LoggedConditions
from core.utils.logging import (
    extract_websocket_path_from_message,
    get_logger,
    is_routine_websocket_lifecycle_message,
)
from core.utils.workers import BoundedWorkerPool

JsonObject = dict[str, Any]

_LOGGER = get_logger("log_viewer")

APPEND_EVENT = "append"
RESET_EVENT = "reset"
CATALOG_EVENT = "catalog"
UNKNOWN_LEVEL = "unknown"
UNKNOWN_LOGGER_NAME = ""
UNKNOWN_TIMESTAMP = ""
WATCHER_SHUTDOWN_TIMEOUT_SECONDS = 1.0

# Newest entries per page (``log.read`` and every reset). A page stays a few
# hundred KiB of JSON and a few milliseconds of parsing, covers several screens
# of the Logs view, and bounds what one reset or append event carries.
LOG_PAGE_ENTRIES = 500
# No single read of a log file reads more bytes than this, whatever the file
# holds (a page of huge tracebacks, a long burst between two watcher ticks).
LOG_READ_MAX_BYTES = 4 * 1024 * 1024
# Events a subscriber may have waiting. The watcher emits at most about ten per
# second, so this is a few seconds of backlog; a subscriber that falls further
# behind loses its backlog and is resynchronized with one reset.
LOG_SUBSCRIBER_QUEUE_SIZE = 32
# A page read starts with this much of the file and doubles it until it holds a
# page or reaches the file start or LOG_READ_MAX_BYTES.
_PAGE_CHUNK_BYTES = 64 * 1024
# A cursor's digest covers the file name and at most this many bytes before the
# cursor's end. A truncated, replaced or rotated file (or another file's cursor)
# fails that check, while issuing and checking a cursor stays one small read
# however long the file grows; up to this size the digest covers the whole read.
_CURSOR_WINDOW_BYTES = 64 * 1024

_LOG_WORKERS = BoundedWorkerPool(name="log-viewer", max_workers=2)

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
class _Resync:
    """Queued instead of a subscriber's dropped backlog: reset it to ``tail``."""

    tail: _LogTail
    catalog: tuple[str, ...] | None


# A subscriber queue carries events and resync markers; shutting it down ends the stream.
_QueueItem = JsonObject | _Resync
_SubscriberQueue = asyncio.Queue[_QueueItem]


@dataclass(slots=True)
class _WatcherState:
    file_name: str
    tail: _LogTail
    catalog: tuple[str, ...]
    directory_modified_ns: int | None = None
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    subscribers: list[_SubscriberQueue] = field(default_factory=list)
    task: asyncio.Task[None] | None = None


@dataclass(frozen=True, slots=True)
class _ReadCursor:
    """What one ``read_file`` covered: the file's first ``byte_length`` bytes, by digest."""

    byte_length: int
    digest: str


@dataclass(slots=True)
class _WatchRefresh:
    """What one worker read found for a watched file; ``None`` means not read."""

    catalog: tuple[str, ...] | None = None
    directory_modified_ns: int | None = None
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


def _catalog_event(file_name: str, files: Sequence[str]) -> JsonObject:
    return {
        "type": CATALOG_EVENT,
        "file": file_name,
        "files": list(files),
        "default_file": files[0] if files else None,
    }


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
    try:
        handle = file_path.open("rb")
    except FileNotFoundError:
        return _reset_event(file_name, [], None)
    with handle:
        # A file that changed since is reset again by the watcher's next event.
        end = min(tail.size, os.fstat(handle.fileno()).st_size)
        page = _read_page(handle, end, LOG_PAGE_ENTRIES)
    return _reset_event(file_name, page.entries, page.next_before)


def _deliver(
    queue: _SubscriberQueue,
    events: Sequence[JsonObject],
    tail: _LogTail,
    catalog: tuple[str, ...],
) -> None:
    """Queue ``events``; a subscriber without room for them is resynchronized.

    Its backlog is dropped and replaced by one ``_Resync`` to ``tail``, the state
    the dropped events led to, so later events still apply in order. A stream
    that already ended receives nothing.
    """

    try:
        if queue.maxsize <= 0 or queue.qsize() + len(events) <= queue.maxsize:
            for event in events:
                queue.put_nowait(event)
            return
        include_catalog = any(event["type"] == CATALOG_EVENT for event in events)
        while True:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if isinstance(item, _Resync):
                include_catalog = include_catalog or item.catalog is not None
            elif item.get("type") == CATALOG_EVENT:
                include_catalog = True
        queue.put_nowait(_Resync(tail=tail, catalog=catalog if include_catalog else None))
    except asyncio.QueueShutDown:
        return


def _end_stream(queue: _SubscriberQueue) -> None:
    """End a subscriber's stream now; its backlog no longer matters."""

    queue.shutdown(immediate=True)


class LogViewer:
    """Read daily log files and stream file-specific updates."""

    def __init__(self, data_dir: str | Path) -> None:
        self._logs_dir = Path(data_dir).expanduser() / "logs"
        self._watchers: dict[str, _WatcherState] = {}
        self._watch_lock = asyncio.Lock()
        # Files whose watcher polls currently fail, so a failure logs once per streak.
        self._poll_failures = LoggedConditions(limit=64)

    async def list_files(self) -> JsonObject:
        return await _LOG_WORKERS.run(self._list_files)

    async def read_file(self, file_name: str, *, before: int | None = None) -> JsonObject:
        """Return the newest page of a file, or with ``before`` the page older than it.

        Only a newest-page read returns a ``cursor``, which resumes after it.
        """

        if before is not None:
            name, page = await _LOG_WORKERS.run(self._read_older, file_name, before)
            return {"file": name, "entries": page.entries, "next_before": page.next_before}
        name, page, cursor = await _LOG_WORKERS.run(self._read_newest, file_name)
        return {
            "file": name,
            "entries": page.entries,
            "next_before": page.next_before,
            "cursor": cursor,
        }

    async def subscribe(
        self,
        file_name: str,
        *,
        cursor: str | None = None,
    ) -> AsyncGenerator[JsonObject]:
        """Stream the file's changes and the log directory's catalog changes.

        With a ``read_file`` cursor the stream first replays what changed since
        that read; when the file no longer starts with the bytes the read covered
        (truncated, rotated, rewritten, or another file's cursor), that replay is
        a ``reset`` with the newest page. Without a cursor the stream starts at
        the file's current end. The stream ends when the file's watcher stops
        unexpectedly; the caller then reads again and subscribes to a fresh one.
        Raises ``ValueError`` for an invalid file name or a malformed cursor and
        ``FileNotFoundError`` for a missing file.
        """

        read_cursor = None if cursor is None else _parse_cursor(cursor)
        file_path = await _LOG_WORKERS.run(self._resolve_existing_file, file_name)
        queue: _SubscriberQueue = asyncio.Queue(maxsize=LOG_SUBSCRIBER_QUEUE_SIZE)
        catch_up_subscribers: list[_SubscriberQueue] = []

        # Finding or starting the watcher and registering the queue are one step under
        # the lock that also retires watchers. Split, the last other subscriber could
        # leave in between, evict the watcher and leave this queue on a dead one.
        orphan: asyncio.Task[None] | None = None
        try:
            async with self._watch_lock:
                watcher = await self._ensure_watcher(file_path.name)
                # One read brings the watcher and this reader's cursor to the same point.
                try:
                    watcher.tail, catch_up_event, replay_event = await _LOG_WORKERS.run(
                        _read_subscribe_start, file_path, file_path.name, watcher.tail, read_cursor
                    )
                except BaseException:
                    # No subscriber would ever leave a watcher this failed read started.
                    if not watcher.subscribers and self._watchers.get(watcher.file_name) is watcher:
                        watcher.stop_event.set()
                        del self._watchers[watcher.file_name]
                        orphan = watcher.task
                    raise
                if catch_up_event is not None:
                    catch_up_subscribers = list(watcher.subscribers)
                watcher.subscribers.append(queue)
                tail, catalog = watcher.tail, watcher.catalog
        finally:
            if orphan is not None:
                await _cancel_watcher_task(orphan)

        if catch_up_event is not None:
            for subscriber in catch_up_subscribers:
                _deliver(subscriber, [catch_up_event], tail, catalog)

        if replay_event is not None:
            _deliver(queue, [replay_event], tail, catalog)

        try:
            while True:
                try:
                    item = await queue.get()
                except asyncio.QueueShutDown:
                    return
                if not isinstance(item, _Resync):
                    yield item
                    continue
                if item.catalog is not None:
                    yield _catalog_event(file_path.name, item.catalog)
                yield await _LOG_WORKERS.run(_resync_event, file_path, file_path.name, item.tail)
        except asyncio.CancelledError:
            return
        finally:
            await self._remove_subscriber(watcher, queue)

    async def aclose(self) -> None:
        async with self._watch_lock:
            watchers = list(self._watchers.values())
            self._watchers.clear()

        for watcher in watchers:
            watcher.stop_event.set()

        for watcher in watchers:
            if watcher.task is None:
                continue
            await _cancel_watcher_task(watcher.task)

    @property
    def watcher_count(self) -> int:
        return len(self._watchers)

    def subscriber_count(self, file_name: str) -> int:
        watcher = self._watchers.get(file_name)
        if watcher is None:
            return 0
        return len(watcher.subscribers)

    async def _ensure_watcher(self, file_name: str) -> _WatcherState:
        """Return the file's running watcher, starting one if needed; the caller holds the lock."""

        watcher = self._watchers.get(file_name)
        if watcher is not None:
            if watcher.task is not None and not watcher.task.done():
                return watcher
            # Its watch loop has ended and serves nobody: never attach to it.
            self._retire_locked(watcher)

        directory_modified_ns, catalog, tail = await _LOG_WORKERS.run(
            self._read_watch_start, self._logs_dir / file_name
        )
        watcher = _WatcherState(
            file_name=file_name,
            tail=tail,
            catalog=catalog,
            directory_modified_ns=directory_modified_ns,
        )
        watcher.task = asyncio.create_task(
            self._run_watcher(watcher), name=f"log-watcher:{file_name}"
        )

        def on_done(task: asyncio.Task[None], file_name: str = file_name) -> None:
            _log_watcher_task_result(file_name, task)

        watcher.task.add_done_callback(on_done)
        self._watchers[file_name] = watcher
        return watcher

    def _retire_locked(self, watcher: _WatcherState) -> None:
        """Unregister a watcher whose loop ended and end its subscribers' streams.

        The caller holds the lock. Ending the streams closes their sockets, so each
        accessor reads again and subscribes to a fresh watcher instead of waiting
        on one that no longer delivers anything.
        """
        watcher.stop_event.set()
        if self._watchers.get(watcher.file_name) is watcher:
            del self._watchers[watcher.file_name]
        subscribers, watcher.subscribers = watcher.subscribers, []
        for subscriber in subscribers:
            _end_stream(subscriber)

    async def _remove_subscriber(self, watcher: _WatcherState, queue: _SubscriberQueue) -> None:
        task: asyncio.Task[None] | None = None

        async with self._watch_lock:
            if queue not in watcher.subscribers:
                return
            watcher.subscribers.remove(queue)
            # A retired watcher may already have a successor for the same file.
            if watcher.subscribers or self._watchers.get(watcher.file_name) is not watcher:
                return

            watcher.stop_event.set()
            task = watcher.task
            del self._watchers[watcher.file_name]

        if task is not None:
            await _cancel_watcher_task(task)

    async def _run_watcher(self, watcher: _WatcherState) -> None:
        """Run the watch loop; one that ends before it was stopped retires its watcher."""
        try:
            await self._watch_file(watcher)
        finally:
            if not watcher.stop_event.is_set():
                async with self._watch_lock:
                    self._retire_locked(watcher)

    async def _watch_file(self, watcher: _WatcherState) -> None:
        try:
            async for changes in awatch(
                self._logs_dir,
                recursive=False,
                debounce=100,
                step=50,
                stop_event=watcher.stop_event,
                rust_timeout=100,
                yield_on_timeout=True,
                force_polling=True,
                poll_delay_ms=50,
            ):
                if watcher.stop_event.is_set():
                    continue
                try:
                    await self._poll(watcher, changes)
                except OSError as error:
                    # A share lock or a virus scanner can deny access for a moment.
                    # Nothing was consumed, so the next poll sees the change again.
                    if self._poll_failures.started(watcher.file_name, type(error).__name__):
                        _LOGGER.warning(
                            "Live-log watcher poll failed, retrying (file=%s error=%s: %s)",
                            watcher.file_name,
                            type(error).__name__,
                            error,
                        )
                    continue
                if self._poll_failures.ended(watcher.file_name):
                    _LOGGER.info("Live-log watcher poll recovered (file=%s)", watcher.file_name)
        except asyncio.CancelledError:
            return
        except UnboundLocalError:
            if watcher.stop_event.is_set():
                return
            raise

    async def _poll(self, watcher: _WatcherState, changes: set[tuple[Any, str]]) -> None:
        """Reconcile one watch batch (empty on a timeout) and fan out its events."""
        watched_path = self._logs_dir / watcher.file_name
        should_refresh_catalog = bool(changes)
        should_read_file = _includes_path(changes, str(watched_path))
        if not changes:
            should_refresh_catalog, should_read_file = await _LOG_WORKERS.run(
                _metadata_changes,
                self._logs_dir,
                watcher.directory_modified_ns,
                watched_path,
                watcher.tail,
            )
        if not should_refresh_catalog and not should_read_file:
            return

        async with self._watch_lock:
            refresh = await _LOG_WORKERS.run(
                self._read_refresh,
                watched_path,
                watcher.file_name,
                watcher.tail,
                catalog=should_refresh_catalog,
                content=should_read_file,
            )
            events: list[JsonObject] = []
            if refresh.catalog is not None:
                watcher.directory_modified_ns = refresh.directory_modified_ns
                if refresh.catalog != watcher.catalog:
                    watcher.catalog = refresh.catalog
                    events.append(_catalog_event(watcher.file_name, refresh.catalog))
            if refresh.tail is not None:
                watcher.tail = refresh.tail
                if refresh.event is not None:
                    events.append(refresh.event)
            subscribers = list(watcher.subscribers)
            tail, catalog = watcher.tail, watcher.catalog

        if events:
            for subscriber in subscribers:
                _deliver(subscriber, events, tail, catalog)

    # Worker-pool operations: they touch the filesystem and never watcher state.

    def _list_files(self) -> JsonObject:
        files = sorted(
            (path.name for path in self._iter_log_files()),
            reverse=True,
        )
        return {"files": files, "default_file": files[0] if files else None}

    def _read_newest(self, file_name: str) -> tuple[str, _Page, str]:
        file_path = self._resolve_existing_file(file_name)
        with file_path.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            page = _read_page(handle, size, LOG_PAGE_ENTRIES)
            cursor = _encode_cursor(handle, file_path.name, size)
        return file_path.name, page, cursor

    def _read_older(self, file_name: str, before: int) -> tuple[str, _Page]:
        file_path = self._resolve_existing_file(file_name)
        with file_path.open("rb") as handle:
            if before > os.fstat(handle.fileno()).st_size:
                raise ValueError("log position is past the end of the file")
            page = _read_page(handle, before, LOG_PAGE_ENTRIES)
        return file_path.name, page

    # The directory stamp is always taken before listing, so a file added in
    # between still counts as a change on the next metadata check.

    def _read_watch_start(self, file_path: Path) -> tuple[int | None, tuple[str, ...], _LogTail]:
        directory_modified_ns = _path_modified_ns(self._logs_dir)
        catalog = tuple(self._list_files()["files"])
        return directory_modified_ns, catalog, _read_tail(file_path)

    def _read_refresh(
        self,
        file_path: Path,
        file_name: str,
        tail: _LogTail,
        *,
        catalog: bool,
        content: bool,
    ) -> _WatchRefresh:
        refresh = _WatchRefresh()
        if catalog:
            refresh.directory_modified_ns = _path_modified_ns(self._logs_dir)
            refresh.catalog = tuple(self._list_files()["files"])
        if content:
            refresh.tail, refresh.event = _advance_tails(file_path, file_name, [tail])[0]
        return refresh

    def _iter_log_files(self) -> Iterable[Path]:
        if not self._logs_dir.exists():
            return ()
        return (path for path in self._logs_dir.iterdir() if path.is_file())

    def _resolve_existing_file(self, file_name: str) -> Path:
        normalized_name = self._normalize_file_name(file_name)
        file_path = self._logs_dir / normalized_name
        if not file_path.is_file():
            raise FileNotFoundError(f"log file not found: {normalized_name}")
        return file_path

    def _normalize_file_name(self, file_name: str) -> str:
        if not isinstance(file_name, str) or not file_name:
            raise ValueError("log file name must be a non-empty string")
        if Path(file_name).name != file_name or "/" in file_name or "\\" in file_name:
            raise ValueError(f"invalid log file name: {file_name}")
        return file_name


def _log_watcher_task_result(file_name: str, task: asyncio.Task[None]) -> None:
    """Log a non-cancellation watcher-task crash so the dead tail isn't silent."""
    if task.cancelled():
        return
    error = task.exception()
    if error is None:
        return
    _LOGGER.error(
        "Live-log watcher task crashed for file=%s: %s",
        file_name,
        error,
        exc_info=(type(error), error, error.__traceback__),
    )


async def _cancel_watcher_task(task: asyncio.Task[None]) -> None:
    if _log_watcher_crash_if_already_dead(task):
        # A real exception was already logged and consumed via task.exception();
        # task.cancel() on a finished task is a no-op and result() would re-raise.
        return
    task.cancel()
    done, _pending = await asyncio.wait({task}, timeout=WATCHER_SHUTDOWN_TIMEOUT_SECONDS)
    if task not in done:
        return
    with suppress(asyncio.CancelledError, UnboundLocalError):
        task.result()


def _log_watcher_crash_if_already_dead(task: asyncio.Task[None]) -> bool:
    """Log a real exception from a watcher that already died before teardown.

    Without this the ``suppress(...)`` around ``task.result()`` would silently
    discard a genuine crash. Only inspects (never awaits/raises) and skips the
    normal cancellation case. Returns whether such a crash was logged (and its
    exception thereby retrieved).
    """
    if not task.done() or task.cancelled():
        return False
    error = task.exception()
    if error is None or isinstance(error, asyncio.CancelledError):
        return False
    _LOGGER.error(
        "Live-log watcher task crashed before shutdown: %s",
        error,
        exc_info=(type(error), error, error.__traceback__),
    )
    return True


def _metadata_changes(
    logs_dir: Path,
    directory_modified_ns: int | None,
    file_path: Path,
    tail: _LogTail,
) -> tuple[bool, bool]:
    """Return whether the catalog and the watched file need a re-read."""
    return (
        _path_modified_ns(logs_dir) != directory_modified_ns,
        _file_metadata_changed(file_path, tail),
    )


def _includes_path(changes: set[tuple[Any, str]], watched_path: str) -> bool:
    return any(changed_path == watched_path for _change, changed_path in changes)


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
