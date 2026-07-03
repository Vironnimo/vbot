"""Tests for settings RPC helpers.

Focus: ``_trace_count`` must not mask unexpected debug-trace-store failures.
Expected store-absence errors (``FileNotFoundError``/``OSError``) return 0
silently; anything unexpected logs at WARNING with a traceback before
returning 0.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.extensions.extensions import ExtensionDeclarations, ExtensionRecord
from core.extensions.settings_schema import parse_settings_fields
from server.rpc import settings_methods
from server.rpc.methods import dispatch_rpc
from server.rpc.settings_methods import _trace_count
from tests.server.test_rpc import StubAdapter, make_state


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

    assert [record for record in caplog.records if record.name == "vbot.server.rpc.settings"] == []


def test_trace_count_logs_warning_on_unexpected_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = _runtime_with_storage(_RaisingStorage(RuntimeError("store corrupt")))

    caplog.set_level(logging.WARNING, logger="vbot.server.rpc.settings")
    assert _trace_count(runtime) == 0

    warning_records = [
        record
        for record in caplog.records
        if record.name == "vbot.server.rpc.settings" and "debug trace count" in record.getMessage()
    ]
    assert len(warning_records) == 1
    assert warning_records[0].exc_info is not None


def test_trace_count_logger_name() -> None:
    assert settings_methods._LOGGER.name == "vbot.server.rpc.settings"


# --- Extensions section: schema validation + restart-required split ----------


class _SchemaRegistry:
    def __init__(self, records: list[ExtensionRecord]) -> None:
        self._records = records

    def records(self) -> list[ExtensionRecord]:
        return list(self._records)


def _schemed_record() -> ExtensionRecord:
    declarations = ExtensionDeclarations()
    declarations.settings_schema = parse_settings_fields(
        [
            {"key": "url", "type": "text", "label": "URL", "required": True},
            {"key": "port", "type": "number", "label": "Port"},
            {"key": "token", "type": "secret", "label": "Token", "env_key": "HASS_TOKEN"},
        ]
    )
    return ExtensionRecord(
        name="homeassistant",
        root_path=Path("/bundled/homeassistant"),
        entry_path=Path("/bundled/homeassistant/__init__.py"),
        status="loaded",
        declarations=declarations,
    )


def _state_with_schema(tmp_path: Path) -> SimpleNamespace:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.extensions = _SchemaRegistry([_schemed_record()])
    return state


async def _update(state: SimpleNamespace, extensions: dict[str, Any]) -> dict[str, Any]:
    return await dispatch_rpc(
        state, {"method": "settings.update", "params": {"extensions": extensions}}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config",
    [
        {"homeassistant": {"unknown": 1}},  # unknown key
        {"homeassistant": {"url": 5}},  # wrong type
        {"homeassistant": {"url": "http://x", "token": "abc"}},  # secret in config
        {"homeassistant": {"port": 80}},  # missing required 'url'
    ],
)
async def test_extensions_update_rejects_invalid_schema_config(
    tmp_path: Path, config: dict[str, Any]
) -> None:
    state = _state_with_schema(tmp_path)

    result = await _update(state, {"disabled": [], "config": config})

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_request"
    # Nothing persisted.
    assert state.runtime.storage.load_extensions_settings() == {"disabled": [], "config": {}}


@pytest.mark.asyncio
async def test_extensions_update_passes_schemaless_config(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())  # no registry → no schemas

    result = await _update(state, {"disabled": [], "config": {"legacy": {"anything": [1, 2]}}})

    assert result["ok"] is True
    assert state.runtime.storage.load_extensions_settings()["config"] == {
        "legacy": {"anything": [1, 2]}
    }


@pytest.mark.asyncio
async def test_extensions_config_only_change_has_no_restart_flag(tmp_path: Path) -> None:
    state = _state_with_schema(tmp_path)

    result = await _update(
        state, {"disabled": [], "config": {"homeassistant": {"url": "http://x"}}}
    )

    assert result["ok"] is True
    assert "restart_required" not in result["result"]


@pytest.mark.asyncio
async def test_extensions_disabled_change_sets_restart_flag(tmp_path: Path) -> None:
    state = _state_with_schema(tmp_path)

    result = await _update(state, {"disabled": ["homeassistant"], "config": {}})

    assert result["ok"] is True
    assert result["result"]["restart_required"] is True


@pytest.mark.asyncio
async def test_extensions_unchanged_disabled_set_has_no_restart_flag(tmp_path: Path) -> None:
    state = _state_with_schema(tmp_path)
    # Seed a persisted disabled set, then resend it unchanged.
    await _update(state, {"disabled": ["homeassistant"], "config": {}})

    result = await _update(
        state, {"disabled": ["homeassistant"], "config": {"homeassistant": {"url": "http://y"}}}
    )

    assert result["ok"] is True
    assert "restart_required" not in result["result"]
