"""Tests for read-only log viewing utilities."""

from __future__ import annotations

import asyncio
import logging
import os
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
    _cancel_watcher_task,
    _log_watcher_task_result,
    _read_tail,
    _WatcherState,
    parse_log_entries,
)

_NOISE = "2026-05-11 09:00:01 [INFO] vbot.server.uvicorn - connection open"


def _line(second: int, message: str, level: str = "INFO") -> str:
    return f"2026-05-11 09:00:{second:02d} [{level}] vbot.core - {message}"


def _text(*lines: str) -> str:
    return "".join(f"{line}\n" for line in lines)


def _offset(text: str, line: str) -> int:
    return text.encode("utf-8").index(line.encode("utf-8"))


def _messages(event: dict[str, Any]) -> list[str]:
    return [entry["message"] for entry in event["entries"]]


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_parse_log_entries_groups_multiline_continuations(newline: str) -> None:
    text = newline.join(
        [
            "2026-05-11 09:00:00 [INFO] vbot.server.app - Server started",
            "Traceback (most recent call last):",
            '  File "server/app.py", line 10, in create_app',
            "2026-05-11 09:00:01 [WARN] vbot.server.app - Slow request",
        ]
    )

    entries = parse_log_entries(text)

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
            "offset": 0,
        },
        {
            "timestamp": "2026-05-11 09:00:01",
            "level": "warn",
            "logger_name": "vbot.server.app",
            "message": "Slow request",
            "continuation": "",
            "raw": "2026-05-11 09:00:01 [WARN] vbot.server.app - Slow request",
            # The byte offset of the entry's first line: the paging position.
            "offset": _offset(text, "2026-05-11 09:00:01 [WARN]"),
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
            "offset": 0,
        },
        {
            "timestamp": "2026-05-11 09:00:00",
            "level": "error",
            "logger_name": "vbot.core",
            "message": "Boom",
            "continuation": "",
            "raw": "2026-05-11 09:00:00 [ERROR] vbot.core - Boom",
            "offset": 12,
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
    text = "\n".join(
        [
            "2026-05-11 09:00:00 [INFO] vbot.server.uvicorn - "
            '127.0.0.1:55090 - "WebSocket /ws" [accepted]',
            _NOISE,
            "2026-05-11 09:00:02 [INFO] vbot.server.uvicorn - "
            '127.0.0.1:60756 - "WebSocket /ws/logs?cursor=abc" [accepted]',
            "2026-05-11 09:00:03 [INFO] vbot.server.uvicorn - connection closed",
            "2026-05-11 09:00:04 [WARN] vbot.server.uvicorn - keepalive ping timeout",
            "2026-05-11 09:00:05 [ERROR] vbot.server.uvicorn - opening handshake failed",
            "2026-05-11 09:00:06 [INFO] vbot.server.app - Ready",
        ]
    )

    entries = parse_log_entries(text)

    assert [
        (entry["level"], entry["logger_name"], entry["message"], entry["offset"])
        for entry in entries
    ] == [
        (
            "warn",
            "vbot.server.uvicorn",
            "keepalive ping timeout",
            _offset(text, "2026-05-11 09:00:04"),
        ),
        (
            "error",
            "vbot.server.uvicorn",
            "opening handshake failed",
            _offset(text, "2026-05-11 09:00:05"),
        ),
        ("info", "vbot.server.app", "Ready", _offset(text, "2026-05-11 09:00:06")),
    ]


@pytest.mark.asyncio
async def test_list_files_returns_newest_first_with_default_selection(tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "2026-05-09").write_text("", encoding="utf-8")
    (logs_dir / "2026-05-11").write_text("", encoding="utf-8")
    (logs_dir / "2026-05-10").write_text("", encoding="utf-8")
    (logs_dir / "server-crash.log").write_text("", encoding="utf-8")
    (logs_dir / "server-crash.log.1").write_text("", encoding="utf-8")
    (logs_dir / "subdir").mkdir()

    viewer = LogViewer(tmp_path)

    # Other files, such as the crash log, follow the daily files and are never the default.
    assert await viewer.list_files() == {
        "files": [
            "2026-05-11",
            "2026-05-10",
            "2026-05-09",
            "server-crash.log",
            "server-crash.log.1",
        ],
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
        newline="\n",
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
            "offset": 0,
        },
        {
            "timestamp": "2026-05-11 09:00:01",
            "level": "error",
            "logger_name": "vbot.core",
            "message": "Boom",
            "continuation": "",
            "raw": "2026-05-11 09:00:01 [ERROR] vbot.core - Boom",
            "offset": 73,
        },
    ]
    assert result["next_before"] is None
    assert isinstance(result["cursor"], str)
    assert result["cursor"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "read_max_bytes", [4 * 1024 * 1024, 160], ids=["whole-pages", "byte-capped-pages"]
)
async def test_read_file_pages_from_the_newest_entries_to_the_file_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read_max_bytes: int
) -> None:
    monkeypatch.setattr(log_viewer_module, "LOG_PAGE_ENTRIES", 2)
    monkeypatch.setattr(log_viewer_module, "_PAGE_CHUNK_BYTES", 32)
    monkeypatch.setattr(log_viewer_module, "LOG_READ_MAX_BYTES", read_max_bytes)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    lines = [
        _line(0, "First", "ERROR"),
        "Traceback (most recent call last):",
        "  RuntimeError: boom",
        _NOISE,
        *(_line(second, f"Entry {second}") for second in range(2, 7)),
    ]
    text = "\n".join(lines) + "\n"
    (logs_dir / "2026-05-11").write_text(text, encoding="utf-8", newline="\n")
    viewer = LogViewer(tmp_path)

    newest = await viewer.read_file("2026-05-11")
    pages = [newest]
    while pages[-1]["next_before"] is not None:
        older = await viewer.read_file("2026-05-11", before=pages[-1]["next_before"])
        # Only the newest page hands a cursor over to the live stream.
        assert "cursor" not in older
        pages.append(older)

    assert [entry["message"] for entry in newest["entries"]] == ["Entry 5", "Entry 6"]
    assert newest["next_before"] == _offset(text, _line(5, "Entry 5"))
    # Following next_before yields every visible entry once, in file order.
    paged = [entry for page in reversed(pages) for entry in page["entries"]]
    assert paged == parse_log_entries(text)
    with pytest.raises(ValueError):
        await viewer.read_file("2026-05-11", before=len(text.encode("utf-8")) + 1)


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
    parse = log_viewer_module._parse_lines

    def slow_parse(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        release.wait(timeout=5)
        return parse(*args, **kwargs)

    monkeypatch.setattr(log_viewer_module, "_parse_lines", slow_parse)
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
@pytest.mark.parametrize("change", [Change.added, Change.deleted], ids=["new-day", "deleted"])
async def test_subscribe_pushes_catalog_changes_for_other_log_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: Change,
) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    selected_file = logs_dir / "2026-05-11"
    selected_file.write_text(
        "2026-05-11 09:00:00 [INFO] vbot.core - Ready\n",
        encoding="utf-8",
    )
    # A new day's file appears, or log retention deletes an old one.
    other_file = logs_dir / ("2026-05-12" if change == Change.added else "2026-05-10")
    if change == Change.deleted:
        other_file.write_text("2026-05-10 00:00:00 [INFO] vbot.core - Old day\n", encoding="utf-8")

    async def fake_awatch(*_args: object, **_kwargs: object):
        if change == Change.added:
            other_file.write_text(
                "2026-05-12 00:00:00 [INFO] vbot.core - New day\n",
                encoding="utf-8",
            )
        else:
            other_file.unlink()
        yield {(change, str(other_file))}
        await asyncio.Event().wait()

    monkeypatch.setattr(log_viewer_module, "awatch", fake_awatch)
    viewer = LogViewer(tmp_path)

    async with aclosing(viewer.subscribe(selected_file.name)) as stream:
        event = await asyncio.wait_for(stream.__anext__(), timeout=1)

    files = (
        [other_file.name, selected_file.name] if change == Change.added else [selected_file.name]
    )
    assert event == {
        "type": "catalog",
        "file": selected_file.name,
        "files": files,
        "default_file": files[0],
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


async def _next_event(stream: AsyncGenerator[dict[str, Any]]) -> dict[str, Any]:
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


class _LiveFile:
    """A watched log whose watcher sees a change batch only when the test sends one."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        self.path = logs_dir / "2026-05-11"
        self.path.write_bytes(text.encode("utf-8"))
        self.watch = _FakeWatch()
        self.viewer = LogViewer(tmp_path)
        monkeypatch.setattr(log_viewer_module, "awatch", self.watch.awatch)
        monkeypatch.setattr(log_viewer_module, "_LOG_WORKERS", _InlineWorkers())

    def changed(self) -> None:
        self.watch.changes.put_nowait({(Change.modified, str(self.path))})

    def append(self, text: str) -> None:
        with self.path.open("ab") as handle:
            handle.write(text.encode("utf-8"))
        self.changed()

    def replace(self, text: str) -> None:
        self.path.write_bytes(text.encode("utf-8"))
        self.changed()

    async def subscribe(self) -> AsyncGenerator[dict[str, Any]]:
        read = await self.viewer.read_file(self.path.name)
        return self.viewer.subscribe(self.path.name, cursor=read["cursor"])

    async def next_after(
        self, stream: AsyncGenerator[dict[str, Any]], change: Callable[[], None]
    ) -> dict[str, Any]:
        pending = asyncio.ensure_future(stream.__anext__())
        await _until(lambda: self.viewer.subscriber_count(self.path.name) == 1)
        change()
        return await asyncio.wait_for(pending, timeout=1)


@pytest.mark.asyncio
async def test_subscribe_replays_what_each_read_missed_from_its_own_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(log_viewer_module, "_CURSOR_WINDOW_BYTES", 128)
    monkeypatch.setattr(log_viewer_module, "_PAGE_CHUNK_BYTES", 128)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    log_file = logs_dir / "2026-05-11"
    watch = _FakeWatch()
    monkeypatch.setattr(log_viewer_module, "awatch", watch.awatch)
    monkeypatch.setattr(log_viewer_module, "_LOG_WORKERS", _InlineWorkers())
    viewer = LogViewer(tmp_path)
    history = _text(*(_line(second % 60, f"History {second}") for second in range(100)))

    log_file.write_bytes((history + _text(_line(0, "Ready"))).encode())
    first_read = await viewer.read_file(log_file.name)
    # The second read catches the next entry half written.
    second_text = history + _text(_line(0, "Ready"), _line(1, "Failed")) + _line(2, "Retr")
    log_file.write_bytes(second_text.encode())
    second_read = await viewer.read_file(log_file.name)
    log_file.write_bytes(
        (history + _text(_line(0, "Ready"), _line(1, "Failed"), _line(2, "Retried"))).encode()
    )
    read_starts: list[int] = []
    read_range = log_viewer_module._read_range

    def record_range(handle: Any, start: int, end: int) -> bytes:
        read_starts.append(start)
        return read_range(handle, start, end)

    async def replay(cursor: str) -> dict[str, Any]:
        async with aclosing(viewer.subscribe(log_file.name, cursor=cursor)) as stream:
            return await asyncio.wait_for(stream.__anext__(), timeout=1)

    # A subscriber without a cursor starts at the file's end and takes nothing from the
    # readers; each read's cursor replays its own gap, in any order and more than once.
    live = viewer.subscribe(log_file.name)
    live_event = asyncio.create_task(_next_event(live))
    try:
        await _until(lambda: viewer.subscriber_count(log_file.name) == 1)
        monkeypatch.setattr(log_viewer_module, "_read_range", record_range)
        second_replay = await replay(second_read["cursor"])
        first_replay = await replay(first_read["cursor"])
        first_again = await replay(first_read["cursor"])

        log_file.write_bytes(
            (
                history
                + _text(
                    _line(0, "Ready"), _line(1, "Failed"), _line(2, "Retried"), _line(3, "Done")
                )
            ).encode()
        )
        watch.changes.put_nowait({(Change.modified, str(log_file))})
        first_live_event = await asyncio.wait_for(live_event, timeout=1)
    finally:
        live_event.cancel()
        await asyncio.gather(live_event, return_exceptions=True)
        await live.aclose()
        await viewer.aclose()

    assert second_replay["type"] == "append"
    # The half-written entry the read showed is replaced by its complete form.
    assert second_replay["from_offset"] == _offset(second_text, _line(2, "Retr"))
    assert _messages(second_replay) == ["Retried"]
    assert first_replay == first_again
    assert first_replay["type"] == "append"
    assert first_replay["from_offset"] == _offset(second_text, _line(1, "Failed"))
    assert _messages(first_replay) == ["Failed", "Retried"]
    assert first_live_event["type"] == "append"
    assert _messages(first_live_event) == ["Done"]
    # Checking and resuming a cursor reads only a bounded window before its end,
    # never the history before it, however long the file is.
    assert min(read_starts) >= len(history) - 128


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("read_name", "read_text", "current_text"),
    [
        pytest.param(
            "2026-05-11",
            _text(_line(0, "Ready"), _line(1, "Failed")),
            _text(_line(2, "Reset")),
            id="truncated",
        ),
        pytest.param(
            "2026-05-11",
            _text(_line(0, "Ready")),
            _text(_line(0, "Reset"), _line(1, "Failed")),
            id="rewritten-in-place",
        ),
        pytest.param(
            "2026-05-10",
            _text(_line(0, "Ready")),
            _text(_line(0, "Ready"), _line(1, "Failed")),
            id="other-file",
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
    (logs_dir / read_name).write_bytes(read_text.encode())
    cursor = (await viewer.read_file(read_name))["cursor"]
    (logs_dir / "2026-05-11").write_bytes(current_text.encode())

    async with aclosing(viewer.subscribe("2026-05-11", cursor=cursor)) as stream:
        event = await asyncio.wait_for(stream.__anext__(), timeout=1)
    await viewer.aclose()

    assert event == {
        "type": "reset",
        "file": "2026-05-11",
        "entries": parse_log_entries(current_text),
        "next_before": None,
    }


@pytest.mark.asyncio
async def test_subscribe_tails_only_the_open_entry_and_the_appended_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = _text(_line(0, "Ready"), _line(1, "Serving"))
    live = _LiveFile(tmp_path, monkeypatch, prefix)
    reads: list[tuple[int, int]] = []
    read_range = log_viewer_module._read_range

    def record_range(handle: Any, start: int, end: int) -> bytes:
        reads.append((start, end))
        return read_range(handle, start, end)

    monkeypatch.setattr(log_viewer_module, "_read_range", record_range)
    serving_at = _offset(prefix, _line(1, "Serving"))
    failed_at = len(prefix)

    def append_partial_line() -> None:
        reads.clear()  # Starting the subscription read the file once.
        live.append(_line(2, "Fai", "ERROR"))

    stream = await live.subscribe()
    try:
        # A partial last line is shown as written so far...
        partial = await live.next_after(stream, append_partial_line)
        # ...and replaced by its complete form once the rest arrives.
        completed = await live.next_after(stream, lambda: live.append("led\n"))
        # A traceback line continues the open entry, which is replaced again.
        continued = await live.next_after(stream, lambda: live.append("  RuntimeError: boom\n"))
        # A new entry closes it: only the new entry is sent.
        appended = await live.next_after(stream, lambda: live.append(_text(_line(3, "Next"))))
    finally:
        await stream.aclose()
        await live.viewer.aclose()

    assert (partial["from_offset"], _messages(partial)) == (failed_at, ["Fai"])
    assert (completed["from_offset"], [entry["raw"] for entry in completed["entries"]]) == (
        failed_at,
        [_line(2, "Failed", "ERROR")],
    )
    assert continued["from_offset"] == failed_at
    assert continued["entries"][0]["continuation"] == "  RuntimeError: boom"
    assert appended["type"] == "append"
    assert _messages(appended) == ["Next"]
    assert appended["from_offset"] == appended["entries"][0]["offset"]
    # Each change re-read the file from the open entry's start, never from the top.
    assert [start for start, _end in reads] == [serving_at, failed_at, failed_at, failed_at]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["truncated", "replaced"])
async def test_subscribe_resets_when_the_file_shrinks_or_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    live = _LiveFile(tmp_path, monkeypatch, _text(_line(0, "Ready"), _line(1, "Serving")))
    stream = await live.subscribe()

    def rewrite() -> None:
        if change == "truncated":
            live.replace(_text(_line(5, "Restarted")))
            return
        # A replacement larger than the old file still counts as a new file.
        replacement = live.path.with_name("replacement")
        lines = [_line(second, f"Replaced {second}") for second in range(5, 9)]
        replacement.write_bytes(_text(*lines).encode())
        os.replace(replacement, live.path)
        live.changed()

    try:
        event = await live.next_after(stream, rewrite)
    finally:
        await stream.aclose()
        await live.viewer.aclose()

    assert event["type"] == "reset"
    assert event["next_before"] is None
    assert event["entries"] == parse_log_entries(live.path.read_bytes().decode("utf-8"))


@pytest.mark.asyncio
async def test_subscriber_that_falls_behind_is_resynchronized_with_one_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(log_viewer_module, "LOG_SUBSCRIBER_QUEUE_SIZE", 2)
    live = _LiveFile(tmp_path, monkeypatch, _text(_line(0, "Ready")))
    stream = await live.subscribe()
    try:
        first = await live.next_after(stream, lambda: live.append(_text(_line(1, "One"))))
        # The subscriber reads nothing while four more changes arrive: its queue
        # holds two events, so its backlog is dropped for a resynchronization.
        for second in range(2, 6):
            live.append(_text(_line(second, f"Burst {second}")))
            await _until(live.watch.changes.empty)
            await asyncio.sleep(0)
        resync = await asyncio.wait_for(stream.__anext__(), timeout=1)
        after = await asyncio.wait_for(stream.__anext__(), timeout=1)
    finally:
        await stream.aclose()
        await live.viewer.aclose()

    assert _messages(first) == ["One"]
    assert resync["type"] == "reset"
    assert _messages(resync) == ["Ready", "One", "Burst 2", "Burst 3", "Burst 4"]
    # Events after the dropped backlog continue from the reset.
    assert after["type"] == "append"
    assert _messages(after) == ["Burst 5"]


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
    ready = "2026-05-11 09:00:00 [INFO] vbot.core - Ready\n"
    selected_file.write_text(ready, encoding="utf-8", newline="\n")
    viewer = LogViewer(tmp_path)
    subscriber: asyncio.Queue[Any] = asyncio.Queue()
    watcher = _WatcherState(
        file_name=selected_file.name,
        tail=_read_tail(selected_file),
        catalog=(selected_file.name,),
        directory_modified_ns=logs_dir.stat().st_mtime_ns,
        subscribers=[subscriber],
    )
    file_reads = 0
    catalog_reads = 0
    read_threads: list[threading.Thread] = []
    advance_tails = log_viewer_module._advance_tails
    original_list_files = viewer._list_files
    awatch_kwargs: dict[str, object] = {}

    def count_file_reads(*args: Any) -> Any:
        nonlocal file_reads
        file_reads += 1
        read_threads.append(threading.current_thread())
        return advance_tails(*args)

    def count_catalog_reads() -> dict[str, object]:
        nonlocal catalog_reads
        catalog_reads += 1
        return original_list_files()

    async def fake_awatch(*_args: object, **kwargs: object):
        awatch_kwargs.update(kwargs)
        yield set()
        selected_file.write_text(
            ready + "2026-05-11 09:00:01 [INFO] vbot.core - Updated\n",
            encoding="utf-8",
            newline="\n",
        )
        yield set()

    monkeypatch.setattr(log_viewer_module, "_advance_tails", count_file_reads)
    monkeypatch.setattr(viewer, "_list_files", count_catalog_reads)
    monkeypatch.setattr(log_viewer_module, "awatch", fake_awatch)

    await viewer._watch_file(watcher)

    assert awatch_kwargs["yield_on_timeout"] is True
    assert file_reads == 1
    assert catalog_reads == 0
    # The watched log is re-read on every change: never on the Event Loop.
    assert threading.current_thread() not in read_threads
    assert subscriber.get_nowait() == {
        "type": "append",
        "file": selected_file.name,
        "from_offset": len(ready),
        "entries": [
            {
                "timestamp": "2026-05-11 09:00:01",
                "level": "info",
                "logger_name": "vbot.core",
                "message": "Updated",
                "continuation": "",
                "raw": "2026-05-11 09:00:01 [INFO] vbot.core - Updated",
                "offset": len(ready),
            }
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cursor", "start_read_error", "expected"),
    [
        pytest.param("9f86d081884c7d659a2feaa0c55ad015", None, ValueError, id="not-a-cursor"),
        pytest.param("v2.0." + "0" * 64, None, ValueError, id="unknown-version"),
        # A share lock or a virus scanner denies the read after the watcher started.
        pytest.param(None, PermissionError("locked"), PermissionError, id="unreadable-file"),
    ],
)
async def test_a_failed_subscribe_leaves_no_watcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cursor: str | None,
    start_read_error: Exception | None,
    expected: type[Exception],
) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "2026-05-11").write_text(
        "2026-05-11 09:00:00 [INFO] vbot.server.app - Ready\n",
        encoding="utf-8",
    )
    watch = _FakeWatch()
    monkeypatch.setattr(log_viewer_module, "awatch", watch.awatch)
    monkeypatch.setattr(log_viewer_module, "_LOG_WORKERS", _InlineWorkers())
    if start_read_error is not None:

        def fail_start_read(*_arguments: object) -> None:
            raise start_read_error

        monkeypatch.setattr(log_viewer_module, "_read_subscribe_start", fail_start_read)

    viewer = LogViewer(tmp_path)

    try:
        with pytest.raises(expected):
            async with aclosing(viewer.subscribe("2026-05-11", cursor=cursor)) as stream:
                await stream.__anext__()
        assert viewer.watcher_count == 0
    finally:
        await viewer.aclose()


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
async def test_a_crashed_watcher_ends_its_streams_and_the_next_subscriber_gets_a_fresh_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(log_viewer_module, "LOG_SUBSCRIBER_QUEUE_SIZE", 1)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    log_file = logs_dir / "2026-05-11"
    log_file.write_bytes(_text(_line(0, "Ready")).encode())
    watch = _FakeWatch()
    monkeypatch.setattr(log_viewer_module, "awatch", watch.awatch)
    monkeypatch.setattr(log_viewer_module, "_LOG_WORKERS", _InlineWorkers())
    caplog.set_level(logging.ERROR, logger="vbot.log_viewer")
    viewer = LogViewer(tmp_path)
    crashed = viewer.subscribe(log_file.name)
    fresh = viewer.subscribe(log_file.name)
    crashed_next = asyncio.create_task(_next_event(crashed))
    fresh_next: asyncio.Task[dict[str, Any]] | None = None
    try:
        await _until(lambda: viewer.subscriber_count(log_file.name) == 1)
        crashed_queue = viewer._watchers[log_file.name].subscribers[0]
        log_file.write_bytes(_text(_line(0, "Ready"), _line(1, "Seen")).encode())
        watch.changes.put_nowait({(Change.modified, str(log_file))})
        assert _messages(await asyncio.wait_for(crashed_next, timeout=1)) == ["Seen"]
        # Its reader falls behind: the stream's queue is full when the watcher dies.
        log_file.write_bytes(
            _text(_line(0, "Ready"), _line(1, "Seen"), _line(2, "Unread")).encode()
        )
        watch.changes.put_nowait({(Change.modified, str(log_file))})
        await _until(crashed_queue.full)
        watch.changes.put_nowait(RuntimeError("awatch exploded"))
        await _until(lambda: viewer.watcher_count == 0)

        # The stream ends, so the socket closes and the accessor reconnects,
        # instead of waiting on a watcher that delivers nothing any more.
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(crashed.__anext__(), timeout=1)

        fresh_next = asyncio.create_task(_next_event(fresh))
        await _until(lambda: viewer.subscriber_count(log_file.name) == 1)
        log_file.write_bytes(
            _text(
                _line(0, "Ready"), _line(1, "Seen"), _line(2, "Unread"), _line(3, "Updated")
            ).encode()
        )
        watch.changes.put_nowait({(Change.modified, str(log_file))})
        event = await asyncio.wait_for(fresh_next, timeout=1)
    finally:
        tasks = [task for task in (crashed_next, fresh_next) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await crashed.aclose()
        await fresh.aclose()
        await viewer.aclose()

    assert _messages(event) == ["Updated"]
    crash_records = [
        record
        for record in caplog.records
        if record.name == "vbot.log_viewer" and record.levelno == logging.ERROR
    ]
    assert len(crash_records) == 1
    assert crash_records[0].exc_info is not None
    assert log_file.name in crash_records[0].getMessage()


@pytest.mark.asyncio
async def test_watch_file_skips_polls_it_cannot_read_and_catches_up_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    log_file = logs_dir / "2026-05-11"
    ready = _text(_line(0, "Ready"))
    log_file.write_bytes(ready.encode())
    viewer = LogViewer(tmp_path)
    subscriber: asyncio.Queue[Any] = asyncio.Queue()
    watcher = _WatcherState(
        file_name=log_file.name,
        tail=_read_tail(log_file),
        catalog=(log_file.name,),
        directory_modified_ns=logs_dir.stat().st_mtime_ns,
        subscribers=[subscriber],
    )
    denied = PermissionError(13, "The process cannot access the file", str(log_file))
    metadata_changes = log_viewer_module._metadata_changes
    advance_tails = log_viewer_module._advance_tails
    metadata_failures = [denied]
    read_failures = [denied]

    def locked_metadata_changes(*arguments: Any) -> tuple[bool, bool]:
        if metadata_failures:
            raise metadata_failures.pop()
        return metadata_changes(*arguments)

    def locked_advance_tails(*arguments: Any) -> Any:
        if read_failures:
            raise read_failures.pop()
        return advance_tails(*arguments)

    async def fake_awatch(*_args: object, **_kwargs: object):
        # Undecodable bytes (a torn or foreign write) must not stop the watcher either.
        log_file.write_bytes(
            ready.encode() + b"2026-05-11 09:00:01 [INFO] vbot.core - bad \xff byte\n"
        )
        yield set()
        yield {(Change.modified, str(log_file))}
        yield set()

    monkeypatch.setattr(log_viewer_module, "_metadata_changes", locked_metadata_changes)
    monkeypatch.setattr(log_viewer_module, "_advance_tails", locked_advance_tails)
    monkeypatch.setattr(log_viewer_module, "awatch", fake_awatch)
    caplog.set_level(logging.INFO, logger="vbot.log_viewer")

    await viewer._watch_file(watcher)

    assert subscriber.get_nowait() == {
        "type": "append",
        "file": log_file.name,
        "from_offset": len(ready),
        "entries": parse_log_entries(
            ready + "2026-05-11 09:00:01 [INFO] vbot.core - bad \ufffd byte\n"
        )[1:],
    }
    assert subscriber.empty()
    # Two failed polls in a row are one degradation, logged once, then its recovery.
    levels = [record.levelno for record in caplog.records if record.name == "vbot.log_viewer"]
    assert levels == [logging.WARNING, logging.INFO]


@pytest.mark.asyncio
async def test_read_file_rejects_invalid_name(tmp_path: Path) -> None:
    viewer = LogViewer(tmp_path)

    with pytest.raises(ValueError):
        await viewer.read_file("../2026-05-11")
