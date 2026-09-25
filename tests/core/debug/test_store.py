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
    def test_failed_index_replace_preserves_previous_trace_and_removes_new_file(
        self, tmp_path, monkeypatch
    ):
        store = DebugTraceStore(tmp_path, trace_limit=1)
        store.save_trace(TRACE_ID_1, _make_trace_data(TRACE_ID_1, "2025-01-01T00:00:00Z"))
        replace = os.replace

        def fail_index(source, target):
            if Path(target).name == "index.json":
                raise OSError("index write failed")
            replace(source, target)

        monkeypatch.setattr("core.utils.atomic.os.replace", fail_index)
        for trace_id in [TRACE_ID_2, TRACE_ID_3]:
            with pytest.raises(OSError, match="index write failed"):
                store.save_trace(trace_id, _make_trace_data(trace_id, "2025-01-02T00:00:00Z"))
        assert [entry["trace_id"] for entry in store.get_traces()] == [TRACE_ID_1]
        assert store.get_trace(TRACE_ID_1)["trace_id"] == TRACE_ID_1
        assert {path.name for path in (store.get_data_dir() / "traces").iterdir()} == {
            f"{TRACE_ID_1}.json"
        }

    def test_next_save_removes_crash_orphans_and_partial_atomic_files(self, tmp_path):
        store = DebugTraceStore(tmp_path, trace_limit=1)
        traces = store.get_data_dir() / "traces"
        traces.mkdir(parents=True)
        (traces / f"{TRACE_ID_1}.json").write_text("raw orphan")
        (traces / f".{TRACE_ID_2}.json.{TRACE_ID_3}.tmp").write_text("partial wire body")
        store.save_trace(TRACE_ID_4, _make_trace_data(TRACE_ID_4, "2025-01-04T00:00:00Z"))
        assert [path.name for path in traces.iterdir()] == [f"{TRACE_ID_4}.json"]

    def test_failure_after_index_publication_keeps_the_published_capture(
        self, tmp_path, monkeypatch
    ):
        store = DebugTraceStore(tmp_path, trace_limit=1)
        store.save_trace(TRACE_ID_1, _make_trace_data(TRACE_ID_1, "2025-01-01T00:00:00Z"))
        write_index = store._write_index

        def fail_after_publication(entries):
            write_index(entries)
            raise OSError("directory sync failed")

        monkeypatch.setattr(store, "_write_index", fail_after_publication)
        with pytest.raises(OSError, match="directory sync failed"):
            store.save_trace(TRACE_ID_2, _make_trace_data(TRACE_ID_2, "2025-01-02T00:00:00Z"))
        assert [entry["trace_id"] for entry in store.get_traces()] == [TRACE_ID_2]
        assert store.get_trace(TRACE_ID_2)["trace_id"] == TRACE_ID_2
        assert not (store.get_data_dir() / "traces" / f"{TRACE_ID_1}.json").exists()

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

    def test_persists_trace_file_and_index_entry(self, tmp_path: Path) -> None:
        """Saving a trace writes the file and a metadata-only index entry."""
        store = DebugTraceStore(tmp_path, trace_limit=10)
        trace_id = TRACE_ID_1
        trace_data = _make_trace_data(trace_id, "2025-06-01T12:00:00Z")

        store.save_trace(trace_id, trace_data)

        # Trace file exists with full content
        trace_path = DataDirectoryLayout(tmp_path).debug / "traces" / f"{trace_id}.json"
        assert trace_path.is_file()
        saved = json.loads(trace_path.read_text(encoding="utf-8"))
        assert saved["trace_id"] == trace_id
        assert saved["request"]["body"]["model"] == "gpt-4"

        # Index contains metadata entry
        index_path = DataDirectoryLayout(tmp_path).debug / "index.json"
        assert index_path.is_file()
        index = json.loads(index_path.read_text(encoding="utf-8"))
        assert isinstance(index, list)
        assert len(index) == 1
        entry = index[0]
        assert entry["trace_id"] == trace_id
        assert entry["type"] == "provider_request"
        assert entry["timestamp"] == "2025-06-01T12:00:00Z"
        assert entry["provider_id"] == "openai"
        assert entry["model_id"] == "gpt-4"
        assert entry["method"] == "POST"
        assert entry["url"] == "https://api.example.com/v1/chat"
        assert entry["status_code"] == 200
        assert entry["duration_ms"] == 150

    def test_index_contains_only_metadata_fields(self, tmp_path: Path) -> None:
        """Index entries must never embed full request/response bodies."""
        store = DebugTraceStore(tmp_path, trace_limit=10)
        trace_data = _make_trace_data(TRACE_ID_1, "2025-06-01T12:00:00Z")

        store.save_trace(TRACE_ID_1, trace_data)

        index_path = DataDirectoryLayout(tmp_path).debug / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        entry = index[0]

        assert "request" not in entry
        assert "response" not in entry
        assert "run_id" not in entry
        assert set(entry.keys()) == {
            "trace_id",
            "type",
            "timestamp",
            "provider_id",
            "model_id",
            "method",
            "url",
            "status_code",
            "duration_ms",
        }


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

    def test_returns_empty_list_when_no_traces(self, tmp_path: Path) -> None:
        """An empty store returns an empty list, not an error."""
        store = DebugTraceStore(tmp_path, trace_limit=10)
        assert store.get_traces() == []

    def test_entries_are_metadata_only(self, tmp_path: Path) -> None:
        """get_traces() returns metadata-only entries, not full trace bodies."""
        store = DebugTraceStore(tmp_path, trace_limit=10)
        store.save_trace(TRACE_ID_1, _make_trace_data(TRACE_ID_1, "2025-06-01T10:00:00Z"))

        traces = store.get_traces()
        entry = traces[0]

        assert "request" not in entry
        assert "response" not in entry
        assert entry["trace_id"] == TRACE_ID_1


# ---------------------------------------------------------------------------
# Get full trace by ID
# ---------------------------------------------------------------------------


class TestGetTrace:
    def test_returns_full_trace_by_id(self, tmp_path: Path) -> None:
        """get_trace() returns the complete trace payload including
        request and response bodies."""
        store = DebugTraceStore(tmp_path, trace_limit=10)
        trace_data = _make_trace_data(TRACE_ID_1, "2025-06-01T12:00:00Z")
        store.save_trace(TRACE_ID_1, trace_data)

        result = store.get_trace(TRACE_ID_1)

        assert isinstance(result, dict)
        assert result["trace_id"] == TRACE_ID_1
        assert result["request"]["body"]["model"] == "gpt-4"
        assert result["response"]["body"]["choices"][0]["message"]["content"] == "hi"

    def test_raises_file_not_found_for_unknown_id(self, tmp_path: Path) -> None:
        """Requesting a non-existent trace raises FileNotFoundError."""
        store = DebugTraceStore(tmp_path, trace_limit=10)

        with pytest.raises(FileNotFoundError, match=f"Debug trace not found: {TRACE_ID_1}"):
            store.get_trace(TRACE_ID_1)

    def test_rejects_path_traversal_before_reading_json(self, tmp_path: Path) -> None:
        """A crafted trace id cannot escape the trace directory."""
        store = DebugTraceStore(tmp_path, trace_limit=10)
        store.save_trace(TRACE_ID_1, _make_trace_data(TRACE_ID_1, "2025-06-01T12:00:00Z"))
        channel_path = tmp_path / "channels" / "telegram" / "channel.json"
        channel_path.parent.mkdir(parents=True)
        channel_path.write_text('{"bot_token": "secret"}', encoding="utf-8")

        with pytest.raises(InvalidTraceIdError):
            store.get_trace("../../../channels/telegram/channel")

    @pytest.mark.parametrize(
        "trace_id",
        [
            "",
            "not-a-uuid",
            "00000000-0000-4000-8000-000000000001",
            "0000000000004000800000000000000A",
            "00000000000010008000000000000001",
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
    def test_deletes_oldest_when_exceeding_limit(self, tmp_path: Path) -> None:
        """Saving N+1 traces removes the oldest trace file and index entry."""
        store = DebugTraceStore(tmp_path, trace_limit=3)
        store.save_trace(TRACE_ID_1, _make_trace_data(TRACE_ID_1, "2025-01-01T00:00:00Z"))
        store.save_trace(TRACE_ID_2, _make_trace_data(TRACE_ID_2, "2025-01-02T00:00:00Z"))
        store.save_trace(TRACE_ID_3, _make_trace_data(TRACE_ID_3, "2025-01-03T00:00:00Z"))

        # Save one more — oldest (id-1) should be pruned
        store.save_trace(TRACE_ID_4, _make_trace_data(TRACE_ID_4, "2025-01-04T00:00:00Z"))

        traces = store.get_traces()
        trace_ids = {t["trace_id"] for t in traces}
        assert trace_ids == {TRACE_ID_2, TRACE_ID_3, TRACE_ID_4}
        assert len(traces) == 3

        # Oldest trace file should be deleted from disk
        old_trace_path = DataDirectoryLayout(tmp_path).debug / "traces" / f"{TRACE_ID_1}.json"
        assert not old_trace_path.exists()

    def test_does_not_prune_when_at_limit(self, tmp_path: Path) -> None:
        """When trace count equals the limit, no pruning occurs."""
        store = DebugTraceStore(tmp_path, trace_limit=3)
        store.save_trace(TRACE_ID_1, _make_trace_data(TRACE_ID_1, "2025-01-01T00:00:00Z"))
        store.save_trace(TRACE_ID_2, _make_trace_data(TRACE_ID_2, "2025-01-02T00:00:00Z"))
        store.save_trace(TRACE_ID_3, _make_trace_data(TRACE_ID_3, "2025-01-03T00:00:00Z"))

        traces = store.get_traces()
        assert len(traces) == 3


# ---------------------------------------------------------------------------
# Clear all
# ---------------------------------------------------------------------------


class TestClearAll:
    def test_clear_all_removes_nested_trace_artifacts_only(self, tmp_path):
        store = DebugTraceStore(tmp_path, trace_limit=10)
        store.save_trace(TRACE_ID_1, _make_trace_data(TRACE_ID_1, "2025-01-01T00:00:00Z"))
        nested = store.get_data_dir() / "traces" / "nested"
        nested.mkdir()
        (nested / "leftover.json").write_text("sensitive trace")
        other = store.get_data_dir().parent / "keep.txt"
        other.write_text("unrelated artifact")

        store.clear_all()

        assert not (store.get_data_dir() / "traces").exists()
        assert not (store.get_data_dir() / "index.json").exists()
        assert other.read_text() == "unrelated artifact"

    def test_removes_all_traces_and_index(self, tmp_path: Path) -> None:
        """clear_all() deletes every trace file and the index."""
        store = DebugTraceStore(tmp_path, trace_limit=10)
        store.save_trace(TRACE_ID_1, _make_trace_data(TRACE_ID_1, "2025-01-01T00:00:00Z"))
        store.save_trace(TRACE_ID_2, _make_trace_data(TRACE_ID_2, "2025-01-02T00:00:00Z"))

        store.clear_all()

        traces_dir = DataDirectoryLayout(tmp_path).debug / "traces"
        index_path = DataDirectoryLayout(tmp_path).debug / "index.json"
        assert not index_path.exists()
        # traces_dir is removed by clear_all's rmdir call
        assert not traces_dir.exists()
        assert store.get_traces() == []

    def test_clear_all_is_safe_when_nothing_exists(self, tmp_path: Path) -> None:
        """Calling clear_all() on a store with no traces does not raise."""
        store = DebugTraceStore(tmp_path, trace_limit=10)
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


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


class TestGetDataDir:
    def test_returns_debug_directory(self, tmp_path: Path) -> None:
        store = DebugTraceStore(tmp_path, trace_limit=10)
        assert store.get_data_dir() == DataDirectoryLayout(tmp_path).debug
