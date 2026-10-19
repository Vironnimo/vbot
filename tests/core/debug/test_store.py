"""Tests for DebugTraceStore write, read, list, prune, and clear behavior."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from core.debug import store as store_module
from core.debug.store import DebugTraceStore, InvalidTraceIdError, drain_debug_traces
from core.storage.layout import DataDirectoryLayout

TRACE_ID_1 = "00000000000040008000000000000001"
TRACE_ID_2 = "00000000000040008000000000000002"
TRACE_ID_3 = "00000000000040008000000000000003"
TRACE_ID_4 = "00000000000040008000000000000004"


def _make_trace_data(
    trace_id: str,
    timestamp: str,
    provider_id: str = "openai",
    model_id: str = "gpt-4",
    request_method: str = "POST",
    request_url: str = "https://api.example.com/v1/chat",
    status_code: int | None = 200,
    duration_ms: int | None = 150,
) -> dict:
    """Build a realistic trace payload used for store tests."""
    return {
        "trace_id": trace_id,
        "type": "provider_request",
        "timestamp": timestamp,
        "provider_id": provider_id,
        "model_id": model_id,
        "duration_ms": duration_ms,
        "request": {
            "method": request_method,
            "url": request_url,
            "headers": {"Content-Type": "application/json"},
            "body": {"model": model_id, "messages": [{"role": "user", "content": "hello"}]},
        },
        "response": {
            "status_code": status_code,
            "headers": {"Content-Type": "application/json"},
            "body": {"choices": [{"message": {"content": "hi"}}]},
        },
    }


class _BlockedTraceWrites:
    """Blocks the process-wide trace thread inside its next trace-file write."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    async def wait_until_blocked(self) -> None:
        for _ in range(500):
            if self.entered.is_set():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("trace thread never reached the blocked write")


@pytest.fixture
def blocked_trace_writes(monkeypatch: pytest.MonkeyPatch) -> Iterator[_BlockedTraceWrites]:
    blocked = _BlockedTraceWrites()
    write = store_module.atomic_write_text

    def slow_write(path: Path, text: str) -> None:
        if not blocked.release.is_set():
            blocked.entered.set()
            blocked.release.wait(timeout=5)
        write(path, text)

    monkeypatch.setattr(store_module, "atomic_write_text", slow_write)
    try:
        yield blocked
    finally:
        # The trace thread is process-wide: never leave it blocked for later tests.
        blocked.release.set()


# ---------------------------------------------------------------------------
# Save trace & index metadata
# ---------------------------------------------------------------------------


class TestSaveTrace:
    @pytest.mark.parametrize(
        "index_problem",
        [
            "unreadable",
            "invalid_json",
            "invalid_root",
            "invalid_entry",
            "invalid_timestamp",
            "invalid_utf8",
        ],
    )
    def test_unusable_index_refuses_save_without_changing_captures(
        self, tmp_path, monkeypatch, deny_access, index_problem
    ):
        store = DebugTraceStore(tmp_path, trace_limit=1)
        previous = _make_trace_data(TRACE_ID_1, "2025-01-01T00:00:00Z")
        store.save_trace(TRACE_ID_1, previous)
        index = store.get_data_dir() / "index.json"
        intact = index.read_bytes()
        damaged = {
            "invalid_json": b"[",
            "invalid_root": b"{}",
            "invalid_entry": json.dumps([{"trace_id": "invalid"}]).encode(),
            "invalid_timestamp": json.dumps([{"trace_id": TRACE_ID_1, "timestamp": None}]).encode(),
            "invalid_utf8": b"\xff",
        }.get(index_problem, intact)
        index.write_bytes(damaged)
        if index_problem == "unreadable":
            deny_access(index)

        assert store.get_traces() == []
        error_type = PermissionError if index_problem == "unreadable" else ValueError
        with pytest.raises(error_type):
            store.save_trace(TRACE_ID_2, _make_trace_data(TRACE_ID_2, "2025-01-02T00:00:00Z"))

        monkeypatch.undo()
        assert index.read_bytes() == damaged
        assert store.get_trace(TRACE_ID_1) == previous
        assert {path.name for path in (store.get_data_dir() / "traces").iterdir()} == {
            f"{TRACE_ID_1}.json"
        }
        # A readable, repaired index resumes ordinary publication and retention.
        index.write_bytes(intact)
        store.save_trace(TRACE_ID_2, _make_trace_data(TRACE_ID_2, "2025-01-02T00:00:00Z"))
        assert [entry["trace_id"] for entry in store.get_traces()] == [TRACE_ID_2]
        assert not (store.get_data_dir() / "traces" / f"{TRACE_ID_1}.json").exists()

    @pytest.mark.parametrize("cleanup_index", ["readable", "unreadable", "missing", "corrupt"])
    def test_failed_index_replace_preserves_previous_trace_and_cleans_only_with_evidence(
        self, tmp_path, monkeypatch, deny_access, cleanup_index
    ):
        store = DebugTraceStore(tmp_path, trace_limit=1)
        store.save_trace(TRACE_ID_1, _make_trace_data(TRACE_ID_1, "2025-01-01T00:00:00Z"))
        index = store.get_data_dir() / "index.json"
        original_index = index.read_bytes()
        replace = os.replace
        failure = OSError("index write failed")

        def fail_index(source, target):
            if Path(target) == index:
                if cleanup_index == "unreadable":
                    deny_access(index)
                elif cleanup_index == "missing":
                    index.unlink()
                elif cleanup_index == "corrupt":
                    index.write_text("[", encoding="utf-8")
                raise failure
            replace(source, target)

        monkeypatch.setattr("core.utils.atomic.os.replace", fail_index)
        with pytest.raises(OSError) as raised:
            store.save_trace(TRACE_ID_2, _make_trace_data(TRACE_ID_2, "2025-01-02T00:00:00Z"))
        assert raised.value is failure
        monkeypatch.undo()
        assert store.get_trace(TRACE_ID_1)["trace_id"] == TRACE_ID_1
        expected = {f"{TRACE_ID_1}.json"}
        if cleanup_index != "readable":
            expected.add(f"{TRACE_ID_2}.json")
        assert {path.name for path in (store.get_data_dir() / "traces").iterdir()} == expected

        index.write_bytes(original_index)
        assert [entry["trace_id"] for entry in store.get_traces()] == [TRACE_ID_1]
        store.save_trace(TRACE_ID_3, _make_trace_data(TRACE_ID_3, "2025-01-03T00:00:00Z"))
        assert {path.name for path in (store.get_data_dir() / "traces").iterdir()} == {
            f"{TRACE_ID_3}.json"
        }

    def test_next_save_removes_crash_orphans_and_partial_atomic_files(self, tmp_path):
        store = DebugTraceStore(tmp_path, trace_limit=1)
        traces = store.get_data_dir() / "traces"
        traces.mkdir(parents=True)
        (traces / f"{TRACE_ID_1}.json").write_text("raw orphan")
        (traces / f".{TRACE_ID_2}.json.{TRACE_ID_3}.tmp").write_text("partial wire body")
        store.save_trace(TRACE_ID_4, _make_trace_data(TRACE_ID_4, "2025-01-04T00:00:00Z"))
        assert [path.name for path in traces.iterdir()] == [f"{TRACE_ID_4}.json"]

    @pytest.mark.parametrize("unreadable_after_publication", [False, True])
    def test_failure_after_index_publication_keeps_the_published_capture(
        self, tmp_path, monkeypatch, deny_access, unreadable_after_publication
    ):
        store = DebugTraceStore(tmp_path, trace_limit=1)
        store.save_trace(TRACE_ID_1, _make_trace_data(TRACE_ID_1, "2025-01-01T00:00:00Z"))
        write_index = store._write_index
        failure = OSError("directory sync failed")

        def fail_after_publication(entries):
            write_index(entries)
            if unreadable_after_publication:
                deny_access(store.get_data_dir() / "index.json")
            raise failure

        monkeypatch.setattr(store, "_write_index", fail_after_publication)
        with pytest.raises(OSError) as raised:
            store.save_trace(TRACE_ID_2, _make_trace_data(TRACE_ID_2, "2025-01-02T00:00:00Z"))
        assert raised.value is failure
        monkeypatch.undo()
        assert [entry["trace_id"] for entry in store.get_traces()] == [TRACE_ID_2]
        assert store.get_trace(TRACE_ID_2)["trace_id"] == TRACE_ID_2
        assert (store.get_data_dir() / "traces" / f"{TRACE_ID_1}.json").exists() == (
            unreadable_after_publication
        )

    def test_concurrent_adapter_stores_retain_one_consistent_bounded_index(self, tmp_path):
        def save(_):
            trace_id = uuid4().hex
            DebugTraceStore(tmp_path, 5).save_trace(
                trace_id, _make_trace_data(trace_id, "2025-01-01T00:00:00Z")
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(save, range(20)))
        store = DebugTraceStore(tmp_path, 5)
        retained = {entry["trace_id"] for entry in store.get_traces()}
        assert len(retained) == 5
        assert {path.stem for path in (store.get_data_dir() / "traces").iterdir()} == retained

    def test_persists_the_full_trace_and_lists_only_its_metadata(self, tmp_path: Path) -> None:
        store = DebugTraceStore(tmp_path, trace_limit=10)
        trace_data = _make_trace_data(TRACE_ID_1, "2025-06-01T12:00:00Z")

        store.save_trace(TRACE_ID_1, trace_data)

        assert store.get_data_dir() == DataDirectoryLayout(tmp_path).debug
        assert (store.get_data_dir() / "traces" / f"{TRACE_ID_1}.json").is_file()
        assert store.get_trace(TRACE_ID_1) == trace_data
        # Index entries never embed request or response bodies.
        assert store.get_traces() == [
            {
                "trace_id": TRACE_ID_1,
                "type": "provider_request",
                "timestamp": "2025-06-01T12:00:00Z",
                "provider_id": "openai",
                "model_id": "gpt-4",
                "method": "POST",
                "url": "https://api.example.com/v1/chat",
                "status_code": 200,
                "duration_ms": 150,
            }
        ]


# ---------------------------------------------------------------------------
# Get trace list (newest first)
# ---------------------------------------------------------------------------


class TestGetTraces:
    def test_returns_newest_first(self, tmp_path: Path) -> None:
        """get_traces() returns entries sorted by timestamp descending."""
        store = DebugTraceStore(tmp_path, trace_limit=10)
        store.save_trace(TRACE_ID_1, _make_trace_data(TRACE_ID_1, "2025-06-01T10:00:00Z"))
        store.save_trace(TRACE_ID_2, _make_trace_data(TRACE_ID_2, "2025-06-01T12:00:00Z"))
        store.save_trace(TRACE_ID_3, _make_trace_data(TRACE_ID_3, "2025-06-01T11:00:00Z"))

        traces = store.get_traces()

        assert len(traces) == 3
        assert [t["trace_id"] for t in traces] == [TRACE_ID_2, TRACE_ID_3, TRACE_ID_1]


# ---------------------------------------------------------------------------
# Get full trace by ID
# ---------------------------------------------------------------------------


class TestGetTrace:
    def test_raises_file_not_found_for_unknown_id(self, tmp_path: Path) -> None:
        """Requesting a non-existent trace raises FileNotFoundError."""
        store = DebugTraceStore(tmp_path, trace_limit=10)

        with pytest.raises(FileNotFoundError, match=f"Debug trace not found: {TRACE_ID_1}"):
            store.get_trace(TRACE_ID_1)

    @pytest.mark.parametrize(
        "trace_id",
        [
            "",
            "not-a-uuid",
            "00000000-0000-4000-8000-000000000001",
            "0000000000004000800000000000000A",
            "00000000000010008000000000000001",
            "../../../channels/telegram/channel",
        ],
    )
    def test_rejects_noncanonical_trace_ids(self, tmp_path: Path, trace_id: str) -> None:
        """Only canonical lowercase UUID4 hex ids may become trace paths."""
        store = DebugTraceStore(tmp_path, trace_limit=10)

        with pytest.raises(InvalidTraceIdError):
            store.get_trace(trace_id)


# ---------------------------------------------------------------------------
# Retention pruning
# ---------------------------------------------------------------------------


class TestRetentionPruning:
    def test_keeps_traces_up_to_the_limit_and_then_deletes_the_oldest(self, tmp_path: Path) -> None:
        store = DebugTraceStore(tmp_path, trace_limit=3)
        for day, trace_id in enumerate((TRACE_ID_1, TRACE_ID_2, TRACE_ID_3), start=1):
            store.save_trace(trace_id, _make_trace_data(trace_id, f"2025-01-0{day}T00:00:00Z"))
        assert len(store.get_traces()) == 3

        store.save_trace(TRACE_ID_4, _make_trace_data(TRACE_ID_4, "2025-01-04T00:00:00Z"))

        assert {t["trace_id"] for t in store.get_traces()} == {TRACE_ID_2, TRACE_ID_3, TRACE_ID_4}
        assert not (store.get_data_dir() / "traces" / f"{TRACE_ID_1}.json").exists()


# ---------------------------------------------------------------------------
# Clear all
# ---------------------------------------------------------------------------


class TestClearAll:
    def test_clear_all_removes_every_trace_artifact_only(self, tmp_path):
        store = DebugTraceStore(tmp_path, trace_limit=10)
        store.save_trace(TRACE_ID_1, _make_trace_data(TRACE_ID_1, "2025-01-01T00:00:00Z"))
        store.save_trace(TRACE_ID_2, _make_trace_data(TRACE_ID_2, "2025-01-02T00:00:00Z"))
        nested = store.get_data_dir() / "traces" / "nested"
        nested.mkdir()
        (nested / "leftover.json").write_text("sensitive trace")
        other = store.get_data_dir().parent / "keep.txt"
        other.write_text("unrelated artifact")

        store.clear_all()

        assert not (store.get_data_dir() / "traces").exists()
        assert not (store.get_data_dir() / "index.json").exists()
        assert store.get_traces() == []
        assert other.read_text() == "unrelated artifact"
        # Clearing again, with nothing left, is safe.
        store.clear_all()


# ---------------------------------------------------------------------------
# Trace thread: Event Loop safety, ordering, backlog, drain
# ---------------------------------------------------------------------------


class TestTraceThread:
    @pytest.mark.asyncio
    async def test_async_save_keeps_the_event_loop_responsive(
        self, tmp_path: Path, blocked_trace_writes: _BlockedTraceWrites
    ) -> None:
        store = DebugTraceStore(tmp_path, trace_limit=10)
        saving = asyncio.create_task(
            store.save_trace_async(TRACE_ID_1, _make_trace_data(TRACE_ID_1, "2025-01-01T00:00:00Z"))
        )
        await blocked_trace_writes.wait_until_blocked()

        loop = asyncio.get_running_loop()
        ticked_at = loop.time()
        for _ in range(5):
            await asyncio.sleep(0.01)
        assert loop.time() - ticked_at < 1
        assert not saving.done()

        blocked_trace_writes.release.set()
        await saving
        assert [entry["trace_id"] for entry in await store.get_traces_async()] == [TRACE_ID_1]

    @pytest.mark.asyncio
    async def test_hand_off_returns_before_the_write_and_reads_follow_it(
        self, tmp_path: Path, blocked_trace_writes: _BlockedTraceWrites
    ) -> None:
        store = DebugTraceStore(tmp_path, trace_limit=10)
        trace = _make_trace_data(TRACE_ID_1, "2025-01-01T00:00:00Z")

        assert store.save_trace_in_background(TRACE_ID_1, lambda: trace) is True
        await blocked_trace_writes.wait_until_blocked()
        listing = asyncio.create_task(store.get_traces_async())
        await asyncio.sleep(0.05)
        assert not listing.done()

        blocked_trace_writes.release.set()
        assert [entry["trace_id"] for entry in await listing] == [TRACE_ID_1]

    @pytest.mark.asyncio
    async def test_clear_runs_after_earlier_hand_offs(
        self, tmp_path: Path, blocked_trace_writes: _BlockedTraceWrites
    ) -> None:
        store = DebugTraceStore(tmp_path, trace_limit=10)
        trace = _make_trace_data(TRACE_ID_1, "2025-01-01T00:00:00Z")
        store.save_trace_in_background(TRACE_ID_1, lambda: trace)
        await blocked_trace_writes.wait_until_blocked()
        clearing = asyncio.create_task(store.clear_all_async())

        blocked_trace_writes.release.set()
        await clearing

        assert await store.get_traces_async() == []
        assert not (store.get_data_dir() / "traces").exists()

    @pytest.mark.asyncio
    async def test_hand_off_backlog_is_bounded(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        blocked_trace_writes: _BlockedTraceWrites,
    ) -> None:
        monkeypatch.setattr(store_module, "MAX_PENDING_CAPTURES", 2)
        store = DebugTraceStore(tmp_path, trace_limit=10)
        traces = {
            trace_id: _make_trace_data(trace_id, f"2025-01-0{index}T00:00:00Z")
            for index, trace_id in enumerate((TRACE_ID_1, TRACE_ID_2, TRACE_ID_3), start=1)
        }

        assert store.save_trace_in_background(TRACE_ID_1, lambda: traces[TRACE_ID_1])
        await blocked_trace_writes.wait_until_blocked()
        assert store.save_trace_in_background(TRACE_ID_2, lambda: traces[TRACE_ID_2])
        assert not store.save_trace_in_background(TRACE_ID_3, lambda: traces[TRACE_ID_3])

        blocked_trace_writes.release.set()
        await drain_debug_traces()
        assert {entry["trace_id"] for entry in await store.get_traces_async()} == {
            TRACE_ID_1,
            TRACE_ID_2,
        }
        # Settled captures free their backlog slots.
        assert store.save_trace_in_background(TRACE_ID_3, lambda: traces[TRACE_ID_3])
        await drain_debug_traces()

    @pytest.mark.asyncio
    async def test_drain_waits_for_handed_off_captures(
        self, tmp_path: Path, blocked_trace_writes: _BlockedTraceWrites
    ) -> None:
        store = DebugTraceStore(tmp_path, trace_limit=10)
        trace = _make_trace_data(TRACE_ID_1, "2025-01-01T00:00:00Z")
        store.save_trace_in_background(TRACE_ID_1, lambda: trace)
        await blocked_trace_writes.wait_until_blocked()
        draining = asyncio.create_task(drain_debug_traces())
        await asyncio.sleep(0.05)
        assert not draining.done()

        blocked_trace_writes.release.set()
        await draining

        assert (store.get_data_dir() / "traces" / f"{TRACE_ID_1}.json").is_file()

    @pytest.mark.asyncio
    async def test_background_failure_is_logged_not_raised(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = DebugTraceStore(tmp_path, trace_limit=10)

        def broken_build() -> dict:
            raise RuntimeError("capture broke")

        caplog.set_level("WARNING", logger="vbot.debug")
        assert store.save_trace_in_background(TRACE_ID_1, broken_build)
        await drain_debug_traces()

        assert await store.get_traces_async() == []
        assert any(record.exc_info for record in caplog.records if record.name == "vbot.debug")
