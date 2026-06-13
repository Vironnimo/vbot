"""Read-only daily log access and live update watching."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncGenerator, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from watchfiles import awatch

from core.utils.logging import (
    extract_websocket_path_from_message,
    get_logger,
    is_routine_websocket_lifecycle_message,
)

JsonObject = dict[str, Any]

_LOGGER = get_logger("log_viewer")

APPEND_EVENT = "append"
RESET_EVENT = "reset"
UNKNOWN_LEVEL = "unknown"
UNKNOWN_LOGGER_NAME = ""
UNKNOWN_TIMESTAMP = ""
MAX_READ_HANDOFFS = 32
WATCHER_SHUTDOWN_TIMEOUT_SECONDS = 1.0

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


@dataclass(slots=True)
class _WatcherState:
    file_name: str
    snapshot: _LogSnapshot
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    subscribers: list[asyncio.Queue[JsonObject]] = field(default_factory=list)
    task: asyncio.Task[None] | None = None


@dataclass(slots=True)
class _ReadHandoff:
    file_name: str
    snapshot: _LogSnapshot


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
            }
            entries.append(current_entry)
            continue

        current_entry["continuation"] = _append_continuation(
            str(current_entry["continuation"]),
            line,
        )

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
        self._read_handoffs: dict[str, _ReadHandoff] = {}
        self._latest_read_cursor_by_file: dict[str, str] = {}
        self._watchers: dict[str, _WatcherState] = {}
        self._watch_lock = asyncio.Lock()

    def list_files(self) -> JsonObject:
        files = sorted(
            (path.name for path in self._iter_log_files()),
            reverse=True,
        )
        return {"files": files, "default_file": files[0] if files else None}

    def read_file(self, file_name: str) -> JsonObject:
        file_path = self._resolve_existing_file(file_name)
        snapshot = self._read_snapshot(file_path)
        return {
            "file": file_path.name,
            "entries": snapshot.entries,
            "cursor": self._store_read_handoff(file_path.name, snapshot),
        }

    async def subscribe(
        self,
        file_name: str,
        *,
        cursor: str | None = None,
    ) -> AsyncGenerator[JsonObject, None]:
        file_path = self._resolve_existing_file(file_name)
        handoff_snapshot = self._take_read_handoff(file_path.name, cursor)
        watcher = await self._ensure_watcher(file_path.name)
        queue: asyncio.Queue[JsonObject] = asyncio.Queue()
        pending_event: JsonObject | None = None
        catch_up_event: JsonObject | None = None
        catch_up_subscribers: list[asyncio.Queue[JsonObject]] = []

        async with self._watch_lock:
            next_snapshot = self._read_snapshot(file_path)
            previous_snapshot = watcher.snapshot
            catch_up_event = _build_snapshot_event(file_path.name, previous_snapshot, next_snapshot)
            if catch_up_event is not None:
                catch_up_subscribers = list(watcher.subscribers)
            watcher.snapshot = next_snapshot
            watcher.subscribers.append(queue)

            if handoff_snapshot is not None:
                pending_event = _build_snapshot_event(
                    file_path.name, handoff_snapshot, next_snapshot
                )
            else:
                pending_event = _build_snapshot_event(
                    file_path.name, previous_snapshot, next_snapshot
                )

        if catch_up_event is not None:
            for subscriber in catch_up_subscribers:
                subscriber.put_nowait(catch_up_event)

        if pending_event is not None:
            queue.put_nowait(pending_event)

        try:
            while True:
                yield await queue.get()
        except asyncio.CancelledError:
            return
        finally:
            await self._remove_subscriber(file_path.name, queue)

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
        async with self._watch_lock:
            watcher = self._watchers.get(file_name)
            if watcher is not None:
                return watcher

            file_path = self._logs_dir / file_name
            watcher = _WatcherState(
                file_name=file_name,
                snapshot=self._read_snapshot(file_path),
            )
            watcher.task = asyncio.create_task(self._watch_file(watcher))

            def on_done(task: asyncio.Task[None], file_name: str = file_name) -> None:
                _log_watcher_task_result(file_name, task)

            watcher.task.add_done_callback(on_done)
            self._watchers[file_name] = watcher
            return watcher

    async def _remove_subscriber(
        self,
        file_name: str,
        queue: asyncio.Queue[JsonObject],
    ) -> None:
        task: asyncio.Task[None] | None = None

        async with self._watch_lock:
            watcher = self._watchers.get(file_name)
            if watcher is None:
                return

            if queue in watcher.subscribers:
                watcher.subscribers.remove(queue)

            if watcher.subscribers:
                return

            watcher.stop_event.set()
            task = watcher.task
            self._watchers.pop(file_name, None)

        if task is not None:
            await _cancel_watcher_task(task)

    async def _watch_file(self, watcher: _WatcherState) -> None:
        watched_path = self._logs_dir / watcher.file_name
        watched_path_str = str(watched_path)

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

                if changes and not _includes_path(changes, watched_path_str):
                    continue

                async with self._watch_lock:
                    next_snapshot = self._read_snapshot(watched_path)
                    event = _build_snapshot_event(
                        watcher.file_name,
                        watcher.snapshot,
                        next_snapshot,
                    )
                    watcher.snapshot = next_snapshot
                    subscribers = list(watcher.subscribers)
                if event is None:
                    continue

                for subscriber in subscribers:
                    subscriber.put_nowait(event)
        except asyncio.CancelledError:
            return
        except UnboundLocalError:
            if watcher.stop_event.is_set():
                return
            raise

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
        try:
            text = file_path.read_text(encoding="utf-8")
            size = file_path.stat().st_size
        except FileNotFoundError:
            return _LogSnapshot(exists=False, size=0, entries=[])

        return _LogSnapshot(exists=True, size=size, entries=parse_log_entries(text))

    def _store_read_handoff(self, file_name: str, snapshot: _LogSnapshot) -> str:
        previous_cursor = self._latest_read_cursor_by_file.get(file_name)
        if previous_cursor is not None:
            self._read_handoffs.pop(previous_cursor, None)

        cursor = uuid4().hex
        self._read_handoffs[cursor] = _ReadHandoff(file_name=file_name, snapshot=snapshot)
        self._latest_read_cursor_by_file[file_name] = cursor
        self._prune_read_handoffs()
        return cursor

    def _prune_read_handoffs(self) -> None:
        while len(self._read_handoffs) > MAX_READ_HANDOFFS:
            oldest_cursor = next(iter(self._read_handoffs))
            handoff = self._read_handoffs.pop(oldest_cursor)
            if self._latest_read_cursor_by_file.get(handoff.file_name) == oldest_cursor:
                self._latest_read_cursor_by_file.pop(handoff.file_name, None)

    def _take_read_handoff(
        self,
        file_name: str,
        cursor: str | None,
    ) -> _LogSnapshot | None:
        if cursor is None:
            latest_cursor = self._latest_read_cursor_by_file.pop(file_name, None)
            if latest_cursor is None:
                return None
            handoff = self._read_handoffs.pop(latest_cursor, None)
            if handoff is None:
                return None
            return handoff.snapshot
        if not isinstance(cursor, str) or not cursor:
            raise ValueError("log cursor must be a non-empty string")

        handoff = self._read_handoffs.pop(cursor, None)
        if handoff is None or handoff.file_name != file_name:
            raise ValueError("invalid log cursor")
        if self._latest_read_cursor_by_file.get(file_name) == cursor:
            self._latest_read_cursor_by_file.pop(file_name, None)
        return handoff.snapshot


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
