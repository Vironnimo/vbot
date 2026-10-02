"""Settings write RPCs: ``settings.update``, ``settings.patch`` and
``settings.set_service_key``.

Covers persistence and the returned payload, refusals that must leave settings
untouched, the live effects a saved change applies to the running Runtime, web
service API keys, and Task Model bindings. Reads live in ``test_settings_methods.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.responses import JSONResponse

from core.extensions.extensions import ExtensionDeclarations, ExtensionRecord
from core.extensions.settings_schema import parse_settings_fields
from core.model_tasks import TASK_TEXT_TO_SPEECH, TaskModelService
from core.models import Capabilities, Model, ReasoningCapabilities
from core.storage import StorageError, StorageManager
from server.rpc.methods import dispatch_rpc
from tests.server.rpc_test_support import (
    JsonObject,
    StubAdapter,
    _no_models_dev_fetch,
    make_state,
    openrouter_provider,
    resource_changes,
    rpc_error,
    rpc_result,
)

__all__ = ["_no_models_dev_fetch"]

_ASYNC_COORDINATION_TIMEOUT_SECONDS = 10.0
_SETTINGS_LOGGER = "vbot.server.rpc.settings"


def _set(path: str, value: Any) -> JsonObject:
    return {"op": "set", "path": path, "value": value}


def _unset(path: str) -> JsonObject:
    return {"op": "unset", "path": path}


def _patch(*operations: JsonObject) -> JsonObject:
    return {"operations": list(operations)}


def _stored_state(tmp_path: Path) -> SimpleNamespace:
    """RPC state backed by the real settings storage instead of the in-memory stub."""

    state = make_state(tmp_path, StubAdapter())
    state.runtime.storage = StorageManager(tmp_path / "settings-data")
    return state


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


# ---------------------------------------------------------------------------
# Persistence and returned payload
# ---------------------------------------------------------------------------


_OPENROUTER_ROUTING: JsonObject = {
    "default": {
        "mode": "allowed",
        "providers": ["anthropic", "amazon-bedrock"],
        "blocked": ["deepinfra"],
        "allow_fallbacks": False,
    },
    "models": {
        "anthropic/claude-sonnet-4": {
            "mode": "ordered",
            "providers": ["anthropic"],
            "blocked": ["google-vertex"],
            "allow_fallbacks": True,
        }
    },
}
_COMPACTION: JsonObject = {
    "enabled": False,
    "trigger": {"type": "context_ratio", "threshold": 0.9},
    "strategy": {"type": "summary_tail", "tail_tokens": 12000, "summary_model": "openai/gpt-5.2"},
}
_SUBAGENTS: JsonObject = {
    "max_subagent_depth": 6,
    "max_subagents_per_turn": 12,
    "subagent_timeout_minutes": 90,
}


@pytest.mark.asyncio
async def test_settings_update_persists_sections_and_returns_the_full_settings_payload(
    tmp_path: Path,
) -> None:
    state = _stored_state(tmp_path)
    state.runtime.providers.add(openrouter_provider())
    state.runtime.models._models["openrouter"] = []
    state.server_bind = {
        "listen_host": "127.0.0.1",
        "listen_port": 8500,
        "port_source": "VBOT_SERVER_PORT",
    }
    storage = state.runtime.storage

    result = await rpc_result(
        state,
        "settings.update",
        appearance={"language": "en", "chat_width": "wide", "chat_working_mode": "compact"},
        skills={"directories": ["~/skills", " C:/skills/team "]},
        subagents=_SUBAGENTS,
        compaction=_COMPACTION,
        defaults={"agent": {"model": "openai/gpt-4.1-mini"}},
        reflection={"enabled": True, "memory_turn_interval": 5},
        librarian={"archive_after_days": 30},
        web_search={"provider": "searxng", "searxng": {"base_url": "http://localhost:9999"}},
        session_titles={"enabled": True, "model": "openai/gpt-4.1-mini::api-key"},
        notifications={"run_completed": False},
        providers={"openrouter": {"routing": _OPENROUTER_ROUTING}},
        server={"keep_awake": True, "timezone": "America/New_York"},
        archive={"retention_days": None},
    )

    # The response is the complete settings payload, read back from storage.
    assert result == await rpc_result(state, "settings.get")
    assert result["general"]["server"] == state.server_bind
    assert result["general"]["keep_awake"] is True
    assert result["general"]["timezone"] == "America/New_York"
    assert result["appearance"] == {
        "language": "en",
        "available_languages": storage.supported_appearance_languages(),
        "chat_width": "wide",
        "chat_working_mode": "compact",
    }
    assert result["skills"] == {
        "default_directory": str(storage.data_dir / "skills"),
        "directories": ["~/skills", "C:/skills/team"],
    }
    assert result["subagents"] == _SUBAGENTS
    assert result["compaction"] == _COMPACTION
    assert result["archive"] == {"retention_days": None}
    assert result["defaults"] == {"agent": {"model": "openai/gpt-4.1-mini"}}
    # A partial section merges with its defaults.
    assert result["reflection"] == {
        "enabled": True,
        "memory_turn_interval": 5,
        "skill_model_step_interval": 10,
    }
    assert result["librarian"] == {
        "enabled": True,
        "interval_days": 7,
        "archive_after_days": 30,
        "consolidate": True,
    }
    assert {key: result["web_search"][key] for key in ("provider", "default_count", "searxng")} == {
        "provider": "searxng",
        "default_count": 12,
        "searxng": {"base_url": "http://localhost:9999"},
    }
    assert result["session_titles"] == {"enabled": True, "model": "openai/gpt-4.1-mini::api-key"}
    assert result["notifications"] == {
        "run_completed": False,
        "run_failed": True,
        "automation_failed": True,
        "update_result": True,
        "server_stopped": True,
    }
    openrouter = next(item for item in result["providers"]["items"] if item["id"] == "openrouter")
    assert openrouter["routing"] == _OPENROUTER_ROUTING


@pytest.mark.asyncio
async def test_settings_update_projects_web_fetch_services_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = make_state(tmp_path, StubAdapter())
    monkeypatch.setattr(
        state.runtime,
        "resolve_environment_credential",
        lambda name: "private-test-key" if name == "PARALLEL_API_KEY" else "",
    )

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.update",
            "params": {"web_fetch": {"provider": "parallel", "mode": "prefer"}},
        },
    )

    assert response["ok"] is True, response
    web_fetch = response["result"]["web_fetch"]
    assert (web_fetch["provider"], web_fetch["mode"]) == ("parallel", "prefer")
    service = next(item for item in web_fetch["services"] if item["id"] == "parallel")
    assert service["configured"] is True
    assert service["api_key_env"] == "PARALLEL_API_KEY"
    assert service["pricing_url"].startswith("https://")
    assert "private-test-key" not in json.dumps(response)
    assert state.runtime.storage.load_web_fetch_settings() == {
        "provider": "parallel",
        "mode": "prefer",
    }


# ---------------------------------------------------------------------------
# Web service API keys
# ---------------------------------------------------------------------------


def _key_status(result: JsonObject, section: str, provider: str) -> tuple[bool, str | None]:
    service = next(item for item in result[section]["services"] if item["id"] == provider)
    return service["configured"], service["source"]


@pytest.mark.asyncio
async def test_settings_set_service_key_saves_replaces_and_removes_one_shared_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    state = make_state(tmp_path, StubAdapter())
    reloads: list[None] = []
    monkeypatch.setattr(
        state.runtime, "reload_environment_credentials", lambda: reloads.append(None)
    )
    secrets = ("first-tavily-secret", "second-tavily-secret")

    async def set_key(value: str) -> JsonObject:
        return await rpc_result(
            state, "settings.set_service_key", api_key_env="TAVILY_API_KEY", value=value
        )

    with caplog.at_level(logging.INFO, logger=_SETTINGS_LOGGER):
        saved = await set_key(f"  {secrets[0]}\n")
        assert state.runtime.storage.load_environment()["TAVILY_API_KEY"] == secrets[0]
        replaced = await set_key(secrets[1])
        assert state.runtime.storage.load_environment()["TAVILY_API_KEY"] == secrets[1]
        removed = await set_key("  ")

    # Web search and web page reading share one Tavily credential.
    for section in ("web_search", "web_fetch"):
        assert _key_status(saved, section, "tavily") == (True, "data_dir")
        assert _key_status(replaced, section, "tavily") == (True, "data_dir")
        assert _key_status(removed, section, "tavily") == (False, None)
    assert "TAVILY_API_KEY" not in state.runtime.storage.load_environment()
    assert removed == await rpc_result(state, "settings.get")
    assert len(reloads) == 3
    messages = [record.getMessage() for record in caplog.records if record.name == _SETTINGS_LOGGER]
    assert len(messages) == 3
    assert all("TAVILY_API_KEY" in message for message in messages)
    assert "saved" in messages[0] and "removed" in messages[2]
    exposed = json.dumps([saved, replaced, removed]) + caplog.text
    assert not any(secret in exposed for secret in secrets)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"api_key_env": "OPENAI_API_KEY", "value": "sk-test"},
        {"api_key_env": "TAVILY_API_KEY", "value": None},
        {"api_key_env": "TAVILY_API_KEY", "value": "first\nsecond"},
        {"value": "sk-test"},
        {"api_key_env": "TAVILY_API_KEY", "value": "sk-test", "source": "data_dir"},
    ],
    ids=["unknown-variable", "non-string", "multi-line", "missing-variable", "extra-field"],
)
async def test_settings_set_service_key_rejects_invalid_requests(
    tmp_path: Path, params: JsonObject
) -> None:
    state = make_state(tmp_path, StubAdapter())
    env_file = tmp_path / ".env"
    env_file.write_text("TAVILY_API_KEY=kept\n", encoding="utf-8")

    error = await rpc_error(state, "settings.set_service_key", **params)

    assert error["code"] == "invalid_request"
    assert "sk-test" not in error["message"]
    assert env_file.read_text(encoding="utf-8") == "TAVILY_API_KEY=kept\n"


@pytest.mark.asyncio
async def test_settings_set_service_key_restores_the_previous_key_when_reload_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    state = make_state(tmp_path, StubAdapter())
    state.runtime.storage.set_data_dir_credential("EXA_API_KEY", "previous-key")
    reloads: list[None] = []

    def reload() -> None:
        reloads.append(None)
        if len(reloads) == 1:
            raise StorageError("Cannot read .env")

    monkeypatch.setattr(state.runtime, "reload_environment_credentials", reload)

    error = await rpc_error(
        state, "settings.set_service_key", api_key_env="EXA_API_KEY", value="new"
    )

    assert error["code"] == "domain_error"
    assert state.runtime.storage.load_environment()["EXA_API_KEY"] == "previous-key"
    assert len(reloads) == 2


@pytest.mark.asyncio
async def test_settings_patch_applies_searxng_configuration_atomically(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    result = await rpc_result(
        state,
        "settings.patch",
        **_patch(
            _set("web_search.provider", "searxng"),
            _set("web_search.searxng.base_url", "https://search.example/"),
        ),
    )

    assert result["changed"] == ["web_search.provider", "web_search.searxng.base_url"]
    assert result["restart_required"] is False
    assert state.runtime.storage.load_web_search_settings() == {
        "provider": "searxng",
        "default_count": 12,
        "searxng": {"base_url": "https://search.example/"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("seed", "active_port", "operation", "expected_change", "restart_required", "stored"),
    [
        # A restart-applied setting reports the active value and the saved one.
        pytest.param(
            {},
            8420,
            _set("server.port", 9000),
            {
                "value": 8420,
                "configured_value": 9000,
                "application": "restart",
                "restart_required": True,
            },
            True,
            {"server_port": 9000},
            id="set-restart-setting",
        ),
        # Unsetting it reports the default as the value pending the next start.
        pytest.param(
            {"server_port": 9000},
            9000,
            _unset("server.port"),
            {"value": 9000, "pending_value": 8420, "configured": False, "restart_required": True},
            True,
            {},
            id="unset-restart-setting",
        ),
        pytest.param(
            {"web_search": {"provider": "searxng"}},
            None,
            _unset("web_search.provider"),
            {"value": "brave", "configured": False, "source": "default"},
            False,
            {},
            id="unset-live-setting",
        ),
    ],
)
async def test_settings_patch_reports_each_change_with_its_effective_value(
    tmp_path: Path,
    seed: JsonObject,
    active_port: int | None,
    operation: JsonObject,
    expected_change: JsonObject,
    restart_required: bool,
    stored: JsonObject,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.storage.save_settings(seed)
    if active_port is not None:
        state.server_bind = {
            "listen_host": "127.0.0.1",
            "listen_port": active_port,
            "port_source": "settings.server_port" if seed else "default",
        }

    result = await rpc_result(state, "settings.patch", **_patch(operation))

    [change] = result["changes"]
    assert {key: change.get(key) for key in expected_change} == expected_change
    assert result["restart_required"] is restart_required
    assert state.runtime.storage.load_settings() == stored


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["settings.patch", "settings.update"])
async def test_settings_change_accepts_raw_null_extension_disabled_set(
    tmp_path: Path, method: str
) -> None:
    state = _stored_state(tmp_path)
    state.runtime.storage.save_settings({"extensions": {"disabled": None}})
    params = (
        _patch(_set("server.keep_awake", False))
        if method == "settings.patch"
        else {"server": {"keep_awake": False}}
    )

    await rpc_result(state, method, **params)

    assert state.runtime.storage.load_settings()["keep_awake"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["settings.patch", "settings.update"])
async def test_reset_last_default_preserves_unknown_fields(tmp_path: Path, method: str) -> None:
    state = _stored_state(tmp_path)
    storage = state.runtime.storage
    storage.save_settings(
        {"defaults": {"agent": {"temperature": 0.2, "future_option": True}, "future_section": 1}}
    )
    params = (
        _patch(_unset("defaults.agent.temperature"))
        if method == "settings.patch"
        else {"defaults": {"agent": {"temperature": None}}}
    )

    await rpc_result(state, method, **params)

    assert storage.load_defaults() == {}
    assert json.loads(storage.settings_path.read_text(encoding="utf-8"))["defaults"] == {
        "agent": {"future_option": True},
        "future_section": 1,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["settings.update", "settings.patch"])
async def test_extensions_update_persists_the_section_with_schemaless_config(
    tmp_path: Path,
    method: str,
) -> None:
    state = _stored_state(tmp_path)  # no registry, so no schemas
    config = {"anything": [None, True, -2, 1.25, {"number": 1e300}]}
    params = (
        {"extensions": {"disabled": ["legacy"], "config": {"legacy": config}}}
        if method == "settings.update"
        else _patch(
            _set("extensions.disabled", ["legacy"]),
            _set('extensions.config["legacy"]', config),
        )
    )

    result = await rpc_result(state, method, **params)

    assert state.runtime.storage.load_extensions_settings() == {
        "disabled": ["legacy"],
        "config": {"legacy": config},
    }
    assert JSONResponse(result).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "registry", "available"),
    [
        # Without a registry accessor, the first-party backends are selectable.
        ("vector", None, ["hybrid", "sqlite_fts", "vector"]),
        ("my_ext_backend", ["sqlite_fts", "my_ext_backend"], ["my_ext_backend", "sqlite_fts"]),
    ],
    ids=["first-party", "extension"],
)
async def test_settings_update_accepts_a_registered_recall_backend(
    tmp_path: Path, backend: str, registry: list[str] | None, available: list[str]
) -> None:
    state = make_state(tmp_path, StubAdapter())
    if registry is not None:
        state.runtime.available_recall_backends = lambda: registry

    result = await rpc_result(state, "settings.update", recall={"backend": backend})

    assert state.runtime.storage.load_recall_settings() == {"backend": backend}
    assert state.runtime.recall_reload_count == 1
    assert result["recall"] == {"backend": backend, "available_backends": available}


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def _extension_config(config: JsonObject) -> JsonObject:
    return {"extensions": {"disabled": [], "config": {"homeassistant": config}}}


_TTS_BINDING = {"target": "openai/gpt-4o-mini-tts::api-key", "options": {"voice": "Mia"}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "named"),
    [
        # A section the parser rejects (the parser's rows: tests/core/settings).
        ("settings.update", {"skills": []}, "params.skills must be an object"),
        (
            "settings.update",
            {"debug": {"enabled": True}, "base": {"recall": {"backend": "sqlite_fts"}}},
            "params.base names sections the update does not change: recall",
        ),
        (
            "settings.update",
            {"debug": {"enabled": True}, "base": []},
            "params.base must be an object",
        ),
        (
            "task_model.update",
            {
                "model_tasks": {TASK_TEXT_TO_SPEECH: {"target": "openai/gpt-4o-mini-tts::api-key"}},
                "base": {"debug": {"enabled": False}},
            },
            "params.base names sections the update does not change: debug",
        ),
        # Values that parse but that the running Runtime does not accept.
        (
            "settings.update",
            {"recall": {"backend": "unknown_backend"}},
            "params.recall.backend must be one of: hybrid, sqlite_fts, vector",
        ),
        (
            "settings.patch",
            _patch(_set("recall.backend", "unknown_backend")),
            "params.recall.backend must be one of: hybrid, sqlite_fts, vector",
        ),
        (
            "settings.update",
            _extension_config({"unknown": 1}),
            "invalid extension config for 'homeassistant'",
        ),
        (
            "settings.update",
            _extension_config({"url": 5}),
            "invalid extension config for 'homeassistant'",
        ),
        (
            "settings.update",
            _extension_config({"url": "http://x", "token": "abc"}),
            "invalid extension config for 'homeassistant'",
        ),
        (
            "settings.update",
            _extension_config({"port": 80}),
            "invalid extension config for 'homeassistant'",
        ),
        (
            "settings.update",
            {"model_tasks": {TASK_TEXT_TO_SPEECH: _TTS_BINDING}},
            "must be one of: alloy, echo",
        ),
        (
            "settings.patch",
            _patch(_set('model_tasks["text_to_speech"]', _TTS_BINDING)),
            "must be one of: alloy, echo",
        ),
        # Secrets never go through settings; the error names the safe command.
        (
            "settings.patch",
            _patch(_set('extensions.config["homeassistant"]["token"]', "must-not-be-stored")),
            "vbot extensions homeassistant set token --stdin",
        ),
        ("settings.patch", _patch({"op": [], "path": "server.port", "value": 8420}), None),
        ("settings.patch", _patch({"op": {}, "path": "server.port", "value": 8420}), None),
        ("settings.patch", _patch(_set("server.port", 10**400)), None),
        ("settings.patch", _patch(_set("defaults.agent.temperature", 10**400)), None),
        ("settings.patch", _patch(_set("live_voice.enabled", True)), None),
        # One invalid operation rejects the whole patch.
        (
            "settings.patch",
            _patch(_set("web_search.provider", "searxng"), _set("debug.trace_limit", 0)),
            None,
        ),
        *[
            pytest.param(
                method,
                (
                    _patch(
                        _set(
                            'extensions.config["audit"]',
                            {"nested": [{"amount": json.loads(number)}]},
                        )
                    )
                    if method == "settings.patch"
                    else {
                        "extensions": {
                            "config": {"audit": {"nested": [{"amount": json.loads(number)}]}}
                        }
                    }
                ),
                None,
                id=f"{method}-{number}",
            )
            for method in ["settings.patch", "settings.update"]
            for number in ["1e999", "NaN", "Infinity", "-Infinity"]
        ],
    ],
)
async def test_rejected_settings_changes_persist_nothing(
    tmp_path: Path, method: str, params: JsonObject, named: str | None
) -> None:
    state = _state_with_schema(tmp_path)
    state.runtime.storage = StorageManager(tmp_path / "settings-data")
    _add_tts_model(state)
    storage = state.runtime.storage
    storage.save_settings({"web_search": {"provider": "brave"}})
    original = storage.settings_path.read_bytes()
    appearance = storage.load_appearance_settings()

    error = await rpc_error(state, method, **params)

    assert error["code"] == "invalid_request"
    if named is not None:
        assert named in error["message"]
    assert storage.load_settings() == {"web_search": {"provider": "brave"}}
    assert storage.load_appearance_settings() == appearance
    assert resource_changes(state) == []
    assert storage.settings_path.read_bytes() == original
    assert JSONResponse({"ok": False, "error": error}).status_code == 200
    for read_method in ["settings.values", "settings.get_raw"]:
        assert JSONResponse(await rpc_result(state, read_method)).status_code == 200


_TTS_TARGET = "openai/gpt-4o-mini-tts::api-key"


def _tts_binding(**options: str) -> JsonObject:
    return {TASK_TEXT_TO_SPEECH: {"target": _TTS_TARGET, "options": options}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "stored", "update", "stale_base", "fresh_base", "stale_path"),
    [
        pytest.param(
            "settings.update",
            {"debug": {"enabled": False, "trace_limit": 80}},
            {"debug": {"enabled": True, "trace_limit": 50}},
            {"debug": {"enabled": False, "trace_limit": 50}},
            {"debug": {"enabled": False, "trace_limit": 80}},
            "debug.trace_limit",
            id="settings",
        ),
        pytest.param(
            "task_model.update",
            {"model_tasks": _tts_binding(voice="echo")},
            {"model_tasks": _tts_binding(voice="alloy")},
            {"model_tasks": _tts_binding()},
            {"model_tasks": _tts_binding(voice="echo")},
            "model_tasks.text_to_speech.options.voice",
            id="task-model",
        ),
    ],
)
async def test_update_based_on_stale_values_is_refused_without_effects(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    method: str,
    stored: JsonObject,
    update: JsonObject,
    stale_base: JsonObject,
    fresh_base: JsonObject,
    stale_path: str,
) -> None:
    state = _tts_state(tmp_path)
    storage = state.runtime.storage
    storage.save_settings(stored)
    original = storage.settings_path.read_bytes()

    with caplog.at_level(logging.DEBUG, logger="vbot"):
        error = await rpc_error(state, method, **update, base=stale_base)

    assert error["code"] == "settings_conflict"
    assert stale_path in error["message"]
    assert storage.settings_path.read_bytes() == original
    assert resource_changes(state) == []
    # An expected refusal the writer resolves by reading again: logged once, quietly.
    refusals = [record for record in caplog.records if "refused" in record.getMessage()]
    assert [record.levelno for record in refusals] == [logging.DEBUG]
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]

    await rpc_result(state, method, **update, base=fresh_base)

    section = next(iter(update))
    assert storage.load_settings()[section] == update[section]


@pytest.mark.asyncio
async def test_settings_update_maps_storage_errors_to_domain_error_without_partial_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    original_settings = {
        "appearance": {"language": "en", "chat_width": "wide"},
        "server_port": 8500,
    }
    state.runtime.storage.save_settings(original_settings)

    def fail_settings_update(_settings_update: object, **_options: object) -> JsonObject:
        raise StorageError("compaction write failed")

    monkeypatch.setattr(state.runtime.storage, "update_settings_sections", fail_settings_update)

    error = await rpc_error(
        state,
        "settings.update",
        appearance={"language": "en"},
        compaction={**_COMPACTION, "strategy": {**_COMPACTION["strategy"], "summary_model": None}},
    )

    assert error["code"] == "domain_error"
    assert state.runtime.storage.load_settings() == original_settings


# ---------------------------------------------------------------------------
# Live effects on the running Runtime
# ---------------------------------------------------------------------------


_NO_EFFECTS: JsonObject = {
    "extension_reloads": 0,
    "disabled_changes": [],
    "skills_reloaded": False,
    "recall_reloads": 0,
    "keep_awake": [],
    "invalidated": [],
}
_EXTENSION_LAYER = ["commands", "extensions", "skills"]


def _extensions(disabled: list[str]) -> JsonObject:
    return {"extensions": {"disabled": disabled, "config": {}}}


# Which live services a Settings change refreshes is the Runtime's decision
# (tests/core/runtime/test_runtime_settings.py). These rows cover the RPC
# wiring: both methods hand over the persisted before/after Settings, an
# explicit section save requests its refresh, a changed Extension layer
# invalidates its Commands and page descriptors, and a rescanned Skill layer
# invalidates the Skill inventory.
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("seed", "method", "params", "effects"),
    [
        pytest.param(
            {},
            "settings.patch",
            _patch(_set("extensions.directories", ["~/extra-extensions"])),
            {"extension_reloads": 1, "invalidated": _EXTENSION_LAYER},
            id="patch-extension-directories",
        ),
        # A patch elsewhere neither re-validates nor reloads an extension-provided backend.
        pytest.param(
            {"recall": {"backend": "extension_backend"}},
            "settings.patch",
            _patch(_set("web_search.provider", "searxng")),
            {},
            id="patch-unrelated",
        ),
        pytest.param(
            {},
            "settings.update",
            _extensions(["homeassistant"]),
            {"disabled_changes": [{"homeassistant"}], "invalidated": _EXTENSION_LAYER},
            id="update-disable",
        ),
        pytest.param(
            {},
            "settings.update",
            {"skills": {"directories": ["~/extra-skills"]}},
            {"skills_reloaded": True, "invalidated": ["skills"]},
            id="update-skill-directories",
        ),
        # A valid config passes the Extension's schema and applies live through
        # its config reader.
        pytest.param(
            {"extensions": {"disabled": ["homeassistant"]}},
            "settings.update",
            {
                "extensions": {
                    "disabled": ["homeassistant"],
                    "config": {"homeassistant": {"url": "http://y"}},
                }
            },
            {},
            id="update-extension-config",
        ),
        # Re-saving the Recall section through the Settings page rebuilds the backend.
        pytest.param(
            {"recall": {"backend": "sqlite_fts"}},
            "settings.update",
            {"recall": {"backend": "sqlite_fts"}},
            {"recall_reloads": 1},
            id="update-recall-resave",
        ),
        # Live effects run after the write, so they read the saved value.
        pytest.param(
            {},
            "settings.update",
            {"server": {"keep_awake": True}},
            {"keep_awake": [True]},
            id="update-keep-awake",
        ),
    ],
)
async def test_saved_settings_apply_their_live_effects(
    tmp_path: Path, seed: JsonObject, method: str, params: JsonObject, effects: JsonObject
) -> None:
    state = _state_with_schema(tmp_path)
    runtime = state.runtime
    runtime.storage.save_settings(seed)
    previous_skills = runtime.skills
    keep_awake: list[bool] = []
    runtime.reload_keep_awake = lambda: keep_awake.append(
        runtime.storage.load_settings()["keep_awake"]
    )

    await rpc_result(state, method, **params)

    assert {
        "extension_reloads": runtime.extension_reload_count,
        "disabled_changes": runtime.extension_disabled_changes,
        "skills_reloaded": runtime.skills is not previous_skills,
        "recall_reloads": runtime.recall_reload_count,
        "keep_awake": keep_awake,
        "invalidated": [change["kind"] for change in resource_changes(state)],
    } == {**_NO_EFFECTS, **effects}


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["settings.patch", "settings.update"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_skill_settings_refresh_is_async_serialized_and_survives_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, cancel: bool
) -> None:
    state = _stored_state(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()
    refreshed: list[list[str]] = []
    first_directory = str(tmp_path / "first")
    second_directory = str(tmp_path / "second")

    def blocking_reload() -> None:
        raise AssertionError("Skill scans must not run synchronously on the Event Loop")

    async def reload_skills() -> None:
        directories = list(state.runtime.storage.load_settings()["skill_directories"])
        if not entered.is_set():
            entered.set()
            await release.wait()
        refreshed.append(directories)

    def request(name: str, directory: str) -> JsonObject:
        params = (
            {"skills": {"directories": [directory]}}
            if name == "settings.update"
            else _patch(_set("skills.directories", [directory]))
        )
        return {"method": name, "params": params}

    monkeypatch.setattr(state.runtime, "reload_skills", blocking_reload)
    monkeypatch.setattr(state.runtime, "reload_skills_async", reload_skills)
    first = asyncio.create_task(dispatch_rpc(state, request(method, first_directory)))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), _ASYNC_COORDINATION_TIMEOUT_SECONDS)
        if cancel:
            first.cancel()
            await asyncio.sleep(0)
            first.cancel()
        other_method = "settings.update" if method == "settings.patch" else "settings.patch"
        second = asyncio.create_task(dispatch_rpc(state, request(other_method, second_directory)))
        await asyncio.sleep(0)
        assert not first.done()
        assert not second.done()
        assert state.runtime.storage.load_settings()["skill_directories"] == [first_directory]
        release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(first, _ASYNC_COORDINATION_TIMEOUT_SECONDS)
        else:
            first_response = await asyncio.wait_for(first, _ASYNC_COORDINATION_TIMEOUT_SECONDS)
            assert first_response["ok"] is True
        assert (await asyncio.wait_for(second, _ASYNC_COORDINATION_TIMEOUT_SECONDS))["ok"] is True
        assert refreshed == [[first_directory], [second_directory]]
        assert state.runtime.storage.load_settings()["skill_directories"] == [second_directory]
    finally:
        release.set()
        await asyncio.gather(
            first, *([second] if second is not None else []), return_exceptions=True
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "logged"),
    [
        pytest.param(
            "settings.update",
            {"session_titles": {"enabled": True, "model": "openai/gpt-4.1-mini::api-key"}},
            ["session_titles.model"],
            id="changed-section",
        ),
        # Appearance is a per-browser preference, not an operational change.
        pytest.param(
            "settings.update",
            {
                "appearance": {
                    "language": "en",
                    "chat_width": "wide",
                    "chat_working_mode": "compact",
                }
            },
            [],
            id="appearance-only",
        ),
        pytest.param(
            "settings.update",
            _extensions(["two"]),
            ["extensions.disabled extensions_enabled=one extensions_disabled=two"],
            id="extension-enablement",
        ),
        pytest.param(
            "settings.patch",
            _patch(_set("web_search.provider", "searxng")),
            ["paths=web_search.provider"],
            id="patch",
        ),
    ],
)
async def test_settings_changes_log_only_operational_changes(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    method: str,
    params: JsonObject,
    logged: list[str],
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.storage.save_settings({"extensions": {"disabled": ["one"]}})

    with caplog.at_level(logging.INFO, logger=_SETTINGS_LOGGER):
        await rpc_result(state, method, **params)

    messages = [record.getMessage() for record in caplog.records if record.name == _SETTINGS_LOGGER]
    assert len(messages) == len(logged)
    for message, fragment in zip(messages, logged, strict=True):
        assert fragment in message
    # Changed setting paths are named, their values never.
    assert "gpt-4.1-mini" not in caplog.text


# ---------------------------------------------------------------------------
# Task Model bindings
# ---------------------------------------------------------------------------


def _tts_state(tmp_path: Path) -> SimpleNamespace:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.storage = StorageManager(tmp_path)
    _add_tts_model(state)
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["settings.update", "settings.patch"])
async def test_task_target_switch_persists_with_validated_options(
    tmp_path: Path, method: str
) -> None:
    state = _tts_state(tmp_path)
    model = state.runtime.models.get("openai", "gpt-4o-mini-tts")
    state.runtime.models._models["openai"].append(
        replace(
            model,
            model_id="tts-default",
            capabilities=replace(model.capabilities, supported_voices=()),
        )
    )
    state.runtime.storage.update_model_task_settings(
        {
            TASK_TEXT_TO_SPEECH: {
                "target": "openai/retired-tts::api-key",
                "options": {"retired_option": True},
            }
        }
    )
    target = "openai/tts-default::api-key"
    params = (
        {"model_tasks": {TASK_TEXT_TO_SPEECH: {"target": target}}}
        if method == "settings.update"
        else _patch(_set('model_tasks["text_to_speech"].target', target))
    )

    await rpc_result(state, method, **params)

    binding = state.runtime.storage.load_model_task_settings()[TASK_TEXT_TO_SPEECH]
    assert binding == {"target": target, "options": {}}
    state.runtime.model_tasks.validate_binding(TASK_TEXT_TO_SPEECH, binding)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["settings.update", "settings.patch", "task_model.update"])
async def test_equivalent_task_target_spelling_preserves_options(
    tmp_path: Path, method: str
) -> None:
    """Re-spelling the same target keeps options that would no longer validate."""

    state = _tts_state(tmp_path)
    options = {"retired_option": True}
    state.runtime.storage.update_model_task_settings(
        {TASK_TEXT_TO_SPEECH: {"target": "openai/gpt-4o-mini-tts::api-key", "options": options}}
    )
    respelled = "openai/gpt-4o-mini-tts::openai:api-key"
    params = (
        _patch(_set('model_tasks["text_to_speech"].target', respelled))
        if method == "settings.patch"
        else {"model_tasks": {TASK_TEXT_TO_SPEECH: {"target": respelled}}}
    )

    await rpc_result(state, method, **params)

    binding = state.runtime.storage.load_model_task_settings()[TASK_TEXT_TO_SPEECH]
    assert binding["options"] == options


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["settings.update", "settings.patch"])
async def test_unchanged_task_binding_does_not_block_other_settings(
    tmp_path: Path, method: str
) -> None:
    state = _tts_state(tmp_path)
    binding = {"target": "openai/retired-tts::api-key", "options": {"retired_option": True}}
    state.runtime.storage.update_model_task_settings({TASK_TEXT_TO_SPEECH: binding})
    previous = state.runtime.storage.load_model_task_settings()
    params = (
        {"model_tasks": {TASK_TEXT_TO_SPEECH: binding}, "server": {"keep_awake": True}}
        if method == "settings.update"
        else _patch(_set('model_tasks["text_to_speech"]', binding), _set("server.keep_awake", True))
    )

    await rpc_result(state, method, **params)

    assert state.runtime.storage.load_model_task_settings() == previous
    assert state.runtime.storage.load_settings()["keep_awake"] is True


@pytest.mark.asyncio
async def test_settings_patch_can_remove_incomplete_task_binding(tmp_path: Path) -> None:
    state = _tts_state(tmp_path)
    state.runtime.storage.save_settings({"model_tasks": {TASK_TEXT_TO_SPEECH: {}}})

    await rpc_result(state, "settings.patch", **_patch(_unset('model_tasks["text_to_speech"]')))

    assert state.runtime.storage.load_model_task_settings() == {}
