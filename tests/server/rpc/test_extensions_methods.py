"""Extension RPCs: the catalog, reload, secret fields, operations and page-scoped Run access.

The ``settings.update`` ``extensions`` section tests at the end route the
disabled-set delta to a live reload or live disable.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, override
from unittest.mock import Mock

import pytest

from core.chat import ChatMessage
from core.chat.messages import ModelFallback
from core.extensions.extensions import (
    CommandDeclaration,
    ExtensionDeclarations,
    ExtensionManifest,
    ExtensionRecord,
    ExtensionRegistrationIdentity,
    ExtensionStatus,
    ExtensionUnavailableError,
    RecallBackendDeclaration,
    SessionCapabilityExpiredError,
    ToolDeclaration,
)
from core.extensions.interactions import InteractionHandlerDeclaration
from core.extensions.settings_schema import parse_settings_fields
from core.runs import Run
from core.utils.errors import ConfigError
from server.events import ServerEventBus
from tests.server.rpc_test_support import (
    JsonObject,
    call,
    resource_changes,
    rpc_error,
    rpc_result,
)

_PAGE = {"id": "main", "epoch": "epoch-a"}
_UNAVAILABLE = "Extension page is unavailable; refresh the page"


def _noop_handler(*_args: Any, **_kwargs: Any) -> None:
    return None


class _Registry:
    """Minimal stand-in for ``ExtensionRegistry`` exposing ``records()``."""

    def __init__(self, records: list[ExtensionRecord]) -> None:
        self._records = records

    def records(self) -> list[ExtensionRecord]:
        return list(self._records)


class _Storage:
    def __init__(self, config: dict[str, dict[str, Any]], disabled: list[str]) -> None:
        self._config = config
        self._disabled = disabled
        self.credentials: dict[str, str] = {}

    def load_extensions_settings(self) -> JsonObject:
        return {"disabled": self._disabled, "config": self._config}

    def load_environment(self) -> dict[str, str]:
        return dict(self.credentials)

    def set_data_dir_credential(self, key: str, value: str) -> None:
        self.credentials[key] = value

    def remove_data_dir_credential(self, key: str) -> bool:
        return self.credentials.pop(key, None) is not None


class _ToolRegistry:
    """Minimal ``ToolRegistry`` stand-in with per-name readiness predicates.

    ``ready`` maps a tool name to its predicate; ``None`` is always ready. A name
    never added raises on ``get``: the "declared but unregistered" case.
    """

    def __init__(self, ready: dict[str, Any] | None = None) -> None:
        self._ready = ready or {}

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
    """Runtime stub: live credential resolution sees saved values only after a reload."""

    def __init__(self, credentials: dict[str, str] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.storage.credentials.update(credentials or {})
        self._live: dict[str, str] = dict(self.storage.credentials)
        self.extension_reloads = 0
        self.reload_error: Exception | None = None

    def resolve_environment_credential(self, key: str) -> str:
        return self._live.get(key, "")

    def reload_environment_credentials(self) -> None:
        self._live = dict(self.storage.credentials)

    async def reload_extensions(self) -> None:
        if self.reload_error is not None:
            raise self.reload_error
        self.extension_reloads += 1


def _state_with_records(
    records: list[ExtensionRecord],
    *,
    config: dict[str, dict[str, Any]] | None = None,
    disabled: list[str] | None = None,
    tools: _ToolRegistry | None = None,
    command_dispatcher: _CommandDispatcher | None = None,
    credentials: dict[str, str] | None = None,
) -> SimpleNamespace:
    runtime = _Runtime(
        credentials,
        extensions=_Registry(records),
        storage=_Storage(config or {}, disabled or []),
        tools=tools if tools is not None else _ToolRegistry(),
    )
    return SimpleNamespace(
        runtime=runtime,
        event_bus=ServerEventBus(),
        command_dispatcher=command_dispatcher or _CommandDispatcher(),
    )


def _record(name: str, status: ExtensionStatus = "loaded", **fields: Any) -> ExtensionRecord:
    return ExtensionRecord(
        name=name,
        root_path=Path(f"/ext/{name}"),
        entry_path=Path(f"/ext/{name}/__init__.py"),
        status=status,
        **fields,
    )


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
    return _record(
        "guard_bash",
        manifest=ExtensionManifest(
            version="1.2.0",
            description="Guards dangerous bash",
            api_version=1,
            display_name="Bash Guard",
        ),
        declarations=declarations,
    )


def _schemed_record(
    name: str = "homeassistant", status: ExtensionStatus = "loaded"
) -> ExtensionRecord:
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
    return _record(name, status, declarations=declarations)


def _tooled_record() -> ExtensionRecord:
    declarations = ExtensionDeclarations()
    declarations.tools.append(
        ToolDeclaration("ha_call_service", "Call a service", {"type": "object"}, _noop_handler)
    )
    return _record("homeassistant", declarations=declarations)


# ---------------------------------------------------------------------------
# extensions.list / extensions.reload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extensions_list_projects_every_record_state() -> None:
    # A failed record keeps its declared schema hidden: only a loaded one has a form.
    failed = _schemed_record("broken", status="failed")
    failed.error = "import failed: boom"
    overridden_by = str(Path("/data/extensions/homeassistant/__init__.py"))
    state = _state_with_records(
        [
            _loaded_record(),
            failed,
            _record("off", status="disabled"),
            _record("homeassistant", status="overridden", overridden_by=overridden_by),
        ],
        # The saved section keeps its own order and entries without a record.
        config={"guard_bash": {"deny": ["rm -rf"]}, "removed": {"level": 1}},
        disabled=["removed", "off"],
        tools=_ToolRegistry({"word_count": None}),
        command_dispatcher=_CommandDispatcher({"workflow": "guard_bash"}),
    )

    result = await rpc_result(state, "extensions.list")

    loaded, failed_item, disabled_item, overridden_item = result["extensions"]
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
    assert failed_item["settings_schema"] is None
    assert failed_item["capabilities"]["tools"] == []
    assert failed_item["capabilities"]["commands"] == []
    assert (disabled_item["status"], disabled_item["disabled"]) == ("disabled", True)
    assert overridden_item["status"] == "overridden"
    assert overridden_item["disabled"] is False
    assert overridden_item["overridden_by"] == overridden_by
    assert result["settings"] == {
        "disabled": ["removed", "off"],
        "config": {"guard_bash": {"deny": ["rm -rf"]}, "removed": {"level": 1}},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tools", "record", "ready_state", "tool_states"),
    [
        (_ToolRegistry({"ha_call_service": lambda: False}), _tooled_record(), "waiting", [False]),
        (_ToolRegistry({"ha_call_service": lambda: True}), _tooled_record(), "ready", [True]),
        # A declared name that never registered (e.g. skipped on a collision) is not ready.
        (_ToolRegistry(), _tooled_record(), "waiting", [False]),
        (_ToolRegistry(), _record("hooks_only"), "ready", []),
    ],
    ids=["tool-not-ready", "tool-ready", "tool-unregistered", "no-tools"],
)
async def test_extension_ready_state_follows_declared_tool_readiness(
    tools: _ToolRegistry, record: ExtensionRecord, ready_state: str, tool_states: list[bool]
) -> None:
    result = await rpc_result(_state_with_records([record], tools=tools), "extensions.list")

    [item] = result["extensions"]
    assert item["ready_state"] == ready_state
    assert [tool["ready"] for tool in item["capabilities"]["tools"]] == tool_states


@pytest.mark.asyncio
async def test_extensions_list_is_empty_without_registry() -> None:
    state = _state_with_records([])
    state.runtime.extensions = None

    assert await rpc_result(state, "extensions.list") == {
        "extensions": [],
        "settings": {"disabled": [], "config": {}},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("credential_set", [True, False])
async def test_extensions_list_carries_settings_schema_and_secret_state(
    credential_set: bool,
) -> None:
    state = _state_with_records(
        [_schemed_record()], credentials={"HASS_TOKEN": "abc"} if credential_set else None
    )

    result = await rpc_result(state, "extensions.list")

    [item] = result["extensions"]
    assert item["settings_schema"] == [
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
            "set": credential_set,
        },
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("closing", [False, True])
async def test_extensions_reload_rebuilds_then_returns_the_catalog(closing: bool) -> None:
    state = _state_with_records([_loaded_record()])
    if closing:
        state.runtime.reload_error = ExtensionUnavailableError("Extension runtime is closing")

    response = await call(state, "extensions.reload")

    if closing:
        assert response["error"]["code"] == "domain_error"
        assert resource_changes(state) == []
        return
    assert state.runtime.extension_reloads == 1
    assert [item["name"] for item in response["result"]["extensions"]] == ["guard_bash"]
    # The rebuild also rescans Extension Skill folders.
    assert resource_changes(state) == [
        {"kind": "commands"},
        {"kind": "extensions"},
        {"kind": "skills"},
    ]


# ---------------------------------------------------------------------------
# extensions.set_secret
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("previous", "value", "is_set", "logged"),
    [
        (None, "super-secret-value", True, ["saved"]),
        ("old-secret-value", "", False, ["removed"]),
        # Saving the same value again changes nothing worth logging.
        ("same-secret-value", "same-secret-value", True, []),
    ],
    ids=["save", "clear", "unchanged"],
)
async def test_set_secret_saves_or_clears_the_credential_without_logging_it(
    caplog: pytest.LogCaptureFixture,
    previous: str | None,
    value: str,
    is_set: bool,
    logged: list[str],
) -> None:
    state = _state_with_records(
        [_schemed_record()], credentials={"HASS_TOKEN": previous} if previous else None
    )
    caplog.set_level(logging.DEBUG)

    result = await rpc_result(
        state, "extensions.set_secret", name="homeassistant", key="token", value=value
    )
    listed = await rpc_result(state, "extensions.list")

    assert result == {"name": "homeassistant", "key": "token", "set": is_set}
    assert state.runtime.storage.credentials == ({"HASS_TOKEN": value} if value else {})
    # Live credential resolution sees the change without a restart.
    assert listed["extensions"][0]["settings_schema"][1]["set"] is is_set
    messages = [record.getMessage() for record in caplog.records]
    assert [message for message in messages if message.startswith("Extension secret")] == [
        f"Extension secret {action} (extension=homeassistant field=token)" for action in logged
    ]
    secrets = [secret for secret in (previous, value) if secret]
    assert not [message for message in messages if any(secret in message for secret in secrets)]


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "named"),
    [
        ("extensions.list", {"name": "x"}, "does not accept params"),
        ("extensions.reload", {"name": "guard_bash"}, "does not accept params"),
        (
            "extensions.page_run",
            {"name": "alpha", "group_id": "group", "run_id": "run"},
            "page must be an object",
        ),
        (
            "extensions.page_run",
            {
                "name": "alpha",
                "page": _PAGE,
                "group_id": "group",
                "run_id": "run",
                "after_sequence": -1,
            },
            "after_sequence",
        ),
        (
            "extensions.set_secret",
            {"name": "nope", "key": "token", "value": "x"},
            "unknown extension",
        ),
        (
            "extensions.set_secret",
            {"name": "homeassistant", "key": "missing", "value": "x"},
            "unknown settings field",
        ),
        (
            "extensions.set_secret",
            {"name": "homeassistant", "key": "url", "value": "x"},
            "is not a secret",
        ),
        (
            "extensions.set_secret",
            {"name": "broken", "key": "token", "value": "x"},
            "is not loaded",
        ),
        (
            "extensions.set_secret",
            {"name": "plain", "key": "token", "value": "x"},
            "declares no settings schema",
        ),
    ],
)
async def test_extension_refusals_change_nothing(
    method: str, params: JsonObject, named: str
) -> None:
    state = _state_with_records(
        [_schemed_record(), _schemed_record("broken", status="failed"), _record("plain")]
    )

    error = await rpc_error(state, method, **params)

    assert error["code"] == "invalid_request"
    assert named in error["message"]
    assert state.runtime.storage.credentials == {}
    assert state.runtime.extension_reloads == 0
    assert resource_changes(state) == []


# ---------------------------------------------------------------------------
# extensions.operation
# ---------------------------------------------------------------------------


class _PageRegistry:
    def __init__(self, host: Any) -> None:
        self.identity = ExtensionRegistrationIdentity("alpha", "epoch-a")
        self._host = host
        self.current = True

    def is_registration_current(self, identity: Any) -> bool:
        return self.current and identity == self.identity

    def current_page(self, identity: Any, page_id: str) -> tuple[Any, Path] | None:
        if not self.is_registration_current(identity) or page_id != "main":
            return None
        return SimpleNamespace(page_id="main"), Path("/page/index.html")

    def host_for(self, identity: Any) -> Any:
        if not self.is_registration_current(identity):
            raise ValueError("stale owner")
        return self._host


def _page_state(temporary_agents: Any, registry: _PageRegistry | None = None) -> SimpleNamespace:
    state = _state_with_records([])
    state.runtime.extensions = registry or _PageRegistry(
        SimpleNamespace(temporary_agents=temporary_agents)
    )
    return state


@pytest.mark.asyncio
async def test_extension_page_operation_rechecks_registration_after_await() -> None:
    registry = _PageRegistry(SimpleNamespace(temporary_agents=None))

    class Management:
        async def invoke(self, operation: str, arguments: JsonObject) -> JsonObject:
            assert (operation, arguments) == ("refresh", {})
            registry.current = False
            return {"stale": True}

    registry.management = lambda name: Management()  # type: ignore[attr-defined]

    error = await rpc_error(
        _page_state(None, registry),
        "extensions.operation",
        name="alpha",
        operation="refresh",
        arguments={},
        page=_PAGE,
    )

    assert error == {"code": "invalid_request", "message": _UNAVAILABLE}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["expired", "domain", "unexpected", "cancelled"])
async def test_extension_operation_error_boundary(kind: str) -> None:
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
    if kind in {"unexpected", "cancelled"}:
        # A bug or a cancellation is not an expected domain outcome: it propagates.
        with pytest.raises(type(error)):
            await call(state, "extensions.operation", name="test", operation="run")
        return
    response = await rpc_error(state, "extensions.operation", name="test", operation="run")
    assert response["code"] == (
        "session_capability_expired" if kind == "expired" else "domain_error"
    )


# ---------------------------------------------------------------------------
# extensions.page_history
# ---------------------------------------------------------------------------


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
        return {**message, "content": "[report](/api/files/capability.signature)"}

    def resolve_token(self, token: str) -> object | None:
        return object() if token == "capability.signature" else None


_CONTEXT_USAGE = {
    "tokens": 150,
    "estimated": True,
    "provider_input_tokens": 120,
    "provider_output_tokens": 30,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("before_cursor", [None, "older"])
async def test_extension_page_history_projects_only_bound_visible_history(
    before_cursor: str | None,
) -> None:
    snapshot = SimpleNamespace(
        runs=(),
        generation_id="generation",
        after_cursor="after",
        incremental=False,
        has_newer=False,
        page=SimpleNamespace(
            record_sequences=(0, 1, 2),
            record_run_ids=("run", "run", "run"),
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
                ChatMessage.note("private"),
                ChatMessage(
                    id="fallback-note",
                    timestamp="2026-09-08T09:00:00+00:00",
                    role="note",
                    content="private switch",
                    model_fallback=ModelFallback(from_model="primary", to_model="fallback"),
                ),
            ),
            has_more=before_cursor is not None,
            before_cursor=before_cursor,
        ),
        session_usage={"input_tokens": 5000},
        # Context usage comes from the canonical tail, outside the visible page.
        context_messages=(
            ChatMessage(
                id="context-anchor",
                timestamp="2026-09-08T09:00:00+00:00",
                role="assistant",
                content="measured",
                usage={"input_tokens": 120, "output_tokens": 30, "context_usage": _CONTEXT_USAGE},
            ),
        ),
    )

    class Groups:
        async def inspect(self, group_id: str, participant_id: str, query: JsonObject) -> Any:
            assert (group_id, participant_id, query) == ("group-a", "participant-a", {"limit": 1})
            return snapshot

    state = _page_state(Groups())
    state.file_delivery = _HistoryDelivery()

    result = await rpc_result(
        state,
        "extensions.page_history",
        name="alpha",
        page=_PAGE,
        group_id="group-a",
        participant_id="participant-a",
        query={"limit": 1},
    )

    # Only an older page to read adds the ``next_before`` cursor.
    older = {"next_before": before_cursor} if before_cursor is not None else {}
    assert result == {
        "messages": [
            {
                "role": "assistant",
                "content": "[report](/api/files/capability.signature)",
                "history_sequence": 0,
                "history_run_id": "run",
            },
            # A Model fallback note shows only its display notice.
            {
                "id": "fallback-note",
                "timestamp": "2026-09-08T09:00:00+00:00",
                "role": "model_fallback",
                "from_model": "primary",
                "to_model": "fallback",
                "history_sequence": 2,
                "history_run_id": "run",
            },
        ],
        "runs": [],
        "history_generation": "generation",
        "next_after": "after",
        **older,
        "incremental": False,
        "has_newer": False,
        "has_more": before_cursor is not None,
        "session_usage": {"input_tokens": 5000},
        "context_usage": _CONTEXT_USAGE,
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

    state = _page_state(Groups())
    state.file_delivery = _HistoryDelivery()
    params: JsonObject = {"name": "alpha", "page": _PAGE, "group_id": "group-a", "query": {}}

    foreign = await rpc_error(state, "extensions.page_history", participant_id="foreign", **params)
    state.runtime.extensions.current = False
    stale = await rpc_error(
        state, "extensions.page_history", participant_id="participant-a", **params
    )

    assert foreign == {"code": "invalid_request", "message": "participant is not owned"}
    assert stale == {"code": "invalid_request", "message": _UNAVAILABLE}


# ---------------------------------------------------------------------------
# extensions.page_cancel_tool / extensions.page_run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario", ["active", "foreign", "stale", "reloaded", "finished", "unknown"]
)
async def test_extension_page_cancel_tool_is_owner_scoped_and_call_local(scenario: str) -> None:
    run = Run(run_id="run-a", agent_id="agent-a", session_id="session-a")
    callback = Mock()
    run.begin_tool_call("call-a")
    run.register_tool_cancel("call-a", callback)

    class Groups:
        async def owned_run(self, group_id: str, run_id: str) -> Any:
            assert (group_id, run_id) == ("group-a", "run-a")
            if scenario == "foreign":
                raise ValueError("test-owned foreign Run")
            if scenario == "reloaded":
                registry.current = False
            return SimpleNamespace(run=None if scenario == "finished" else run)

    registry = _PageRegistry(SimpleNamespace(temporary_agents=Groups()))
    registry.current = scenario != "stale"

    response = await call(
        _page_state(None, registry),
        "extensions.page_cancel_tool",
        name="alpha",
        page=_PAGE,
        group_id="group-a",
        run_id="run-a",
        tool_call_id="missing" if scenario == "unknown" else "call-a",
    )

    assert response["ok"] is (scenario == "active")
    assert run.tool_call_cancelled("call-a") is (scenario == "active")
    assert not run.cancel_requested
    assert callback.call_count == (1 if scenario == "active" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["live", "retired"])
async def test_extension_page_run_reports_verified_replay_watermark(scenario: str) -> None:
    run = SimpleNamespace(last_sequence=7)

    class Groups:
        reads = 0

        async def owned_run(self, group_id: str, run_id: str) -> Any:
            assert (group_id, run_id) == ("group-a", "run-a")
            self.reads += 1
            if scenario == "retired":
                registry.current = False
            return SimpleNamespace(
                run=run,
                record=SimpleNamespace(owner=SimpleNamespace(participant_id="participant-a")),
            )

    class Registry(_PageRegistry):
        @override
        def current_page(self, identity: Any, page_id: str) -> tuple[Any, Path] | None:
            # Events the Run emits while ownership is re-verified belong to replay.
            if groups.reads:
                run.last_sequence = 14
            return super().current_page(identity, page_id)

    class Delivery:
        def open_extension_run(self, **kwargs: Any) -> Any:
            assert kwargs["after_sequence"] == 3
            return {"url": "/api/extension-runs/test"}

    groups = Groups()
    registry = Registry(SimpleNamespace(temporary_agents=groups))
    state = _page_state(None, registry)
    state.file_delivery = Delivery()

    response = await call(
        state,
        "extensions.page_run",
        name="alpha",
        page=_PAGE,
        group_id="group-a",
        run_id="run-a",
        after_sequence=3,
    )

    assert groups.reads == 1
    if scenario == "retired":
        assert response["error"]["message"] == _UNAVAILABLE
        return
    assert response == {
        "ok": True,
        "result": {
            "stream": {"url": "/api/extension-runs/test"},
            "replay_through_sequence": 14,
            "participant_id": "participant-a",
        },
    }
