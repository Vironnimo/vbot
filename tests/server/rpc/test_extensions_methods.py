"""Tests for extension visibility RPC handlers.

Coverage:
- ``extensions.list``: payload for loaded / failed / disabled records, capability
  summary, persisted-config merge, empty when no registry, rejects params.
- ``extensions.reload``: rejects params, drives the runtime rebuild, returns the
  ``extensions.list`` shape.
- ``settings.update`` ``extensions`` section: round-trip persistence plus the live
  reload / live-disable routing of the disabled-set delta.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.extensions.extensions import (
    ExtensionDeclarations,
    ExtensionManifest,
    ExtensionRecord,
    RecallBackendDeclaration,
    ToolDeclaration,
)
from core.extensions.settings_schema import parse_settings_fields
from server.rpc.methods import dispatch_rpc
from tests.server.test_rpc import StubAdapter, make_state

JsonObject = dict[str, Any]


def _noop_handler(*_args: Any, **_kwargs: Any) -> None:
    return None


class _Registry:
    """Minimal stand-in for ``ExtensionRegistry`` exposing ``records()``."""

    def __init__(self, records: list[ExtensionRecord]) -> None:
        self._records = records

    def records(self) -> list[ExtensionRecord]:
        return list(self._records)


class _Storage:
    def __init__(self, config: dict[str, dict[str, Any]]) -> None:
        self._config = config
        self.credentials: dict[str, str] = {}
        self.removed: list[str] = []

    def load_extensions_settings(self) -> JsonObject:
        return {"disabled": [], "config": self._config}

    def set_data_dir_credential(self, key: str, value: str) -> None:
        self.credentials[key] = value

    def remove_data_dir_credential(self, key: str) -> bool:
        self.removed.append(key)
        return self.credentials.pop(key, None) is not None


class _ToolRegistry:
    """Minimal ``ToolRegistry`` stand-in with per-name readiness predicates.

    ``ready`` maps a tool name to its predicate; a name absent from ``ready`` is
    always ready. A name never added at all raises on ``get`` — the "declared but
    unregistered" case.
    """

    def __init__(self, ready: dict[str, Any] | None = None) -> None:
        self._ready = ready or {}

    def add(self, name: str, *, ready: Any = None) -> None:
        self._ready[name] = ready

    def get(self, name: str) -> Any:
        if name not in self._ready:
            raise KeyError(name)
        return SimpleNamespace(name=name, ready=self._ready[name])


class _Runtime(SimpleNamespace):
    """Runtime stub exposing the credential seam the handlers touch."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.reloaded = 0

    def resolve_environment_credential(self, key: str) -> str:
        value: str = self.storage.credentials.get(key, "")
        return value

    def reload_provider_credentials(self) -> None:
        self.reloaded += 1


def _state_with_records(
    records: list[ExtensionRecord],
    config: dict[str, dict[str, Any]] | None = None,
    tools: _ToolRegistry | None = None,
) -> SimpleNamespace:
    runtime = _Runtime(
        extensions=_Registry(records),
        storage=_Storage(config or {}),
        tools=tools if tools is not None else _ToolRegistry(),
    )
    return SimpleNamespace(runtime=runtime)


def _loaded_record() -> ExtensionRecord:
    declarations = ExtensionDeclarations()
    declarations.hooks["tool_call"].append(_noop_handler)
    declarations.hooks["run_end"].append(_noop_handler)
    declarations.tools.append(
        ToolDeclaration("word_count", "Count words", {"type": "object"}, _noop_handler)
    )
    declarations.recall_backends.append(RecallBackendDeclaration("my_backend", _noop_handler))
    declarations.startup.append(_noop_handler)
    return ExtensionRecord(
        name="guard_bash",
        root_path=Path("/ext/guard_bash"),
        entry_path=Path("/ext/guard_bash/__init__.py"),
        status="loaded",
        manifest=ExtensionManifest(
            version="1.2.0",
            description="Guards dangerous bash",
            api_version=1,
            display_name="Bash Guard",
        ),
        declarations=declarations,
    )


@pytest.mark.asyncio
async def test_extensions_list_returns_loaded_failed_disabled_records() -> None:
    failed = ExtensionRecord(
        name="broken",
        root_path=Path("/ext/broken.py"),
        entry_path=Path("/ext/broken.py"),
        status="failed",
        error="import failed: boom",
    )
    disabled = ExtensionRecord(
        name="off",
        root_path=Path("/ext/off.py"),
        entry_path=Path("/ext/off.py"),
        status="disabled",
    )
    tools = _ToolRegistry()
    tools.add("word_count")  # registered and ready (no predicate)
    state = _state_with_records(
        [_loaded_record(), failed, disabled],
        config={"guard_bash": {"deny": ["rm -rf"]}},
        tools=tools,
    )

    result = await dispatch_rpc(state, {"method": "extensions.list", "params": {}})

    assert result["ok"] is True
    extensions = result["result"]["extensions"]
    assert [item["name"] for item in extensions] == ["guard_bash", "broken", "off"]

    loaded, failed_item, disabled_item = extensions
    assert loaded == {
        "name": "guard_bash",
        "status": "loaded",
        "disabled": False,
        "root": str(Path("/ext/guard_bash")),
        "entry": str(Path("/ext/guard_bash/__init__.py")),
        "error": None,
        "overridden_by": None,
        "capability_errors": [],
        "version": "1.2.0",
        "description": "Guards dangerous bash",
        "display_name": "Bash Guard",
        "api_version": 1,
        "config": {"deny": ["rm -rf"]},
        "settings_schema": None,
        "ready_state": "ready",
        "capabilities": {
            "hooks": {"tool_call": 1, "run_end": 1},
            "tools": [{"name": "word_count", "ready": True}],
            "recall_backends": ["my_backend"],
            "startup": True,
            "shutdown": False,
        },
    }
    assert failed_item["status"] == "failed"
    assert failed_item["error"] == "import failed: boom"
    assert failed_item["config"] == {}
    assert failed_item["capabilities"]["tools"] == []
    assert disabled_item["status"] == "disabled"
    assert disabled_item["disabled"] is True


@pytest.mark.asyncio
async def test_extensions_list_round_trips_overridden_record() -> None:
    overridden = ExtensionRecord(
        name="homeassistant",
        root_path=Path("/bundled/homeassistant"),
        entry_path=Path("/bundled/homeassistant/__init__.py"),
        status="overridden",
        overridden_by=str(Path("/data/extensions/homeassistant/__init__.py")),
    )
    state = _state_with_records([overridden])

    result = await dispatch_rpc(state, {"method": "extensions.list", "params": {}})

    assert result["ok"] is True
    (item,) = result["result"]["extensions"]
    assert item["status"] == "overridden"
    assert item["disabled"] is False
    assert item["overridden_by"] == str(Path("/data/extensions/homeassistant/__init__.py"))


def _tooled_record(name: str = "homeassistant") -> ExtensionRecord:
    declarations = ExtensionDeclarations()
    declarations.tools.append(
        ToolDeclaration("ha_call_service", "Call a service", {"type": "object"}, _noop_handler)
    )
    return ExtensionRecord(
        name=name,
        root_path=Path(f"/bundled/{name}"),
        entry_path=Path(f"/bundled/{name}/__init__.py"),
        status="loaded",
        declarations=declarations,
    )


@pytest.mark.asyncio
async def test_ready_state_waiting_when_a_declared_tool_is_not_ready() -> None:
    tools = _ToolRegistry()
    tools.add("ha_call_service", ready=lambda: False)
    state = _state_with_records([_tooled_record()], tools=tools)

    result = await dispatch_rpc(state, {"method": "extensions.list", "params": {}})

    (item,) = result["result"]["extensions"]
    assert item["ready_state"] == "waiting"
    assert item["capabilities"]["tools"] == [{"name": "ha_call_service", "ready": False}]


@pytest.mark.asyncio
async def test_ready_state_ready_when_all_declared_tools_are_ready() -> None:
    tools = _ToolRegistry()
    tools.add("ha_call_service", ready=lambda: True)
    state = _state_with_records([_tooled_record()], tools=tools)

    result = await dispatch_rpc(state, {"method": "extensions.list", "params": {}})

    (item,) = result["result"]["extensions"]
    assert item["ready_state"] == "ready"
    assert item["capabilities"]["tools"] == [{"name": "ha_call_service", "ready": True}]


@pytest.mark.asyncio
async def test_ready_state_waiting_when_a_declared_tool_is_unregistered() -> None:
    # An empty registry: the declared name never registered (e.g. a collision
    # skipped it) → reported not ready → the extension is waiting.
    state = _state_with_records([_tooled_record()], tools=_ToolRegistry())

    result = await dispatch_rpc(state, {"method": "extensions.list", "params": {}})

    (item,) = result["result"]["extensions"]
    assert item["ready_state"] == "waiting"
    assert item["capabilities"]["tools"] == [{"name": "ha_call_service", "ready": False}]


@pytest.mark.asyncio
async def test_ready_state_ready_for_a_loaded_extension_with_no_tools() -> None:
    record = ExtensionRecord(
        name="hooks_only",
        root_path=Path("/ext/hooks_only.py"),
        entry_path=Path("/ext/hooks_only.py"),
        status="loaded",
    )
    state = _state_with_records([record])

    result = await dispatch_rpc(state, {"method": "extensions.list", "params": {}})

    (item,) = result["result"]["extensions"]
    assert item["ready_state"] == "ready"


@pytest.mark.asyncio
async def test_extensions_list_empty_without_registry() -> None:
    runtime = _Runtime(extensions=None, storage=_Storage({}))
    state = SimpleNamespace(runtime=runtime)

    result = await dispatch_rpc(state, {"method": "extensions.list", "params": {}})

    assert result == {"ok": True, "result": {"extensions": []}}


@pytest.mark.asyncio
async def test_extensions_list_rejects_params() -> None:
    state = _state_with_records([])

    result = await dispatch_rpc(state, {"method": "extensions.list", "params": {"name": "x"}})

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_request"


def _schemed_record(name: str = "homeassistant") -> ExtensionRecord:
    declarations = ExtensionDeclarations()
    declarations.settings_schema = parse_settings_fields(
        [
            {
                "key": "url",
                "type": "text",
                "label": "URL",
                "description": "Server URL",
                "default": "http://homeassistant.local:8123",
            },
            {"key": "token", "type": "secret", "label": "Token", "env_key": "HASS_TOKEN"},
        ]
    )
    return ExtensionRecord(
        name=name,
        root_path=Path(f"/bundled/{name}"),
        entry_path=Path(f"/bundled/{name}/__init__.py"),
        status="loaded",
        declarations=declarations,
    )


@pytest.mark.asyncio
async def test_extensions_list_carries_settings_schema_and_secret_state() -> None:
    state = _state_with_records([_schemed_record()])
    state.runtime.storage.credentials["HASS_TOKEN"] = "abc"

    result = await dispatch_rpc(state, {"method": "extensions.list", "params": {}})

    (item,) = result["result"]["extensions"]
    schema = item["settings_schema"]
    assert schema == [
        {
            "key": "url",
            "type": "text",
            "label": "URL",
            "description": "Server URL",
            "required": False,
            "default": "http://homeassistant.local:8123",
        },
        {
            "key": "token",
            "type": "secret",
            "label": "Token",
            "description": None,
            "required": False,
            "default": None,
            "env_key": "HASS_TOKEN",
            "set": True,
        },
    ]


@pytest.mark.asyncio
async def test_extensions_list_secret_set_flag_flips_with_resolver() -> None:
    state = _state_with_records([_schemed_record()])

    result = await dispatch_rpc(state, {"method": "extensions.list", "params": {}})
    (item,) = result["result"]["extensions"]
    secret_field = item["settings_schema"][1]
    assert secret_field["set"] is False


@pytest.mark.asyncio
async def test_extensions_list_no_schema_for_unloaded_record() -> None:
    record = _schemed_record()
    record.status = "failed"
    state = _state_with_records([record])

    result = await dispatch_rpc(state, {"method": "extensions.list", "params": {}})
    (item,) = result["result"]["extensions"]
    assert item["settings_schema"] is None


@pytest.mark.asyncio
async def test_set_secret_writes_and_reloads() -> None:
    state = _state_with_records([_schemed_record()])

    result = await dispatch_rpc(
        state,
        {
            "method": "extensions.set_secret",
            "params": {"name": "homeassistant", "key": "token", "value": "s3cret"},
        },
    )

    assert result["ok"] is True
    assert result["result"] == {"name": "homeassistant", "key": "token", "set": True}
    assert state.runtime.storage.credentials["HASS_TOKEN"] == "s3cret"
    assert state.runtime.reloaded == 1


@pytest.mark.asyncio
async def test_set_secret_empty_value_clears() -> None:
    state = _state_with_records([_schemed_record()])
    state.runtime.storage.credentials["HASS_TOKEN"] = "old"

    result = await dispatch_rpc(
        state,
        {
            "method": "extensions.set_secret",
            "params": {"name": "homeassistant", "key": "token", "value": ""},
        },
    )

    assert result["result"] == {"name": "homeassistant", "key": "token", "set": False}
    assert "HASS_TOKEN" in state.runtime.storage.removed
    assert "HASS_TOKEN" not in state.runtime.storage.credentials
    assert state.runtime.reloaded == 1


@pytest.mark.asyncio
async def test_set_secret_does_not_log_the_value(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    state = _state_with_records([_schemed_record()])
    caplog.set_level(logging.DEBUG)

    await dispatch_rpc(
        state,
        {
            "method": "extensions.set_secret",
            "params": {"name": "homeassistant", "key": "token", "value": "super-secret-value"},
        },
    )

    assert all("super-secret-value" not in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "key", "value"),
    [
        ("nope", "token", "x"),  # unknown extension
        ("homeassistant", "missing", "x"),  # unknown field
        ("homeassistant", "url", "x"),  # field is not a secret
    ],
)
async def test_set_secret_error_cases_return_invalid_request(
    name: str, key: str, value: str
) -> None:
    state = _state_with_records([_schemed_record()])

    result = await dispatch_rpc(
        state,
        {"method": "extensions.set_secret", "params": {"name": name, "key": key, "value": value}},
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_set_secret_not_loaded_returns_invalid_request() -> None:
    record = _schemed_record()
    record.status = "failed"
    state = _state_with_records([record])

    result = await dispatch_rpc(
        state,
        {
            "method": "extensions.set_secret",
            "params": {"name": "homeassistant", "key": "token", "value": "x"},
        },
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_set_secret_no_schema_returns_invalid_request() -> None:
    record = ExtensionRecord(
        name="plain",
        root_path=Path("/ext/plain.py"),
        entry_path=Path("/ext/plain.py"),
        status="loaded",
    )
    state = _state_with_records([record])

    result = await dispatch_rpc(
        state,
        {
            "method": "extensions.set_secret",
            "params": {"name": "plain", "key": "token", "value": "x"},
        },
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_settings_update_extensions_disable_applies_live(tmp_path: Path) -> None:
    # Disabling takes the surgical live-disable path: the section persists, the
    # disabled name is applied live, and no full reload runs.
    state = make_state(tmp_path, StubAdapter())

    result = await dispatch_rpc(
        state,
        {
            "method": "settings.update",
            "params": {
                "extensions": {
                    "disabled": ["legacy"],
                    "config": {"guard_bash": {"deny": ["rm -rf"]}},
                }
            },
        },
    )

    assert result["ok"] is True
    assert state.runtime.extension_disabled_changes == [{"legacy"}]
    assert state.runtime.extension_reload_count == 0
    assert state.runtime.storage.load_extensions_settings() == {
        "disabled": ["legacy"],
        "config": {"guard_bash": {"deny": ["rm -rf"]}},
    }


@pytest.mark.asyncio
async def test_settings_update_extensions_enable_reloads_layer(tmp_path: Path) -> None:
    # Enabling (removing a name from the persisted disabled set) rebuilds the whole
    # extension layer live — no restart signal anymore.
    state = make_state(tmp_path, StubAdapter())
    state.runtime.storage.update_settings_sections(
        {"extensions": {"disabled": ["legacy"], "config": {}}}
    )

    result = await dispatch_rpc(
        state,
        {"method": "settings.update", "params": {"extensions": {"disabled": [], "config": {}}}},
    )

    assert result["ok"] is True
    assert state.runtime.extension_reload_count == 1
    assert state.runtime.extension_disabled_changes == []
    assert state.runtime.storage.load_extensions_settings() == {"disabled": [], "config": {}}


@pytest.mark.asyncio
async def test_settings_update_without_extensions_touches_no_extension_seam(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())

    result = await dispatch_rpc(
        state,
        {"method": "settings.update", "params": {"appearance": {"language": "en"}}},
    )

    assert result["ok"] is True
    assert state.runtime.extension_reload_count == 0
    assert state.runtime.extension_disabled_changes == []


@pytest.mark.asyncio
async def test_reload_extensions_rejects_params() -> None:
    state = _state_with_records([_loaded_record()])

    result = await dispatch_rpc(
        state, {"method": "extensions.reload", "params": {"name": "guard_bash"}}
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_reload_extensions_drives_runtime_and_returns_list_shape(tmp_path: Path) -> None:
    # The handler awaits the runtime rebuild, then returns the same payload shape as
    # extensions.list (one entry per discovered record).
    state = make_state(tmp_path, StubAdapter())
    state.runtime.extensions = _Registry([_loaded_record()])

    result = await dispatch_rpc(state, {"method": "extensions.reload", "params": {}})

    assert result["ok"] is True
    assert state.runtime.extension_reload_count == 1
    names = [extension["name"] for extension in result["result"]["extensions"]]
    assert names == ["guard_bash"]
