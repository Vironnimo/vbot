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
from core.model_tasks import TASK_TEXT_TO_SPEECH, TaskModelService
from core.models import Capabilities, Model, ReasoningCapabilities
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


@pytest.mark.asyncio
async def test_session_title_settings_round_trip_and_validate_model_connection(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = make_state(tmp_path, StubAdapter())

    with caplog.at_level(logging.INFO, logger="vbot.server.rpc.settings"):
        result = await dispatch_rpc(
            state,
            {
                "method": "settings.update",
                "params": {
                    "session_titles": {
                        "enabled": True,
                        "model": "openai/gpt-4.1-mini::api-key",
                    }
                },
            },
        )

    assert result["ok"] is True
    assert result["result"]["session_titles"] == {
        "enabled": True,
        "model": "openai/gpt-4.1-mini::api-key",
    }
    assert state.runtime.storage.load_session_title_settings() == result["result"]["session_titles"]
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "vbot.server.rpc.settings"
    ]
    assert messages == ["Settings updated (sections=session_titles)"]


@pytest.mark.asyncio
async def test_appearance_only_update_does_not_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    state = make_state(tmp_path, StubAdapter())

    with caplog.at_level(logging.INFO, logger="vbot.server.rpc.settings"):
        result = await dispatch_rpc(
            state,
            {
                "method": "settings.update",
                "params": {
                    "appearance": {
                        "language": "en",
                        "chat_width": "wide",
                        "chat_working_mode": "compact",
                    }
                },
            },
        )

    assert result["ok"] is True
    assert not [record for record in caplog.records if record.name == "vbot.server.rpc.settings"]


def _add_tts_model(state: SimpleNamespace) -> None:
    state.runtime.models._models["openai"].append(
        Model(
            model_id="gpt-4o-mini-tts",
            name="GPT-4o mini TTS",
            capabilities=Capabilities(
                vision=False,
                tools=False,
                json_mode=False,
                reasoning=ReasoningCapabilities(supported=False),
                input_modalities=("text",),
                output_modalities=("speech",),
                supported_voices=("alloy", "echo"),
            ),
            context_window=None,
            max_output_tokens=None,
        )
    )
    state.runtime.model_tasks = TaskModelService(
        state.runtime.providers,
        state.runtime.models,
        state.runtime.provider_credentials,
        state.runtime.storage,
    )


@pytest.mark.asyncio
async def test_settings_update_rejects_invalid_task_model_option_before_persistence(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    _add_tts_model(state)

    result = await dispatch_rpc(
        state,
        {
            "method": "settings.update",
            "params": {
                "model_tasks": {
                    TASK_TEXT_TO_SPEECH: {
                        "target": "openai/gpt-4o-mini-tts::api-key",
                        "options": {"voice": "Mia"},
                    }
                }
            },
        },
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_request"
    assert "must be one of: alloy, echo" in result["error"]["message"]
    assert state.runtime.storage.load_model_task_settings() == {}


@pytest.mark.asyncio
async def test_settings_patch_rejects_invalid_task_model_option_before_persistence(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    _add_tts_model(state)

    result = await dispatch_rpc(
        state,
        {
            "method": "settings.patch",
            "params": {
                "operations": [
                    {
                        "op": "set",
                        "path": 'model_tasks["text_to_speech"]',
                        "value": {
                            "target": "openai/gpt-4o-mini-tts::api-key",
                            "options": {"voice": "Mia"},
                        },
                    }
                ]
            },
        },
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_request"
    assert "must be one of: alloy, echo" in result["error"]["message"]
    assert state.runtime.storage.load_model_task_settings() == {}


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
async def test_settings_path_rejects_extension_secret_with_safe_command_hint(
    tmp_path: Path,
) -> None:
    state = _state_with_schema(tmp_path)

    result = await dispatch_rpc(
        state,
        {
            "method": "settings.patch",
            "params": {
                "operations": [
                    {
                        "op": "set",
                        "path": 'extensions.config["homeassistant"]["token"]',
                        "value": "must-not-be-stored",
                    }
                ]
            },
        },
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_request"
    assert "vbot extensions homeassistant set token --stdin" in result["error"]["message"]
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
async def test_extensions_config_only_change_touches_neither_seam(tmp_path: Path) -> None:
    state = _state_with_schema(tmp_path)

    result = await _update(
        state, {"disabled": [], "config": {"homeassistant": {"url": "http://x"}}}
    )

    assert result["ok"] is True
    # A config-value-only save applies live via ExtensionAPI.get_config(): it
    # neither reloads the layer nor runs the live-disable path.
    assert state.runtime.extension_reload_count == 0
    assert state.runtime.extension_disabled_changes == []
    assert state.event_bus.events == []


@pytest.mark.asyncio
async def test_extensions_disabled_path_patch_invalidates_commands(tmp_path: Path) -> None:
    state = _state_with_schema(tmp_path)

    result = await dispatch_rpc(
        state,
        {
            "method": "settings.patch",
            "params": {
                "operations": [
                    {
                        "op": "set",
                        "path": "extensions.disabled",
                        "value": ["homeassistant"],
                    }
                ]
            },
        },
    )

    assert result["ok"] is True
    assert state.runtime.extension_disabled_changes == [{"homeassistant"}]
    assert state.event_bus.events[-1]["payload"] == {"kind": "commands"}


def _reset_extension_spies(state: SimpleNamespace) -> None:
    """Reset the StubRuntime's reload / live-disable call counters between saves."""
    state.runtime.extension_reload_count = 0
    state.runtime.extension_disabled_changes.clear()


@pytest.mark.asyncio
async def test_extensions_newly_disabled_applies_live(tmp_path: Path) -> None:
    state = _state_with_schema(tmp_path)

    result = await _update(state, {"disabled": ["homeassistant"], "config": {}})

    assert result["ok"] is True
    # A disable-only save takes the surgical live-disable path (no full reload).
    assert state.runtime.extension_disabled_changes == [{"homeassistant"}]
    assert state.runtime.extension_reload_count == 0


@pytest.mark.asyncio
async def test_extensions_newly_enabled_reloads_layer(tmp_path: Path) -> None:
    state = _state_with_schema(tmp_path)
    # Seed a persisted disabled set, then re-enable (remove from disabled).
    await _update(state, {"disabled": ["homeassistant"], "config": {}})
    _reset_extension_spies(state)

    result = await _update(state, {"disabled": [], "config": {}})

    assert result["ok"] is True
    # Enabling rebuilds the whole layer; the surgical live-disable path is NOT run.
    assert state.runtime.extension_reload_count == 1
    assert state.runtime.extension_disabled_changes == []


@pytest.mark.asyncio
async def test_extensions_mixed_enable_and_disable_reloads_only(tmp_path: Path) -> None:
    state = _state_with_schema(tmp_path)
    # Start with "one" disabled; then flip to "two" disabled: enables one, disables two.
    await _update(state, {"disabled": ["one"], "config": {}})
    _reset_extension_spies(state)

    result = await _update(state, {"disabled": ["two"], "config": {}})

    assert result["ok"] is True
    # A save that enables any name reloads the whole layer and does nothing else —
    # the reload reads the freshly persisted state, so it also applies the disable.
    assert state.runtime.extension_reload_count == 1
    assert state.runtime.extension_disabled_changes == []


@pytest.mark.asyncio
async def test_extensions_unchanged_disabled_set_touches_neither_seam(
    tmp_path: Path,
) -> None:
    state = _state_with_schema(tmp_path)
    # Seed a persisted disabled set, then resend it unchanged (config-only change).
    await _update(state, {"disabled": ["homeassistant"], "config": {}})
    _reset_extension_spies(state)

    result = await _update(
        state, {"disabled": ["homeassistant"], "config": {"homeassistant": {"url": "http://y"}}}
    )

    assert result["ok"] is True
    # No disabled-set delta: neither the reload nor the live-disable path runs.
    assert state.runtime.extension_reload_count == 0
    assert state.runtime.extension_disabled_changes == []
