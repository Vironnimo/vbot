"""Explicit Extension services reuse the public application contracts."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.extensions import ExtensionRegistrationIdentity
from core.extensions.extensions import ExtensionDeclarations, ExtensionRecord, ExtensionRegistry
from core.extensions.services import ExtensionServiceCatalog, ExtensionServiceError
from core.extensions.settings_schema import parse_settings_fields
from core.runs import RunKind
from core.runtime._shell_review_context import current_input_authority
from core.tools import ToolContext, tool_success
from server.access import Principal, bound_principal, current_principal
from server.extension_services import (
    SERVICE_DEFINITIONS,
    bind_application_services,
    is_extension_service_call,
)
from server.rpc._mutations import serialized_mutation, serialized_read
from server.rpc.dispatcher import RpcMethodHandler, dispatch_method
from server.rpc.errors import RpcError
from server.rpc.event_bridge import publish_resource_changed
from server.rpc.methods import METHODS
from tests.core.extensions.extension_test_support import write_extension
from tests.server.rpc_test_support import StubAdapter, make_state


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_service_binding_reuses_handlers_errors_and_clears_request_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, asynchronous: bool
) -> None:
    calls = []

    async def handler(_state: Any, params: dict[str, Any]) -> dict[str, Any]:
        calls.append((params, current_principal()))
        if params.get("id") == "refused":
            raise RpcError("test_conflict", "test-owned refusal", data={"revision": 3})
        return {"result": params}

    catalog = ExtensionServiceCatalog()
    state = SimpleNamespace(runtime=SimpleNamespace(extension_services=catalog))
    handlers: dict[str, RpcMethodHandler] = {name: handler for name, _, _, _ in SERVICE_DEFINITIONS}
    handlers["agent.list"] = serialized_read(handler, lock_attribute="_publication_lock")
    close = bind_application_services(state, handlers)
    services = catalog.for_owner(ExtensionRegistrationIdentity("fixture", "one"), lambda: None)
    with bound_principal(Principal("host")):
        assert await services.query("agent.list") == {"result": {}}
        assert await services.execute("agent.update", {"id": "fixture"}) == {
            "result": {"id": "fixture"}
        }
        assert current_principal() is not None
    assert calls == [({}, None), ({"id": "fixture"}, None)]

    # An exported service clears request authority while keeping the live read
    # lease needed by nested canonical queries during lifecycle publication.
    write_extension(
        tmp_path,
        "nested",
        "probe = None\n"
        + (
            "async def read(params): return await probe(params)\n"
            if asynchronous
            else "def read(params): return probe(params)\n"
        )
        + "def register(api):\n"
        "    api.register_service('read', 'query', {'type': 'object'}, read)\n",
    )
    registry = await ExtensionRegistry.aload(tmp_path)
    catalog.install_exports(registry)
    contexts = []

    def inspect_export(_params: dict[str, Any]) -> Awaitable[dict[str, Any]]:
        contexts.append((current_principal(), is_extension_service_call()))
        return services.query("agent.list")

    monkeypatch.setattr(sys.modules["vbot_ext.nested"], "probe", inspect_export)

    async def publish(_state: Any, _params: dict[str, Any]) -> dict[str, Any]:
        return await services.query("extension:nested:read")

    enclosing = serialized_mutation(publish, lock_attribute="_publication_lock")
    with bound_principal(Principal("host")):
        assert await enclosing(state, {}) == {"result": {}}
        assert current_principal() is not None
        assert is_extension_service_call() is False
    assert contexts == [(None, True)]
    with pytest.raises(ExtensionServiceError) as refused:
        await services.execute("agent.update", {"id": "refused"})
    assert refused.value.code == "test_conflict"
    assert refused.value.data == {"revision": 3}
    for private in (
        "settings.get_raw",
        "settings.set_service_key",
        "access.devices",
        "permissions.resolve",
        "session.set_execution",
        "chat.queue_update",
        "chat.cancel_tool_call",
    ):
        with pytest.raises(ExtensionServiceError) as unknown:
            await services.execute(private)
        assert unknown.value.code == "service_not_found"
    close()
    assert services.describe()["available"] is True  # Exported services outlive the app binding.
    await catalog.release_owner(registry.registration_identity("nested"))
    sys.modules.pop("vbot_ext.nested", None)
    assert services.describe()["available"] is False


@pytest.mark.asyncio
async def test_services_use_canonical_session_preferences_and_resource_publication(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    close = bind_application_services(state, METHODS)
    catalog = state.runtime.extension_services
    identity = ExtensionRegistrationIdentity("fixture", "one")
    services = catalog.for_owner(identity, lambda: None)
    observed: list[dict[str, Any]] = []
    changed = asyncio.Event()

    async def observe(event: dict[str, Any]) -> None:
        observed.append(event)
        changed.set()

    services.subscribe(["sessions"], observe)
    address = {"agent_id": "coder", "session_id": "extension-created"}
    try:
        created = await services.execute("session.create", address)
        assert created["session_id"] == address["session_id"]
        await services.execute("session.rename", {**address, "title": "Test session"})
        await services.execute("session.set_pinned", {**address, "pinned": True})
        session = (await services.query("session.get", address))["session"]
        assert session["title"] == "Test session"
        assert session["pinned_at"] is not None
        await changed.wait()
        assert observed[-1]["kind"] == "sessions"
        changed.clear()
        # Ordinary public mutation publication uses the same observer path.
        publish_resource_changed(state, "sessions", scope=address)
        await changed.wait()
        assert observed[-1]["scope"] == address
    finally:
        await catalog.release_owner(identity)
        close()


@pytest.mark.asyncio
async def test_observer_actions_have_independent_principal_and_publication_context() -> None:
    catalog = ExtensionServiceCatalog()
    state = SimpleNamespace(runtime=SimpleNamespace(extension_services=catalog))
    entered, finished = asyncio.Event(), asyncio.Event()
    principals = []
    mutations = []
    errors = []

    async def update(_state: Any, params: dict[str, Any]) -> dict[str, Any]:
        mutations.append(params)
        return {"committed": True}

    mutation = serialized_mutation(update, lock_attribute="_publication_lock")
    close = bind_application_services(
        state, {name: mutation for name, _, _, _ in SERVICE_DEFINITIONS}
    )
    identity = ExtensionRegistrationIdentity("observer", "one")
    services = catalog.for_owner(identity, lambda: None)

    async def observe(_event: dict[str, Any]) -> None:
        principals.append(current_principal())
        entered.set()
        try:
            assert await services.execute("agent.update", {"id": "observer"}) == {"committed": True}
        except Exception as error:
            errors.append(error)
        finally:
            finished.set()

    services.subscribe(["sessions"], observe)

    async def publish(_state: Any, _params: dict[str, Any]) -> dict[str, Any]:
        catalog.publish("sessions")
        # The observer attempts its own mutation while this publication holds
        # the lock. It must wait independently, never inherit our read lease.
        await entered.wait()
        assert mutations == []
        return {}

    publish_mutation = serialized_mutation(publish, lock_attribute="_publication_lock")
    try:
        with bound_principal(Principal("host")):
            await publish_mutation(state, {})
            assert current_principal() is not None
        await finished.wait()
        assert principals == [None]
        assert errors == []
        assert mutations == [{"id": "observer"}]
    finally:
        await catalog.release_owner(identity)
        close()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["chat.send", "chat.stream"])
async def test_extension_chat_uses_system_provenance_without_human_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    adapter = StubAdapter(
        responses=[
            {
                "content": "",
                "tool_calls": [
                    {"id": "inspect-authority", "name": "inspect_authority", "arguments": {}}
                ],
            },
            {"content": "Done", "tool_calls": None},
        ]
    )
    state = make_state(tmp_path, adapter)
    close = bind_application_services(state, METHODS)
    catalog = state.runtime.extension_services
    services = catalog.for_owner(ExtensionRegistrationIdentity("fixture", "one"), lambda: None)
    inspected = []

    async def inspect_authority(context: ToolContext, _arguments: dict[str, Any]) -> dict[str, Any]:
        authority = await current_input_authority(
            state.runtime.chat_sessions, context, run=state.chat_runs.get(context.run_id)
        )
        inspected.append((context.run_kind, context.direct_user_input, authority))
        return tool_success({"authority": authority})

    state.runtime.tools.register(
        "inspect_authority", "Inspect test authority.", {"type": "object"}, inspect_authority
    )
    address = {"agent_id": "coder", "session_id": "integration-input"}
    try:
        await services.execute("session.create", address)
        for content, extra in (("/new", {}), ("Input", {"input_id": "user-receipt"})):
            with pytest.raises(ExtensionServiceError) as refused:
                await services.execute(method, {**address, "content": content, **extra})
            assert refused.value.code == "invalid_request"
        result = await services.execute(method, {**address, "content": "Inspect this input"})
        run = state.chat_runs.get(result["run_id"])
        await run.wait()
        assert run.run_kind == RunKind.SYSTEM
        assert run.accessor_input_id is None
        assert inspected == [(RunKind.SYSTEM, False, "none")]
    finally:
        await state.chat_runs.aclose()
        close()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["settings.patch", "settings.update", "extensions.reload"])
async def test_lifecycle_callbacks_query_canonical_services_inside_settings_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    state = make_state(tmp_path, StubAdapter())
    registry = ExtensionRegistry()
    declarations = ExtensionDeclarations()
    declarations.settings_schema = parse_settings_fields(
        [{"key": "mode", "type": "text", "label": "Mode"}]
    )
    registry._records.append(  # noqa: SLF001 - install the fixture's completed registration
        ExtensionRecord(
            name="fixture",
            root_path=tmp_path,
            entry_path=tmp_path / "fixture.py",
            status="loaded",
            declarations=declarations,
        )
    )
    state.runtime.extensions = registry
    close = bind_application_services(state, METHODS)
    services = state.runtime.extension_services.for_owner(
        registry.registration_identity("fixture"), lambda: None
    )
    observed: list[dict[str, Any]] = []
    model = state.runtime.models._models["openai"][0]  # noqa: SLF001

    async def read_services() -> None:
        observed.extend(
            await asyncio.gather(
                services.query("settings.values"),
                services.query("settings.catalog", {"prefix": "defaults.agent"}),
                services.query(
                    "settings.get_path", {"path": "defaults.agent.model", "allow_missing": True}
                ),
                services.query("model.list"),
                services.query("model.get", {"model": f"openai/{model.model_id}"}),
            )
        )

    async def config_changed(_previous: dict[str, Any], _current: dict[str, Any]) -> None:
        await read_services()

    declarations.startup.append(read_services)
    declarations.config_change.append(config_changed)
    # Keep the real registry lifecycle dispatch while replacing filesystem scan.
    monkeypatch.setattr(state.runtime, "reload_extensions", registry.fire_startup)
    params = (
        {
            "operations": [
                {"op": "set", "path": 'extensions.config["fixture"]["mode"]', "value": "changed"}
            ]
        }
        if method == "settings.patch"
        else {"extensions": {"config": {"fixture": {"mode": "changed"}}}}
        if method == "settings.update"
        else {}
    )
    try:
        async with asyncio.timeout(2):
            await dispatch_method(state, method, params, METHODS)
        assert len(observed) == 5
        assert not state._settings_mutation_lock.locked()
    finally:
        close()
