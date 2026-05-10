"""Read-only daily log access and live update watching."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncGenerator, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from watchfiles import awatch

JsonObject = dict[str, Any]

APPEND_EVENT = "append"
RESET_EVENT = "reset"
UNKNOWN_LEVEL = "unknown"
UNKNOWN_LOGGER_NAME = ""
UNKNOWN_TIMESTAMP = ""

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

    def list_files(self) -> JsonObject:
        files = sorted(
            (path.name for path in self._iter_log_files()),
            reverse=True,
        )
        return {"files": files, "default_file": files[0] if files else None}

    def read_file(self, file_name: str) -> JsonObject:
        file_path = self._resolve_existing_file(file_name)
        return {
            "file": file_path.name,
            "entries": parse_log_entries(file_path.read_text(encoding="utf-8")),
        }

    async def subscribe(self, file_name: str) -> AsyncGenerator[JsonObject, None]:
        file_path = self._resolve_existing_file(file_name)
        watcher = await self._ensure_watcher(file_path.name)
        queue: asyncio.Queue[JsonObject] = asyncio.Queue()
        pending_event: JsonObject | None = None

        async with self._watch_lock:
            watcher.subscribers.append(queue)
            next_snapshot = self._read_snapshot(file_path)
            pending_event = _build_snapshot_event(file_path.name, watcher.snapshot, next_snapshot)
            watcher.snapshot = next_snapshot

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
            with suppress(asyncio.CancelledError):
                await watcher.task

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
            task.cancel()
            with suppress(asyncio.CancelledError, UnboundLocalError):
                await task

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


def _includes_path(changes: set[tuple[Any, str]], watched_path: str) -> bool:
    return any(changed_path == watched_path for _change, changed_path in changes)
