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
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from watchfiles import awatch

from core.utils._log_reader import (
    APPEND_EVENT as APPEND_EVENT,
)
from core.utils._log_reader import (
    LOG_LINE_PATTERN as LOG_LINE_PATTERN,
)
from core.utils._log_reader import (
    LOG_PAGE_ENTRIES as LOG_PAGE_ENTRIES,
)
from core.utils._log_reader import (
    LOG_READ_MAX_BYTES as LOG_READ_MAX_BYTES,
)
from core.utils._log_reader import (
    RESET_EVENT as RESET_EVENT,
)
from core.utils._log_reader import (
    UNKNOWN_LEVEL as UNKNOWN_LEVEL,
)
from core.utils._log_reader import (
    UNKNOWN_LOGGER_NAME as UNKNOWN_LOGGER_NAME,
)
from core.utils._log_reader import (
    UNKNOWN_TIMESTAMP as UNKNOWN_TIMESTAMP,
)
from core.utils._log_reader import (
    JsonObject as JsonObject,
)
from core.utils._log_reader import (
    LogReader,
    _LogTail,
    _parse_cursor,
    _read_subscribe_start,
    _resync_event,
)
from core.utils._log_reader import (
    parse_log_entries as parse_log_entries,
)
from core.utils.log_conditions import LoggedConditions
from core.utils.logging import (
    get_logger,
)
from core.utils.workers import BoundedWorkerPool

_LOGGER = get_logger("log_viewer")

CATALOG_EVENT = "catalog"
WATCHER_SHUTDOWN_TIMEOUT_SECONDS = 1.0

# Events a subscriber may have waiting. The watcher emits at most about ten per
# second, so this is a few seconds of backlog; a subscriber that falls further
# behind loses its backlog and is resynchronized with one reset.
LOG_SUBSCRIBER_QUEUE_SIZE = 32

_LOG_WORKERS = BoundedWorkerPool(name="log-viewer", max_workers=2)


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
    directory_modified_ns: tuple[int | None, ...] | None = None
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    subscribers: list[_SubscriberQueue] = field(default_factory=list)
    task: asyncio.Task[None] | None = None


def _catalog_event(file_name: str, files: Sequence[str]) -> JsonObject:
    return {
        "type": CATALOG_EVENT,
        "file": file_name,
        "files": list(files),
        "default_file": files[0] if files else None,
    }


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

    def __init__(
        self, data_dir: str | Path, *, additional_directories: Mapping[str, Path] | None = None
    ) -> None:
        self._reader = LogReader(data_dir, additional_directories=additional_directories)
        self._watchers: dict[str, _WatcherState] = {}
        self._watch_lock = asyncio.Lock()
        # Files whose watcher polls currently fail, so a failure logs once per streak.
        self._poll_failures = LoggedConditions(limit=64)

    async def list_files(self) -> JsonObject:
        return await _LOG_WORKERS.run(self._reader.list_files)

    async def read_file(self, file_name: str, *, before: int | None = None) -> JsonObject:
        """Return the newest page of a file, or with ``before`` the page older than it.

        Only a newest-page read returns a ``cursor``, which resumes after it.
        """

        if before is not None:
            name, page = await _LOG_WORKERS.run(self._reader.read_older, file_name, before)
            return {"file": name, "entries": page.entries, "next_before": page.next_before}
        name, page, cursor = await _LOG_WORKERS.run(self._reader.read_newest, file_name)
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
        file_path = await _LOG_WORKERS.run(self._reader.resolve_existing_file, file_name)
        queue: _SubscriberQueue = asyncio.Queue(maxsize=LOG_SUBSCRIBER_QUEUE_SIZE)
        catch_up_subscribers: list[_SubscriberQueue] = []

        # Finding or starting the watcher and registering the queue are one step under
        # the lock that also retires watchers. Split, the last other subscriber could
        # leave in between, evict the watcher and leave this queue on a dead one.
        orphan: asyncio.Task[None] | None = None
        try:
            async with self._watch_lock:
                watcher = await self._ensure_watcher(file_name)
                # One read brings the watcher and this reader's cursor to the same point.
                try:
                    watcher.tail, catch_up_event, replay_event = await _LOG_WORKERS.run(
                        _read_subscribe_start, file_path, file_name, watcher.tail, read_cursor
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
                    yield _catalog_event(file_name, item.catalog)
                yield await _LOG_WORKERS.run(_resync_event, file_path, file_name, item.tail)
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
            self._reader.read_watch_start, self._reader.file_path(file_name)
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
                *await _LOG_WORKERS.run(self._reader.watch_directories),
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
        watched_path = self._reader.file_path(watcher.file_name)
        should_refresh_catalog = bool(changes)
        should_read_file = _includes_path(changes, str(watched_path))
        if not changes:
            should_refresh_catalog, should_read_file = await _LOG_WORKERS.run(
                self._reader.metadata_changes,
                watcher.directory_modified_ns,
                watched_path,
                watcher.tail,
            )
        if not should_refresh_catalog and not should_read_file:
            return

        async with self._watch_lock:
            refresh = await _LOG_WORKERS.run(
                self._reader.read_refresh,
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


def _includes_path(changes: set[tuple[Any, str]], watched_path: str) -> bool:
    return any(changed_path == watched_path for _change, changed_path in changes)
