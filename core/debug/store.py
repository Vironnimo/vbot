"""Debug trace storage for vBot.

Provides ``DebugTraceStore``, which persists provider wire traces as
individual JSON files under ``<data_dir>/artifacts/debug/traces/`` with a
metadata-only ``index.json`` for fast listing and retention pruning.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import RLock
from typing import Any

from core.storage.layout import DataDirectoryLayout
from core.utils.atomic import atomic_write_text
from core.utils.logging import get_logger

_logger = get_logger("debug")

_TRACES_DIR_NAME = "traces"
_INDEX_FILE_NAME = "index.json"
_TRACE_ID_PATTERN = re.compile(r"^[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$")
# Adapters have separate stores pointing at the same files. Hold the lock across
# publication and cleanup so another capture cannot mistake an in-flight file
# for an orphan. This also serializes operator clears with capture finalization.
_STORE_LOCK = RLock()


class InvalidTraceIdError(ValueError):
    """Raised when a trace id is not a canonical UUID4 hex value."""


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
    ``<data_dir>/artifacts/debug/traces/<trace_id>.json``. A metadata-only
    ``index.json`` in ``<data_dir>/artifacts/debug/`` enables fast listing without
    reading every trace file.  Oldest traces are pruned automatically
    after each write so the total count never exceeds the configured
    limit.

    Args:
        data_dir: Absolute path to the vBot data directory
                  (e.g. ``~/.vbot``).  Traces live under
                  ``<data_dir>/artifacts/debug/``.
        trace_limit: Maximum number of traces to retain.  Traces
                     exceeding this limit are deleted (oldest first)
                     after every ``save_trace()`` call.
    """

    def __init__(self, data_dir: str | Path, trace_limit: int) -> None:
        self._debug_dir = DataDirectoryLayout(data_dir).debug
        self._traces_dir = self._debug_dir / _TRACES_DIR_NAME
        self._index_path = self._debug_dir / _INDEX_FILE_NAME
        self._trace_limit = trace_limit

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_data_dir(self) -> Path:
        """Return the debug data directory path under ``artifacts/debug/``."""
        return self._debug_dir

    def save_trace(self, trace_id: str, trace_data: dict[str, Any]) -> None:
        """Persist a full trace and update the metadata index.

        Writes the complete *trace_data* payload to
        ``<data_dir>/artifacts/debug/traces/<trace_id>.json``, inserts a
        metadata-only entry into ``index.json``, and prunes the oldest
        traces when the total count exceeds the configured limit.

        Args:
            trace_id: Unique identifier for this trace.
            trace_data: Full trace payload following the canonical shape in
                ``.vorch/domain-maps/debug.md``. Metadata is extracted from the
                nested ``request`` / ``response`` objects for the index entry.
        """
        trace_path = self._trace_path(trace_id)
        trace_json = json.dumps(trace_data, ensure_ascii=False, indent=2)
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

        with _STORE_LOCK:
            self._ensure_directories()
            previous = self._read_index()
            self._remove_unindexed_files(previous)
            entries = [item for item in previous if item.get("trace_id") != trace_id]
            entries.append(asdict(entry))
            entries = self._prune_oldest(entries)
            try:
                atomic_write_text(trace_path, trace_json)
                self._write_index(entries)
            except OSError:
                # Re-read publication state: a directory fsync can fail after
                # replacement already committed the new index.
                try:
                    self._remove_unindexed_files(self._read_index())
                except OSError:
                    _logger.warning("Failed to remove unindexed debug traces", exc_info=True)
                raise
            self._remove_unindexed_files(entries)

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
            InvalidTraceIdError: *trace_id* is not a canonical UUID4 hex value.
            FileNotFoundError: No trace file exists for *trace_id*.
        """
        trace_path = self._trace_path(trace_id)
        if not trace_path.is_file():
            raise FileNotFoundError(f"Debug trace not found: {trace_id}")
        with open(trace_path, encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, dict):
            raise ValueError(f"Debug trace {trace_id} is not a JSON object")
        return data

    def clear_all(self) -> None:
        """Delete all trace files and the metadata index."""
        with _STORE_LOCK:
            if self._traces_dir.is_symlink():
                self._traces_dir.unlink()
            elif hasattr(self._traces_dir, "is_junction") and self._traces_dir.is_junction():
                self._traces_dir.rmdir()
            elif self._traces_dir.exists():
                if self._traces_dir.resolve().parent != self._debug_dir.resolve():
                    raise ValueError("Trace directory must stay inside the debug directory")
                shutil.rmtree(self._traces_dir)
            self._index_path.unlink(missing_ok=True)
        _logger.info("Cleared all debug traces and index")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_directories(self) -> None:
        """Create the debug and traces directories if they do not exist."""
        self._traces_dir.mkdir(parents=True, exist_ok=True)

    def _trace_path(self, trace_id: str) -> Path:
        """Return the trace path after validating its canonical UUID4 hex id."""
        if not isinstance(trace_id, str) or _TRACE_ID_PATTERN.fullmatch(trace_id) is None:
            raise InvalidTraceIdError("Invalid debug trace id")
        return self._traces_dir / f"{trace_id}.json"

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
        return [
            entry
            for entry in data
            if isinstance(entry, dict)
            and isinstance(entry.get("trace_id"), str)
            and _TRACE_ID_PATTERN.fullmatch(entry["trace_id"]) is not None
        ]

    def _write_index(self, entries: list[dict[str, Any]]) -> None:
        """Write the index entries to disk."""
        self._ensure_directories()
        atomic_write_text(self._index_path, json.dumps(entries, ensure_ascii=False, indent=2))

    def _prune_oldest(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Remove oldest entries until count is within the configured limit.

        Files are removed only after the replacement index is published.
        """
        if self._trace_limit <= 0:
            return entries
        if len(entries) <= self._trace_limit:
            return entries

        entries.sort(key=lambda entry: entry.get("timestamp", ""))
        return entries[-self._trace_limit :]

    def _remove_unindexed_files(self, entries: list[dict[str, Any]]) -> None:
        """Remove orphan/pruned captures and interrupted atomic-write remnants."""
        retained = {f"{entry['trace_id']}.json" for entry in entries}
        for path in self._traces_dir.iterdir():
            is_trace = path.suffix == ".json" and _TRACE_ID_PATTERN.fullmatch(path.stem)
            parts = path.name.split(".")
            is_temporary = (
                len(parts) == 5
                and parts[0] == ""
                and parts[2] == "json"
                and parts[4] == "tmp"
                and _TRACE_ID_PATTERN.fullmatch(parts[1])
                and _TRACE_ID_PATTERN.fullmatch(parts[3])
            )
            if (is_trace and path.name not in retained) or is_temporary:
                path.unlink(missing_ok=True)
