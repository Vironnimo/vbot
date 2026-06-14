"""Debug trace storage for vBot.

Provides ``DebugTraceStore``, which persists provider wire traces as
individual JSON files under ``<data_dir>/debug/traces/`` with a
metadata-only ``index.json`` for fast listing and retention pruning.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.utils.logging import get_logger

_logger = get_logger("debug")

_TRACES_DIR_NAME = "traces"
_INDEX_FILE_NAME = "index.json"


@dataclass
class _TraceIndexEntry:
    """Metadata-only trace entry stored in index.json."""

    trace_id: str
    type: str
    timestamp: str
    provider_id: str
    model_id: str
    method: str
    url: str
    status_code: int | None
    duration_ms: int | None


class DebugTraceStore:
    """Persists and retrieves provider wire traces on the local filesystem.

    Traces are stored as individual JSON files under
    ``<data_dir>/debug/traces/<trace_id>.json``.  A metadata-only
    ``index.json`` in ``<data_dir>/debug/`` enables fast listing without
    reading every trace file.  Oldest traces are pruned automatically
    after each write so the total count never exceeds the configured
    limit.

    Args:
        data_dir: Absolute path to the vBot data directory
                  (e.g. ``~/.vbot``).  Traces live under
                  ``<data_dir>/debug/``.
        trace_limit: Maximum number of traces to retain.  Traces
                     exceeding this limit are deleted (oldest first)
                     after every ``save_trace()`` call.
    """

    def __init__(self, data_dir: str | Path, trace_limit: int) -> None:
        self._debug_dir = Path(data_dir) / "debug"
        self._traces_dir = self._debug_dir / _TRACES_DIR_NAME
        self._index_path = self._debug_dir / _INDEX_FILE_NAME
        self._trace_limit = trace_limit

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_data_dir(self) -> Path:
        """Return the debug data directory path (``<data_dir>/debug/``)."""
        return self._debug_dir

    def save_trace(self, trace_id: str, trace_data: dict[str, Any]) -> None:
        """Persist a full trace and update the metadata index.

        Writes the complete *trace_data* payload to
        ``<data_dir>/debug/traces/<trace_id>.json``, inserts a
        metadata-only entry into ``index.json``, and prunes the oldest
        traces when the total count exceeds the configured limit.

        Args:
            trace_id: Unique identifier for this trace.
            trace_data: Full trace payload following the canonical shape in
                ``.vorch/domain-maps/debug.md``. Metadata is extracted from the
                nested ``request`` / ``response`` objects for the index entry.
        """
        self._ensure_directories()

        trace_path = self._traces_dir / f"{trace_id}.json"
        with open(trace_path, "w", encoding="utf-8") as file:
            json.dump(trace_data, file, ensure_ascii=False, indent=2)

        request = trace_data.get("request") or {}
        response = trace_data.get("response") or {}
        entry = _TraceIndexEntry(
            trace_id=trace_id,
            type=trace_data.get("type", ""),
            timestamp=trace_data.get("timestamp", ""),
            provider_id=trace_data.get("provider_id", ""),
            model_id=trace_data.get("model_id", ""),
            method=request.get("method", ""),
            url=request.get("url", ""),
            status_code=response.get("status_code"),
            duration_ms=trace_data.get("duration_ms"),
        )

        entries = self._read_index()
        entries.append(asdict(entry))
        entries = self._prune_oldest(entries)
        self._write_index(entries)

    def get_traces(self) -> list[dict[str, Any]]:
        """Return trace metadata from the index, newest first.

        Each entry contains only metadata fields: ``trace_id``, ``type``,
        ``timestamp``, ``provider_id``, ``model_id``, ``method``, ``url``,
        ``status_code``, and ``duration_ms``.

        Returns:
            A list of index entries sorted by ``timestamp`` descending.
            Returns an empty list when ``index.json`` does not exist or
            cannot be parsed.
        """
        entries = self._read_index()
        entries.sort(key=lambda entry: entry.get("timestamp", ""), reverse=True)
        return entries

    def get_trace(self, trace_id: str) -> dict[str, Any]:
        """Return the full trace JSON for *trace_id*.

        Args:
            trace_id: Unique identifier for the trace to retrieve.

        Returns:
            The complete trace data dictionary.

        Raises:
            FileNotFoundError: No trace file exists for *trace_id*.
        """
        trace_path = self._traces_dir / f"{trace_id}.json"
        if not trace_path.is_file():
            raise FileNotFoundError(f"Debug trace not found: {trace_id}")
        with open(trace_path, encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, dict):
            raise ValueError(f"Debug trace {trace_id} is not a JSON object")
        return data

    def clear_all(self) -> None:
        """Delete all trace files and the metadata index."""
        if self._traces_dir.exists():
            for trace_file in self._traces_dir.iterdir():
                trace_file.unlink(missing_ok=True)
            self._traces_dir.rmdir()
        if self._index_path.exists():
            self._index_path.unlink()
        _logger.info("Cleared all debug traces and index")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_directories(self) -> None:
        """Create the debug and traces directories if they do not exist."""
        self._traces_dir.mkdir(parents=True, exist_ok=True)

    def _read_index(self) -> list[dict[str, Any]]:
        """Read and return the index entries, or an empty list on failure."""
        if not self._index_path.is_file():
            return []
        try:
            with open(self._index_path, encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError):
            _logger.warning("Debug trace index is unreadable; treating as empty")
            return []
        if not isinstance(data, list):
            _logger.warning("Debug trace index has unexpected format; treating as empty")
            return []
        return data

    def _write_index(self, entries: list[dict[str, Any]]) -> None:
        """Write the index entries to disk."""
        self._ensure_directories()
        with open(self._index_path, "w", encoding="utf-8") as file:
            json.dump(entries, file, ensure_ascii=False, indent=2)

    def _prune_oldest(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Remove oldest entries until count is within the configured limit.

        Entries are sorted by ``timestamp`` ascending; the oldest are
        removed from the list and their trace files deleted.  Returns the
        trimmed list.
        """
        if self._trace_limit <= 0:
            return entries
        if len(entries) <= self._trace_limit:
            return entries

        entries.sort(key=lambda entry: entry.get("timestamp", ""))
        while len(entries) > self._trace_limit:
            removed = entries.pop(0)
            removed_id = removed.get("trace_id", "")
            if removed_id:
                self._delete_trace_file(removed_id)
                _logger.info("Pruned oldest debug trace: %s", removed_id)

        return entries

    def _delete_trace_file(self, trace_id: str) -> None:
        """Delete a single trace file, silently ignoring a missing file."""
        trace_path = self._traces_dir / f"{trace_id}.json"
        trace_path.unlink(missing_ok=True)
