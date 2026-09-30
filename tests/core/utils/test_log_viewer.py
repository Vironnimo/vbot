"""Tests for read-only log viewing utilities."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing, suppress
from pathlib import Path
from typing import Any, Literal

import pytest
from watchfiles import Change

from core.utils import log_viewer as log_viewer_module
from core.utils.log_viewer import (
    LogViewer,
    _build_snapshot_event,
    _cancel_watcher_task,
    _log_watcher_task_result,
    _LogSnapshot,
    _WatcherState,
    parse_log_entries,
)


def test_parse_log_entries_groups_multiline_continuations() -> None:
    entries = parse_log_entries(
        "\n".join(
            [
                "2026-05-11 09:00:00 [INFO] vbot.server.app - Server started",
                "Traceback (most recent call last):",
                '  File "server/app.py", line 10, in create_app',
                "2026-05-11 09:00:01 [WARN] vbot.server.app - Slow request",
            ]
        )
    )

    assert entries == [
        {
            "timestamp": "2026-05-11 09:00:00",
            "level": "info",
            "logger_name": "vbot.server.app",
            "message": "Server started",
            "continuation": (
                'Traceback (most recent call last):\n  File "server/app.py", line 10, in create_app'
            ),
            "raw": (
                "2026-05-11 09:00:00 [INFO] vbot.server.app - Server started\n"
                "Traceback (most recent call last):\n"
                '  File "server/app.py", line 10, in create_app'
            ),
        },
        {
            "timestamp": "2026-05-11 09:00:01",
            "level": "warn",
            "logger_name": "vbot.server.app",
            "message": "Slow request",
            "continuation": "",
            "raw": "2026-05-11 09:00:01 [WARN] vbot.server.app - Slow request",
        },
    ]


def test_parse_log_entries_keeps_orphan_lines_visible() -> None:
    entries = parse_log_entries("orphan line\n2026-05-11 09:00:00 [ERROR] vbot.core - Boom")

    assert entries == [
        {
            "timestamp": "",
            "level": "unknown",
            "logger_name": "",
            "message": "orphan line",
            "continuation": "",
            "raw": "orphan line",
        },
        {
            "timestamp": "2026-05-11 09:00:00",
            "level": "error",
            "logger_name": "vbot.core",
            "message": "Boom",
            "continuation": "",
            "raw": "2026-05-11 09:00:00 [ERROR] vbot.core - Boom",
        },
    ]


def test_parse_log_entries_captures_verbatim_raw_line() -> None:
    source = "2026-05-11 09:00:00 [INFO] vbot.core - hello - world  "

    entries = parse_log_entries(source)

    # ``raw`` is the source line byte-for-byte (trailing spaces and the in-message
    # " - " kept), so the UI can copy an entry 1:1 even though ``message`` is only
    # the post-separator remainder and ``level`` is lower-cased for display.
    assert entries[0]["raw"] == source
    assert entries[0]["message"] == "hello - world  "
    assert entries[0]["level"] == "info"


def test_parse_log_entries_filters_routine_websocket_noise_but_keeps_real_transport_logs() -> None:
    entries = parse_log_entries(
        "\n".join(
            [
                "2026-05-11 09:00:00 [INFO] vbot.server.uvicorn - "
                '127.0.0.1:55090 - "WebSocket /ws" [accepted]',
                "2026-05-11 09:00:01 [INFO] vbot.server.uvicorn - connection open",
                "2026-05-11 09:00:02 [INFO] vbot.server.uvicorn - "
                '127.0.0.1:60756 - "WebSocket /ws/logs?cursor=abc" [accepted]',
                "2026-05-11 09:00:03 [INFO] vbot.server.uvicorn - connection closed",
                "2026-05-11 09:00:04 [WARN] vbot.server.uvicorn - keepalive ping timeout",
                "2026-05-11 09:00:05 [ERROR] vbot.server.uvicorn - opening handshake failed",
                "2026-05-11 09:00:06 [INFO] vbot.server.app - Ready",
            ]
        )
    )

    assert entries == [
        {
            "timestamp": "2026-05-11 09:00:04",
            "level": "warn",
            "logger_name": "vbot.server.uvicorn",
            "message": "keepalive ping timeout",
            "continuation": "",
            "raw": "2026-05-11 09:00:04 [WARN] vbot.server.uvicorn - keepalive ping timeout",
        },
        {
            "timestamp": "2026-05-11 09:00:05",
            "level": "error",
            "logger_name": "vbot.server.uvicorn",
            "message": "opening handshake failed",
            "continuation": "",
            "raw": "2026-05-11 09:00:05 [ERROR] vbot.server.uvicorn - opening handshake failed",
        },
        {
            "timestamp": "2026-05-11 09:00:06",
            "level": "info",
            "logger_name": "vbot.server.app",
            "message": "Ready",
            "continuation": "",
            "raw": "2026-05-11 09:00:06 [INFO] vbot.server.app - Ready",
        },
    ]


@pytest.mark.asyncio
async def test_list_files_returns_newest_first_with_default_selection(tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "2026-05-09").write_text("", encoding="utf-8")
    (logs_dir / "2026-05-11").write_text("", encoding="utf-8")
    (logs_dir / "2026-05-10").write_text("", encoding="utf-8")
    (logs_dir / "subdir").mkdir()

    viewer = LogViewer(tmp_path)

    assert await viewer.list_files() == {
        "files": ["2026-05-11", "2026-05-10", "2026-05-09"],
        "default_file": "2026-05-11",
    }


@pytest.mark.asyncio
async def test_read_file_returns_structured_entries(tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "2026-05-11").write_text(
        "\n".join(
            [
                "2026-05-11 09:00:00 [INFO] vbot.server.app - Server started",
                "details line",
                "2026-05-11 09:00:01 [ERROR] vbot.core - Boom",
            ]
        ),
        encoding="utf-8",
    )

    viewer = LogViewer(tmp_path)

    result = await viewer.read_file("2026-05-11")

    assert result["file"] == "2026-05-11"
    assert result["entries"] == [
        {
            "timestamp": "2026-05-11 09:00:00",
            "level": "info",
            "logger_name": "vbot.server.app",
            "message": "Server started",
            "continuation": "details line",
            "raw": "2026-05-11 09:00:00 [INFO] vbot.server.app - Server started\ndetails line",
        },
        {
            "timestamp": "2026-05-11 09:00:01",
            "level": "error",
            "logger_name": "vbot.core",
            "message": "Boom",
            "continuation": "",
            "raw": "2026-05-11 09:00:01 [ERROR] vbot.core - Boom",
        },
    ]
    assert isinstance(result["cursor"], str)
    assert result["cursor"]


@pytest.mark.asyncio
async def test_read_file_parses_the_log_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "2026-05-11").write_text(
        "2026-05-11 09:00:00 [INFO] vbot.core - Ready\n",
        encoding="utf-8",
    )
    viewer = LogViewer(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    parse = log_viewer_module.parse_log_entries

    def slow_parse(text: str) -> list[dict[str, object]]:
        entered.set()
        release.wait(timeout=5)
        return parse(text)

    monkeypatch.setattr(log_viewer_module, "parse_log_entries", slow_parse)
    reading = asyncio.create_task(viewer.read_file("2026-05-11"))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        loop = asyncio.get_running_loop()
        ticked_at = loop.time()
        for _ in range(5):
            await asyncio.sleep(0.01)
        assert loop.time() - ticked_at < 1
        assert not reading.done()
    finally:
        release.set()
    result = await reading

    assert [entry["message"] for entry in result["entries"]] == ["Ready"]


@pytest.mark.asyncio
async def test_subscribe_pushes_catalog_changes_for_other_log_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    selected_file = logs_dir / "2026-05-11"
    selected_file.write_text(
        "2026-05-11 09:00:00 [INFO] vbot.core - Ready\n",
        encoding="utf-8",
    )
    next_file = logs_dir / "2026-05-12"

    async def fake_awatch(*_args: object, **_kwargs: object):
        next_file.write_text(
            "2026-05-12 00:00:00 [INFO] vbot.core - New day\n",
            encoding="utf-8",
        )
        yield {(Change.added, str(next_file))}
        await asyncio.Event().wait()

    monkeypatch.setattr(log_viewer_module, "awatch", fake_awatch)
    viewer = LogViewer(tmp_path)

    async with aclosing(viewer.subscribe(selected_file.name)) as stream:
        event = await asyncio.wait_for(stream.__anext__(), timeout=1)

    assert event == {
        "type": "catalog",
        "file": selected_file.name,
        "files": [next_file.name, selected_file.name],
        "default_file": next_file.name,
    }


class _InlineWorkers:
    """Runs worker-pool callables on the Event Loop, so a test controls every interleaving."""

    async def run(self, function: Callable[..., Any], *arguments: Any, **keywords: Any) -> Any:
        return function(*arguments, **keywords)


class _ObservedLock(asyncio.Lock):
    """Counts acquire attempts, so a test can order the tasks that queue on the lock."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def acquire(self) -> Literal[True]:
        self.attempts += 1
        return await super().acquire()


async def _until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not condition():
            await asyncio.sleep(0)


async def _next_event(stream: AsyncGenerator[dict[str, Any], None]) -> dict[str, Any]:
    return await stream.__anext__()


class _FakeWatch:
    """Stands in for ``awatch``: yields the change batches a test puts, raises a put exception."""

    def __init__(self) -> None:
        self.changes: asyncio.Queue[set[tuple[Change, str]] | BaseException] = asyncio.Queue()

    async def awatch(self, *_args: object, **_kwargs: object):
        while True:
            item = await self.changes.get()
            if isinstance(item, BaseException):
                raise item
            yield item


def _line(second: int, message: str) -> str:
    return f"2026-05-11 09:00:{second:02d} [INFO] vbot.core - {message}\n"


def _messages(event: dict[str, Any]) -> list[str]:
    return [entry["message"] for entry in event["entries"]]


@pytest.mark.asyncio
async def test_subscribe_replays_what_each_read_missed_from_its_own_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    log_file = logs_dir / "2026-05-11"
    watch = _FakeWatch()
    monkeypatch.setattr(log_viewer_module, "awatch", watch.awatch)
    monkeypatch.setattr(log_viewer_module, "_LOG_WORKERS", _InlineWorkers())
    viewer = LogViewer(tmp_path)

    log_file.write_text(_line(0, "Ready"), encoding="utf-8")
    first_read = await viewer.read_file(log_file.name)
    log_file.write_text(_line(0, "Ready") + _line(1, "Failed"), encoding="utf-8")
    second_read = await viewer.read_file(log_file.name)
    log_file.write_text(_line(0, "Ready") + _line(1, "Failed") + _line(2, "Retried"), "utf-8")

    async def replay(cursor: str) -> dict[str, Any]:
        async with aclosing(viewer.subscribe(log_file.name, cursor=cursor)) as stream:
            return await asyncio.wait_for(stream.__anext__(), timeout=1)

    # A subscriber without a cursor starts at the file's end and takes nothing from the
    # readers; each read's cursor replays its own gap, in any order and more than once.
    live = viewer.subscribe(log_file.name)
    live_event = asyncio.create_task(_next_event(live))
    try:
        await _until(lambda: viewer.subscriber_count(log_file.name) == 1)
        second_replay = await replay(second_read["cursor"])
        first_replay = await replay(first_read["cursor"])
        first_again = await replay(first_read["cursor"])

        log_file.write_text(
            _line(0, "Ready") + _line(1, "Failed") + _line(2, "Retried") + _line(3, "Done"),
            encoding="utf-8",
        )
        watch.changes.put_nowait({(Change.modified, str(log_file))})
        first_live_event = await asyncio.wait_for(live_event, timeout=1)
    finally:
        live_event.cancel()
        await asyncio.gather(live_event, return_exceptions=True)
        await live.aclose()
        await viewer.aclose()

    assert second_replay["type"] == "append"
    assert _messages(second_replay) == ["Retried"]
    assert first_replay == first_again
    assert first_replay["type"] == "append"
    assert _messages(first_replay) == ["Failed", "Retried"]
    assert first_live_event["type"] == "append"
    assert _messages(first_live_event) == ["Done"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("read_name", "read_text", "current_text"),
    [
        pytest.param(
            "2026-05-11", _line(0, "Ready") + _line(1, "Failed"), _line(2, "Reset"), id="truncated"
        ),
        pytest.param(
            "2026-05-11",
            _line(0, "Ready"),
            _line(0, "Reset") + _line(1, "Failed"),
            id="rewritten-in-place",
        ),
        pytest.param(
            "2026-05-10", _line(0, "Ready"), _line(0, "Ready") + _line(1, "Failed"), id="other-file"
        ),
    ],
)
async def test_subscribe_resets_when_the_file_no_longer_starts_with_the_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    read_name: str,
    read_text: str,
    current_text: str,
) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    monkeypatch.setattr(log_viewer_module, "awatch", _FakeWatch().awatch)
    viewer = LogViewer(tmp_path)
    (logs_dir / read_name).write_text(read_text, encoding="utf-8")
    cursor = (await viewer.read_file(read_name))["cursor"]
    (logs_dir / "2026-05-11").write_text(current_text, encoding="utf-8")

    async with aclosing(viewer.subscribe("2026-05-11", cursor=cursor)) as stream:
        event = await asyncio.wait_for(stream.__anext__(), timeout=1)
    await viewer.aclose()

    assert event == {
        "type": "reset",
        "file": "2026-05-11",
        "entries": parse_log_entries(current_text),
    }


@pytest.mark.asyncio
async def test_subscribe_joins_a_live_watcher_when_the_last_subscriber_leaves_meanwhile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    log_file = logs_dir / "2026-05-11"
    ready = "2026-05-11 09:00:00 [INFO] vbot.core - Ready\n"
    log_file.write_text(ready, encoding="utf-8")
    changes: asyncio.Queue[set[tuple[Change, str]]] = asyncio.Queue()

    async def fake_awatch(*_args: object, **_kwargs: object):
        while True:
            yield await changes.get()

    monkeypatch.setattr(log_viewer_module, "awatch", fake_awatch)
    monkeypatch.setattr(log_viewer_module, "_LOG_WORKERS", _InlineWorkers())
    viewer = LogViewer(tmp_path)
    lock = _ObservedLock()
    viewer._watch_lock = lock
    leaving = asyncio.create_task(_next_event(viewer.subscribe(log_file.name)))
    joining: asyncio.Task[dict[str, Any]] | None = None
    try:
        await _until(lambda: viewer.subscriber_count(log_file.name) == 1)

        # The watcher is busy re-reading a large log while the Logs view reconnects: the
        # new socket queues for the lock before the old one is closed.
        await lock.acquire()
        attempts = lock.attempts
        joining = asyncio.create_task(_next_event(viewer.subscribe(log_file.name)))
        await _until(lambda: lock.attempts > attempts)
        leaving.cancel()
        await _until(lambda: lock.attempts > attempts + 1)
        lock.release()
        await _until(leaving.done)

        assert viewer.subscriber_count(log_file.name) == 1
        log_file.write_text(
            ready + "2026-05-11 09:00:01 [INFO] vbot.core - Updated\n", encoding="utf-8"
        )
        changes.put_nowait({(Change.modified, str(log_file))})
        event = await asyncio.wait_for(joining, timeout=1)
        assert [entry["message"] for entry in event["entries"]] == ["Updated"]
    finally:
        tasks = [task for task in (leaving, joining) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await viewer.aclose()


@pytest.mark.asyncio
async def test_watch_file_skips_unchanged_timeouts_and_reconciles_metadata_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    selected_file = logs_dir / "2026-05-11"
    selected_file.write_text(
        "2026-05-11 09:00:00 [INFO] vbot.core - Ready\n",
        encoding="utf-8",
    )
    viewer = LogViewer(tmp_path)
    subscriber: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    watcher = _WatcherState(
        file_name=selected_file.name,
        snapshot=viewer._read_snapshot(selected_file),
        catalog=(selected_file.name,),
        directory_modified_ns=logs_dir.stat().st_mtime_ns,
        subscribers=[subscriber],
    )
    snapshot_reads = 0
    catalog_reads = 0
    read_threads: list[threading.Thread] = []
    original_read_snapshot = viewer._read_snapshot
    original_list_files = viewer._list_files
    awatch_kwargs: dict[str, object] = {}

    def count_snapshot_reads(file_path: Path) -> _LogSnapshot:
        nonlocal snapshot_reads
        snapshot_reads += 1
        read_threads.append(threading.current_thread())
        return original_read_snapshot(file_path)

    def count_catalog_reads() -> dict[str, object]:
        nonlocal catalog_reads
        catalog_reads += 1
        return original_list_files()

    async def fake_awatch(*_args: object, **kwargs: object):
        awatch_kwargs.update(kwargs)
        yield set()
        selected_file.write_text(
            "".join(
                [
                    "2026-05-11 09:00:00 [INFO] vbot.core - Ready\n",
                    "2026-05-11 09:00:01 [INFO] vbot.core - Updated\n",
                ]
            ),
            encoding="utf-8",
        )
        yield set()

    monkeypatch.setattr(viewer, "_read_snapshot", count_snapshot_reads)
    monkeypatch.setattr(viewer, "_list_files", count_catalog_reads)
    monkeypatch.setattr(log_viewer_module, "awatch", fake_awatch)

    await viewer._watch_file(watcher)

    assert awatch_kwargs["yield_on_timeout"] is True
    assert snapshot_reads == 1
    assert catalog_reads == 0
    # The watched log is re-read on every change: never on the Event Loop.
    assert threading.current_thread() not in read_threads
    assert subscriber.get_nowait() == {
        "type": "append",
        "file": selected_file.name,
        "entries": [
            {
                "timestamp": "2026-05-11 09:00:01",
                "level": "info",
                "logger_name": "vbot.core",
                "message": "Updated",
                "continuation": "",
                "raw": "2026-05-11 09:00:01 [INFO] vbot.core - Updated",
            }
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cursor",
    [
        pytest.param("9f86d081884c7d659a2feaa0c55ad015", id="not-a-cursor"),
        pytest.param("v2.0." + "0" * 64, id="unknown-version"),
    ],
)
async def test_subscribe_rejects_a_malformed_cursor(tmp_path: Path, cursor: str) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "2026-05-11").write_text(
        "2026-05-11 09:00:00 [INFO] vbot.server.app - Ready\n",
        encoding="utf-8",
    )

    viewer = LogViewer(tmp_path)

    with pytest.raises(ValueError):
        async with aclosing(viewer.subscribe("2026-05-11", cursor=cursor)) as stream:
            await stream.__anext__()
    assert viewer.watcher_count == 0


@pytest.mark.asyncio
async def test_cancel_watcher_task_returns_when_task_suppresses_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(log_viewer_module, "WATCHER_SHUTDOWN_TIMEOUT_SECONDS", 0.01)
    started = asyncio.Event()
    stop = asyncio.Event()

    async def cancellation_resistant_task() -> None:
        started.set()
        while not stop.is_set():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                if stop.is_set():
                    raise

    task = asyncio.create_task(cancellation_resistant_task())
    await asyncio.wait_for(started.wait(), timeout=1)

    await asyncio.wait_for(_cancel_watcher_task(task), timeout=1)

    assert task.done() is False
    stop.set()
    task.cancel()
    with suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_log_watcher_task_result_ignores_cancellation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def block() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(block())
    await asyncio.sleep(0)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    caplog.set_level(logging.ERROR, logger="vbot.log_viewer")
    _log_watcher_task_result("2026-05-11", task)

    assert [record for record in caplog.records if record.name == "vbot.log_viewer"] == []


@pytest.mark.asyncio
async def test_cancel_watcher_task_logs_a_real_crash_before_suppressing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def crash() -> None:
        raise RuntimeError("watcher boom")

    task = asyncio.create_task(crash())
    with suppress(RuntimeError):
        await task

    caplog.set_level(logging.ERROR, logger="vbot.log_viewer")
    await _cancel_watcher_task(task)

    crash_records = [
        record
        for record in caplog.records
        if record.name == "vbot.log_viewer" and "crashed before shutdown" in record.getMessage()
    ]
    assert len(crash_records) == 1
    assert crash_records[0].exc_info is not None


@pytest.mark.asyncio
async def test_ensure_watcher_attaches_crash_logging_done_callback(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "2026-05-11").write_text(
        "2026-05-11 09:00:00 [INFO] vbot.core - Ready\n", encoding="utf-8"
    )

    viewer = LogViewer(tmp_path)

    async def explode(_watcher: object) -> None:
        raise RuntimeError("awatch exploded")

    # Replace the watch loop so the created task fails with a real exception.
    viewer._watch_file = explode  # type: ignore[method-assign,assignment]

    caplog.set_level(logging.ERROR, logger="vbot.log_viewer")
    async with viewer._watch_lock:
        watcher = await viewer._ensure_watcher("2026-05-11")
    assert watcher.task is not None
    with suppress(RuntimeError):
        await watcher.task

    crash_records = [
        record
        for record in caplog.records
        if record.name == "vbot.log_viewer" and "watcher task crashed" in record.getMessage()
    ]
    assert len(crash_records) == 1
    assert crash_records[0].exc_info is not None
    assert "2026-05-11" in crash_records[0].getMessage()


@pytest.mark.asyncio
async def test_read_file_rejects_invalid_name(tmp_path: Path) -> None:
    viewer = LogViewer(tmp_path)

    with pytest.raises(ValueError):
        await viewer.read_file("../2026-05-11")


def test_build_snapshot_event_appends_only_new_entries() -> None:
    previous = _LogSnapshot(
        exists=True,
        size=10,
        entries=[
            {
                "timestamp": "2026-05-11 09:00:00",
                "level": "info",
                "logger_name": "vbot.server.app",
                "message": "Server started",
                "continuation": "",
            }
        ],
    )
    current = _LogSnapshot(
        exists=True,
        size=20,
        entries=[
            previous.entries[0],
            {
                "timestamp": "2026-05-11 09:00:01",
                "level": "warn",
                "logger_name": "vbot.server.app",
                "message": "Slow request",
                "continuation": "",
            },
        ],
    )

    assert _build_snapshot_event("2026-05-11", previous, current) == {
        "type": "append",
        "file": "2026-05-11",
        "entries": [
            {
                "timestamp": "2026-05-11 09:00:01",
                "level": "warn",
                "logger_name": "vbot.server.app",
                "message": "Slow request",
                "continuation": "",
            }
        ],
    }


def test_build_snapshot_event_resets_when_previous_tail_changes() -> None:
    previous = _LogSnapshot(
        exists=True,
        size=10,
        entries=[
            {
                "timestamp": "2026-05-11 09:00:00",
                "level": "error",
                "logger_name": "vbot.server.app",
                "message": "Boom",
                "continuation": "",
            }
        ],
    )
    current = _LogSnapshot(
        exists=True,
        size=25,
        entries=[
            {
                "timestamp": "2026-05-11 09:00:00",
                "level": "error",
                "logger_name": "vbot.server.app",
                "message": "Boom",
                "continuation": "Traceback line",
            }
        ],
    )

    assert _build_snapshot_event("2026-05-11", previous, current) == {
        "type": "reset",
        "file": "2026-05-11",
        "entries": current.entries,
    }
