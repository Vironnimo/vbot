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
    CommandDeclaration,
    ExtensionDeclarations,
    ExtensionManifest,
    ExtensionRecord,
    ExtensionRegistrationIdentity,
    RecallBackendDeclaration,
    ToolDeclaration,
)
from core.extensions.interactions import InteractionHandlerDeclaration
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

    def load_environment(self) -> dict[str, str]:
        return dict(self.credentials)

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


class _CommandDispatcher:
    def __init__(self, owners: dict[str, str] | None = None) -> None:
        self._owners = owners or {}

    def extension_command_owner(self, name: str) -> str | None:
        return self._owners.get(name)


class _Runtime(SimpleNamespace):
    """Runtime stub exposing the credential seam the handlers touch."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.reloaded = 0

    def resolve_environment_credential(self, key: str) -> str:
        value: str = self.storage.credentials.get(key, "")
        return value

    def reload_environment_credentials(self) -> None:
        self.reloaded += 1


def _state_with_records(
    records: list[ExtensionRecord],
    config: dict[str, dict[str, Any]] | None = None,
    tools: _ToolRegistry | None = None,
    command_dispatcher: _CommandDispatcher | None = None,
) -> SimpleNamespace:
    runtime = _Runtime(
        extensions=_Registry(records),
        storage=_Storage(config or {}),
        tools=tools if tools is not None else _ToolRegistry(),
        command_dispatcher=command_dispatcher or _CommandDispatcher(),
    )
    return SimpleNamespace(runtime=runtime)


def _loaded_record() -> ExtensionRecord:
    declarations = ExtensionDeclarations()
    declarations.hooks["tool_call"].append(_noop_handler)
    declarations.hooks["run_end"].append(_noop_handler)
    declarations.tools.append(
        ToolDeclaration("word_count", "Count words", {"type": "object"}, _noop_handler)
    )
    declarations.commands.append(CommandDeclaration("workflow", "Run workflow", _noop_handler))
    declarations.recall_backends.append(RecallBackendDeclaration("my_backend", _noop_handler))
    declarations.interaction_handlers.append(InteractionHandlerDeclaration("chk", _noop_handler))
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
        command_dispatcher=_CommandDispatcher({"workflow": "guard_bash"}),
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
            "commands": [{"name": "workflow", "registered": True}],
            "recall_backends": ["my_backend"],
            "interaction_handlers": ["chk"],
            "startup": True,
            "shutdown": False,
        },
    }
    assert failed_item["status"] == "failed"
    assert failed_item["error"] == "import failed: boom"
    assert failed_item["config"] == {}
    assert failed_item["capabilities"]["tools"] == []
    assert failed_item["capabilities"]["commands"] == []
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


@pytest.mark.asyncio
async def test_extension_page_run_rejects_unscoped_or_invalid_cursor_requests() -> None:
    state = _state_with_records([])

    missing_scope = await dispatch_rpc(
        state,
        {
            "method": "extensions.page_run",
            "params": {"name": "alpha", "group_id": "group", "run_id": "run"},
        },
    )
    invalid_cursor = await dispatch_rpc(
        state,
        {
            "method": "extensions.page_run",
            "params": {
                "name": "alpha",
                "page": {"id": "overview", "epoch": "epoch"},
                "group_id": "group",
                "run_id": "run",
                "after_sequence": -1,
            },
        },
    )

    assert missing_scope["ok"] is False
    assert invalid_cursor["ok"] is False
    assert invalid_cursor["error"]["code"] == "invalid_request"


class _PageRegistry:
    def __init__(self, host: Any) -> None:
        self.identity = ExtensionRegistrationIdentity("alpha", "epoch-a")
        self._host = host
        self.current = True

    def is_registration_current(self, identity: Any) -> bool:
        return self.current and identity == self.identity

    def page_declarations(self) -> list[tuple[Any, Any, Path]]:
        return [(self.identity, SimpleNamespace(page_id="main"), Path("/page/index.html"))]

    def host_for(self, identity: Any) -> Any:
        if not self.is_registration_current(identity):
            raise ValueError("stale owner")
        return self._host


class _ProjectedMessage:
    def __init__(self, role: str, payload: JsonObject) -> None:
        self.role = role
        self._payload = payload

    def to_dict(self) -> JsonObject:
        return dict(self._payload)


class _HistoryDelivery:
    def project_message(self, message: JsonObject) -> JsonObject:
        if message.get("role") != "assistant":
            return dict(message)
        return {
            **message,
            "content": "[report](/api/files/capability.signature)",
        }

    def resolve_token(self, token: str) -> object | None:
        return object() if token == "capability.signature" else None


@pytest.mark.asyncio
async def test_extension_page_history_projects_only_bound_visible_history() -> None:
    snapshot = SimpleNamespace(
        page=SimpleNamespace(
            messages=(
                _ProjectedMessage(
                    "assistant",
                    {
                        "role": "assistant",
                        "content": "raw",
                        "output_files": [{"path": "/private/report.txt"}],
                        "reasoning_meta": {"secret": True},
                    },
                ),
                _ProjectedMessage("note", {"role": "note", "content": "private"}),
            ),
            has_more=False,
            before_cursor=None,
        ),
        session_usage={"input_tokens": 2},
        context_messages=(),
    )

    class Groups:
        async def inspect(self, group_id: str, participant_id: str, query: JsonObject) -> Any:
            assert (group_id, participant_id, query) == ("group-a", "participant-a", {"limit": 1})
            return snapshot

    registry = _PageRegistry(SimpleNamespace(temporary_agents=Groups()))
    state = _state_with_records([])
    state.runtime.extensions = registry
    state.file_delivery = _HistoryDelivery()

    result = await dispatch_rpc(
        state,
        {
            "method": "extensions.page_history",
            "params": {
                "name": "alpha",
                "page": {"id": "main", "epoch": "epoch-a"},
                "group_id": "group-a",
                "participant_id": "participant-a",
                "query": {"limit": 1},
            },
        },
    )

    assert result["ok"] is True
    assert result["result"] == {
        "messages": [{"role": "assistant", "content": "[report](/api/files/capability.signature)"}],
        "has_more": False,
        "session_usage": {"input_tokens": 2},
        "context_usage": None,
        "file_urls": ["/api/files/capability.signature"],
    }


@pytest.mark.asyncio
async def test_extension_page_history_rejects_stale_page_and_foreign_participant() -> None:
    class Groups:
        async def inspect(self, group_id: str, participant_id: str, query: JsonObject) -> Any:
            if participant_id != "participant-a":
                raise ValueError("participant is not owned")
            return SimpleNamespace(
                page=SimpleNamespace(messages=(), has_more=False, before_cursor=None),
                session_usage={},
            )

    registry = _PageRegistry(SimpleNamespace(temporary_agents=Groups()))
    state = _state_with_records([])
    state.runtime.extensions = registry
    state.file_delivery = _HistoryDelivery()
    params: JsonObject = {
        "name": "alpha",
        "page": {"id": "main", "epoch": "epoch-a"},
        "group_id": "group-a",
        "participant_id": "foreign",
        "query": {},
    }

    foreign = await dispatch_rpc(state, {"method": "extensions.page_history", "params": params})
    registry.current = False
    stale = await dispatch_rpc(
        state,
        {
            "method": "extensions.page_history",
            "params": {**params, "participant_id": "participant-a"},
        },
    )

    assert foreign["ok"] is False
    assert stale["ok"] is False


@pytest.mark.asyncio
async def test_extension_page_operation_rechecks_registration_after_await() -> None:
    registry = _PageRegistry(SimpleNamespace(temporary_agents=None))

    class Management:
        async def invoke(self, operation: str, arguments: JsonObject) -> JsonObject:
            assert (operation, arguments) == ("refresh", {})
            registry.current = False
            return {"stale": True}

    registry.management = lambda name: Management()  # type: ignore[attr-defined]
    state = _state_with_records([])
    state.runtime.extensions = registry

    result = await dispatch_rpc(
        state,
        {
            "method": "extensions.operation",
            "params": {
                "name": "alpha",
                "operation": "refresh",
                "arguments": {},
                "page": {"id": "main", "epoch": "epoch-a"},
            },
        },
    )

    assert result["ok"] is False


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
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "vbot.server.rpc.extensions"
    ]
    assert len(messages) == 1
    assert "extension=homeassistant" in messages[0]
    assert "field=token" in messages[0]


@pytest.mark.asyncio
async def test_set_secret_same_value_does_not_log(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    state = _state_with_records([_schemed_record()])
    state.runtime.storage.credentials["HASS_TOKEN"] = "unchanged"
    caplog.set_level(logging.INFO, logger="vbot.server.rpc.extensions")

    await dispatch_rpc(
        state,
        {
            "method": "extensions.set_secret",
            "params": {"name": "homeassistant", "key": "token", "value": "unchanged"},
        },
    )

    assert not [record for record in caplog.records if record.name == "vbot.server.rpc.extensions"]


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
    assert state.event_bus.events[-1]["payload"] == {"kind": "commands"}
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
    assert state.event_bus.events[-1]["payload"] == {"kind": "commands"}
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
    assert [event["payload"] for event in state.event_bus.events[-2:]] == [
        {"kind": "commands"},
        {"kind": "extensions"},
    ]
    names = [extension["name"] for extension in result["result"]["extensions"]]
    assert names == ["guard_bash"]


def test_temporary_history_context_uses_canonical_tail_outside_visible_page():
    from core.chat import ChatMessage
    from server.rpc.extensions_methods import _temporary_history_projection

    snapshot = SimpleNamespace(
        page=SimpleNamespace(messages=(), has_more=True, before_cursor="older"),
        session_usage={"input_tokens": 5000},
        context_messages=(
            ChatMessage(
                id="context-anchor",
                timestamp="2026-09-08T09:00:00+00:00",
                role="assistant",
                content="measured",
                usage={"input_tokens": 120, "output_tokens": 30},
            ),
        ),
    )
    result = _temporary_history_projection(snapshot, None)
    assert result["context_usage"] == {
        "tokens": 150,
        "estimated": False,
        "provider_input_tokens": 120,
        "provider_output_tokens": 30,
    }
    assert result["messages"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["expired", "domain", "unexpected", "cancelled"])
async def test_extension_operation_error_boundary(kind: str) -> None:
    import asyncio

    from core.extensions.extensions import SessionCapabilityExpiredError
    from core.utils.errors import ConfigError

    errors: dict[str, BaseException] = {
        "expired": SessionCapabilityExpiredError("test-owned-expiry"),
        "domain": ConfigError("test-owned-domain"),
        "unexpected": RuntimeError("test-owned-bug"),
        "cancelled": asyncio.CancelledError(),
    }
    error = errors[kind]

    async def invoke(_operation: str, _arguments: JsonObject) -> JsonObject:
        raise error

    registry = SimpleNamespace(management=lambda _name: SimpleNamespace(invoke=invoke))
    state = SimpleNamespace(runtime=SimpleNamespace(extensions=registry))
    request = {"method": "extensions.operation", "params": {"name": "test", "operation": "run"}}
    if kind in {"unexpected", "cancelled"}:
        with pytest.raises(type(error)):
            await dispatch_rpc(state, request)
    else:
        result = await dispatch_rpc(state, request)
        assert result["ok"] is False
        assert result["error"]["code"] == (
            "session_capability_expired" if kind == "expired" else "domain_error"
        )
