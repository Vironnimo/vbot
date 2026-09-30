"""Read-only daily log access and live update watching.

Every log-directory scan, stat, file read, parse and cursor hash runs on the
``log-viewer`` worker pool: a watched daily log is re-read and re-parsed on each
change, up to ten times a second while the server logs, and must never stall the
Event Loop. Watcher state and event fan-out stay on the Event Loop.

A read cursor holds no server state: it names the bytes the read covered (their
length and a digest bound to the file name), so any number of readers can each
resume from their own read, in any order, as often as they like.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from collections.abc import AsyncGenerator, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from watchfiles import awatch

from core.utils.log_conditions import LoggedConditions
from core.utils.logging import (
    extract_websocket_path_from_message,
    get_logger,
    is_routine_websocket_lifecycle_message,
)
from core.utils.workers import BoundedWorkerPool

JsonObject = dict[str, Any]
# A subscriber queue carries events; ``None`` ends the subscriber's stream.
_SubscriberQueue = asyncio.Queue[JsonObject | None]

_LOGGER = get_logger("log_viewer")

APPEND_EVENT = "append"
RESET_EVENT = "reset"
CATALOG_EVENT = "catalog"
UNKNOWN_LEVEL = "unknown"
UNKNOWN_LOGGER_NAME = ""
UNKNOWN_TIMESTAMP = ""
WATCHER_SHUTDOWN_TIMEOUT_SECONDS = 1.0
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
class _LogSnapshot:
    exists: bool
    size: int
    entries: list[JsonObject]
    modified_ns: int | None = None


@dataclass(slots=True)
class _WatcherState:
    file_name: str
    snapshot: _LogSnapshot
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

    catalog: JsonObject | None = None
    directory_modified_ns: int | None = None
    snapshot: _LogSnapshot | None = None


def parse_log_entries(text: str) -> list[JsonObject]:
    """Parse one daily log file into structured entries."""

    entries: list[JsonObject] = []
    current_entry: JsonObject | None = None

    for line in text.splitlines():
        match = LOG_LINE_PATTERN.match(line)
        if match is not None:
            current_entry = {
                "timestamp": match.group("timestamp"),
                "level": match.group("level").lower(),
                "logger_name": match.group("logger_name"),
                "message": match.group("message"),
                "continuation": "",
                "raw": line,
            }
            if _should_include_entry(current_entry):
                entries.append(current_entry)
            continue

        if current_entry is None:
            current_entry = {
                "timestamp": UNKNOWN_TIMESTAMP,
                "level": UNKNOWN_LEVEL,
                "logger_name": UNKNOWN_LOGGER_NAME,
                "message": line,
                "continuation": "",
                "raw": line,
            }
            entries.append(current_entry)
            continue

        # Keep the verbatim source line(s) so the UI can copy an entry exactly as
        # it appears in the file; continuation rows extend both the display tail
        # and the raw block.
        current_entry["continuation"] = _append_continuation(
            str(current_entry["continuation"]),
            line,
        )
        current_entry["raw"] = _append_continuation(str(current_entry["raw"]), line)

    return entries


def _should_include_entry(entry: JsonObject) -> bool:
    message = str(entry["message"])
    return not is_routine_websocket_lifecycle_message(
        level=str(entry["level"]).upper(),
        logger_name=str(entry["logger_name"]),
        message=message,
        websocket_path=extract_websocket_path_from_message(message),
    )


def _append_continuation(existing: str, line: str) -> str:
    if not existing:
        return line
    return f"{existing}\n{line}"


def _build_snapshot_event(
    file_name: str,
    previous: _LogSnapshot,
    current: _LogSnapshot,
) -> JsonObject | None:
    if not current.exists:
        return {"type": RESET_EVENT, "file": file_name, "entries": []}

    if not previous.exists or current.size < previous.size:
        return {"type": RESET_EVENT, "file": file_name, "entries": current.entries}

    prefix_length = len(previous.entries)
    if current.entries[:prefix_length] != previous.entries:
        return {"type": RESET_EVENT, "file": file_name, "entries": current.entries}

    appended_entries = current.entries[prefix_length:]
    if not appended_entries:
        return None

    return {"type": APPEND_EVENT, "file": file_name, "entries": appended_entries}


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

    async def read_file(self, file_name: str) -> JsonObject:
        """Return the file's parsed entries and the cursor that resumes after them."""
        file_path, snapshot, cursor = await _LOG_WORKERS.run(self._read_existing_file, file_name)
        return {"file": file_path.name, "entries": snapshot.entries, "cursor": cursor}

    async def subscribe(
        self,
        file_name: str,
        *,
        cursor: str | None = None,
    ) -> AsyncGenerator[JsonObject, None]:
        """Stream the file's changes and the log directory's catalog changes.

        With a ``read_file`` cursor the stream first replays what changed since
        that read; when the file no longer starts with the bytes the read covered
        (truncated, rotated, rewritten, or another file's cursor), that replay is
        a ``reset`` carrying the whole file. Without a cursor the stream starts at
        the file's current end. The stream ends when the file's watcher stops
        unexpectedly; the caller then reads again and subscribes to a fresh one.
        Raises ``ValueError`` for an invalid file name or a malformed cursor and
        ``FileNotFoundError`` for a missing file.
        """
        read_cursor = None if cursor is None else _parse_cursor(cursor)
        file_path = await _LOG_WORKERS.run(self._resolve_existing_file, file_name)
        queue: _SubscriberQueue = asyncio.Queue()
        pending_event: JsonObject | None = None
        catch_up_event: JsonObject | None = None
        catch_up_subscribers: list[_SubscriberQueue] = []

        # Finding or starting the watcher and registering the queue are one step under
        # the lock that also retires watchers. Split, the last other subscriber could
        # leave in between, evict the watcher and leave this queue on a dead one.
        async with self._watch_lock:
            watcher = await self._ensure_watcher(file_path.name)
            next_snapshot, read_snapshot = await _LOG_WORKERS.run(
                _read_subscribe_start, file_path, read_cursor
            )
            previous_snapshot = watcher.snapshot
            catch_up_event = _build_snapshot_event(file_path.name, previous_snapshot, next_snapshot)
            if catch_up_event is not None:
                catch_up_subscribers = list(watcher.subscribers)
            watcher.snapshot = next_snapshot
            watcher.subscribers.append(queue)

            if read_snapshot is not None:
                pending_event = _build_snapshot_event(file_path.name, read_snapshot, next_snapshot)

        if catch_up_event is not None:
            for subscriber in catch_up_subscribers:
                subscriber.put_nowait(catch_up_event)

        if pending_event is not None:
            queue.put_nowait(pending_event)

        try:
            while (event := await queue.get()) is not None:
                yield event
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

        directory_modified_ns, catalog, snapshot = await _LOG_WORKERS.run(
            self._read_watch_start, self._logs_dir / file_name
        )
        watcher = _WatcherState(
            file_name=file_name,
            snapshot=snapshot,
            catalog=catalog,
            directory_modified_ns=directory_modified_ns,
        )
        watcher.task = asyncio.create_task(self._run_watcher(watcher))

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
            subscriber.put_nowait(None)

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
        should_read_snapshot = _includes_path(changes, str(watched_path))
        if not changes:
            should_refresh_catalog, should_read_snapshot = await _LOG_WORKERS.run(
                _metadata_changes,
                self._logs_dir,
                watcher.directory_modified_ns,
                watched_path,
                watcher.snapshot,
            )
        if not should_refresh_catalog and not should_read_snapshot:
            return

        async with self._watch_lock:
            refresh = await _LOG_WORKERS.run(
                self._read_refresh,
                watched_path,
                catalog=should_refresh_catalog,
                snapshot=should_read_snapshot,
            )
            catalog_event = None
            if refresh.catalog is not None:
                next_catalog = tuple(refresh.catalog["files"])
                watcher.directory_modified_ns = refresh.directory_modified_ns
                if next_catalog != watcher.catalog:
                    watcher.catalog = next_catalog
                    catalog_event = {
                        "type": CATALOG_EVENT,
                        "file": watcher.file_name,
                        **refresh.catalog,
                    }

            event = None
            if refresh.snapshot is not None:
                event = _build_snapshot_event(
                    watcher.file_name,
                    watcher.snapshot,
                    refresh.snapshot,
                )
                watcher.snapshot = refresh.snapshot
            subscribers = list(watcher.subscribers)

        for subscriber in subscribers:
            if catalog_event is not None:
                subscriber.put_nowait(catalog_event)
            if event is not None:
                subscriber.put_nowait(event)

    # Worker-pool operations: they touch the filesystem and never watcher state.

    def _list_files(self) -> JsonObject:
        files = sorted(
            (path.name for path in self._iter_log_files()),
            reverse=True,
        )
        return {"files": files, "default_file": files[0] if files else None}

    def _read_existing_file(self, file_name: str) -> tuple[Path, _LogSnapshot, str]:
        file_path = self._resolve_existing_file(file_name)
        data, snapshot = _read_log(file_path)
        return file_path, snapshot, _encode_cursor(file_path.name, data)

    # The directory stamp is always taken before listing, so a file added in
    # between still counts as a change on the next metadata check.

    def _read_watch_start(
        self, file_path: Path
    ) -> tuple[int | None, tuple[str, ...], _LogSnapshot]:
        directory_modified_ns = _path_modified_ns(self._logs_dir)
        catalog = tuple(self._list_files()["files"])
        return directory_modified_ns, catalog, self._read_snapshot(file_path)

    def _read_refresh(self, file_path: Path, *, catalog: bool, snapshot: bool) -> _WatchRefresh:
        refresh = _WatchRefresh()
        if catalog:
            refresh.directory_modified_ns = _path_modified_ns(self._logs_dir)
            refresh.catalog = self._list_files()
        if snapshot:
            refresh.snapshot = self._read_snapshot(file_path)
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

    def _read_snapshot(self, file_path: Path) -> _LogSnapshot:
        return _read_log(file_path)[1]


def _read_log(file_path: Path) -> tuple[bytes, _LogSnapshot]:
    """Read and parse the file; a missing file reads as no bytes and a missing snapshot."""
    try:
        # Stat first: a write after it changes the metadata again, so the next
        # metadata check re-reads instead of missing it.
        modified_ns = file_path.stat().st_mtime_ns
        data = file_path.read_bytes()
    except FileNotFoundError:
        return b"", _LogSnapshot(exists=False, size=0, entries=[])

    return data, _LogSnapshot(
        exists=True,
        size=len(data),
        entries=parse_log_entries(_decode_log(data)),
        modified_ns=modified_ns,
    )


def _decode_log(data: bytes) -> str:
    # A torn or foreign byte sequence must show as U+FFFD, not stop the viewer.
    return data.decode("utf-8", errors="replace")


def _read_subscribe_start(
    file_path: Path, cursor: _ReadCursor | None
) -> tuple[_LogSnapshot, _LogSnapshot | None]:
    """Read the file now and, for a cursor, rebuild what its read returned.

    The rebuilt snapshot is missing when the file no longer starts with the bytes
    the read covered, so the diff against it is a ``reset`` with the whole file.
    """
    data, snapshot = _read_log(file_path)
    if cursor is None:
        return snapshot, None
    missing = _LogSnapshot(exists=False, size=0, entries=[])
    if not snapshot.exists or cursor.byte_length > len(data):
        return snapshot, missing
    prefix = data[: cursor.byte_length]
    if _prefix_digest(file_path.name, prefix) != cursor.digest:
        return snapshot, missing
    if cursor.byte_length == len(data):
        return snapshot, snapshot
    return snapshot, _LogSnapshot(
        exists=True,
        size=cursor.byte_length,
        entries=parse_log_entries(_decode_log(prefix)),
    )


def _encode_cursor(file_name: str, data: bytes) -> str:
    return f"v1.{len(data)}.{_prefix_digest(file_name, data)}"


def _parse_cursor(cursor: object) -> _ReadCursor:
    match = _CURSOR_PATTERN.fullmatch(cursor) if isinstance(cursor, str) else None
    if match is None:
        raise ValueError("invalid log cursor")
    return _ReadCursor(byte_length=int(match.group(1)), digest=match.group(2))


def _prefix_digest(file_name: str, prefix: bytes) -> str:
    """Digest of the covered bytes, bound to the file name so a cursor never fits another file."""
    digest = hashlib.sha256(os.fsencode(file_name))
    digest.update(b"\0")
    digest.update(prefix)
    return digest.hexdigest()


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
    snapshot: _LogSnapshot,
) -> tuple[bool, bool]:
    """Return whether the catalog and the watched file's snapshot need a re-read."""
    return (
        _path_modified_ns(logs_dir) != directory_modified_ns,
        _snapshot_metadata_changed(file_path, snapshot),
    )


def _includes_path(changes: set[tuple[Any, str]], watched_path: str) -> bool:
    return any(changed_path == watched_path for _change, changed_path in changes)


def _path_modified_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def _snapshot_metadata_changed(file_path: Path, snapshot: _LogSnapshot) -> bool:
    try:
        file_stat = file_path.stat()
    except FileNotFoundError:
        return snapshot.exists
    return (
        not snapshot.exists
        or file_stat.st_size != snapshot.size
        or file_stat.st_mtime_ns != snapshot.modified_ns
    )
