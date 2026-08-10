"""Tests for DebugTraceStore write, read, list, prune, and clear behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.debug.store import DebugTraceStore, InvalidTraceIdError
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


# ---------------------------------------------------------------------------
# Save trace & index metadata
# ---------------------------------------------------------------------------


class TestSaveTrace:
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
# Misc
# ---------------------------------------------------------------------------


class TestGetDataDir:
    def test_returns_debug_directory(self, tmp_path: Path) -> None:
        store = DebugTraceStore(tmp_path, trace_limit=10)
        assert store.get_data_dir() == DataDirectoryLayout(tmp_path).debug
