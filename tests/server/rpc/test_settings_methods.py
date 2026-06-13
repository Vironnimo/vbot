"""Tests for settings RPC helpers.

Focus: ``_trace_count`` must not mask unexpected debug-trace-store failures.
Expected store-absence errors (``FileNotFoundError``/``OSError``) return 0
silently; anything unexpected logs at WARNING with a traceback before
returning 0.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from server.rpc import settings_methods
from server.rpc.settings_methods import _trace_count


class _RaisingStorage:
    """Storage stub whose ``load_debug_settings`` raises a chosen error."""

    data_dir = "."

    def __init__(self, error: BaseException) -> None:
        self._error = error

    def load_debug_settings(self) -> dict[str, Any]:
        raise self._error


def _runtime_with_storage(storage: Any) -> Any:
    return SimpleNamespace(storage=storage)


def test_trace_count_returns_zero_silently_on_missing_store(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = _runtime_with_storage(_RaisingStorage(FileNotFoundError("no traces yet")))

    caplog.set_level(logging.WARNING, logger="vbot.server.rpc.settings")
    assert _trace_count(runtime) == 0

    assert [
        record for record in caplog.records if record.name == "vbot.server.rpc.settings"
    ] == []


def test_trace_count_logs_warning_on_unexpected_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = _runtime_with_storage(_RaisingStorage(RuntimeError("store corrupt")))

    caplog.set_level(logging.WARNING, logger="vbot.server.rpc.settings")
    assert _trace_count(runtime) == 0

    warning_records = [
        record
        for record in caplog.records
        if record.name == "vbot.server.rpc.settings"
        and "debug trace count" in record.getMessage()
    ]
    assert len(warning_records) == 1
    assert warning_records[0].exc_info is not None


def test_trace_count_logger_name() -> None:
    assert settings_methods._LOGGER.name == "vbot.server.rpc.settings"
