from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from cli.application._service_prerequisites import ActionRequiredError
from core.extensions import ExtensionRegistrationIdentity
from core.extensions.services import ExtensionServiceCatalog, ExtensionServiceError
from server.extension_services import bind_application_services
from server.rpc import managed_service_methods as methods
from server.rpc import settings_methods
from server.rpc._mutations import serialized_mutation, state_lock
from server.rpc.dispatcher import dispatch_method
from server.rpc.methods import METHODS


class ServiceStub:
    def __init__(self):
        self.record = {"state": "idle", "installed": False, "port": 18888}
        self.execute_effect = lambda: None

    def read(self):
        return deepcopy(self.record)

    def save(self, record, **fields):
        record.update(fields)
        self.record = deepcopy(record)

    def status(self, *, active=False):
        return {**self.record, "plan": {"id": "reviewed"}, "active": active}

    def admit(self, action, *, plan_id, allow_prerequisites):
        self.record.update(action=action, state="running")

    def execute(self, action, *, allow_prerequisites):
        self.execute_effect()
        self.record["installed"] = action != "remove"
        return self.url(self.record) if action != "remove" else None

    def finish(self, *, error=None):
        self.record.update(state="failed" if error else "completed", error=error)

    @staticmethod
    def url(record):
        return f"http://127.0.0.1:{record['port']}"


@pytest.fixture
def service_state(tmp_path, monkeypatch):
    raw = {"web_search": {"provider": "brave", "searxng": {"base_url": "https://old.example"}}}
    state = SimpleNamespace(
        runtime=SimpleNamespace(
            storage=SimpleNamespace(data_dir=tmp_path, load_settings=lambda: deepcopy(raw))
        )
    )
    service = ServiceStub()
    monkeypatch.setattr(methods, "owning_installation", lambda _: object())
    monkeypatch.setattr(methods, "ManagedService", lambda _: service)
    patches = []

    async def patch(_state, params):
        patches.append(params)
        for operation in params["operations"]:
            if operation["path"] == "web_search.provider":
                raw["web_search"]["provider"] = operation.get("value")
            else:
                raw["web_search"]["searxng"]["base_url"] = operation.get("value")
        return {}

    monkeypatch.setattr(settings_methods, "_patch_setting_paths", patch)
    return state, service, raw, patches


async def apply(state, action):
    await dispatch_method(
        state,
        "managed_service.apply",
        {"id": "searxng", "action": action, "plan_id": "reviewed"},
        methods.method_handlers(),
    )
    await state._managed_service_operations.task
    return await dispatch_method(
        state, "managed_service.get", {"id": "searxng"}, methods.method_handlers()
    )


@pytest.mark.asyncio
async def test_managed_service_restores_previous_search_only_while_owned(service_state):
    state, service, raw, patches = service_state
    original = deepcopy(raw)
    result = await apply(state, "install")
    assert result["state"] == "completed"
    assert raw["web_search"]["provider"] == "searxng"
    assert raw["web_search"]["searxng"]["base_url"] == service.url(service.record)
    await apply(state, "remove")
    assert raw == original
    await apply(state, "install")
    raw["web_search"]["provider"] = "tavily"
    before = len(patches)
    await apply(state, "remove")
    assert raw["web_search"]["provider"] == "tavily"
    assert len(patches) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["search_failed", "selection_changed"])
async def test_managed_service_does_not_overwrite_search_after_failed_check_or_concurrent_change(
    service_state, cause
):
    state, service, raw, patches = service_state

    def effect():
        if cause == "search_failed":
            raise ActionRequiredError("The test search failed")
        raw["web_search"]["provider"] = "tavily"

    service.execute_effect = effect
    result = await apply(state, "install")
    assert result["state"] == "action_required"
    assert not patches
    assert raw["web_search"]["provider"] == ("brave" if cause == "search_failed" else "tavily")


@pytest.mark.asyncio
async def test_managed_service_development_checkout_is_inspectable_without_host_actions(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(methods, "owning_installation", lambda _: None)
    state = SimpleNamespace(runtime=SimpleNamespace(storage=SimpleNamespace(data_dir=tmp_path)))
    result = await dispatch_method(
        state, "managed_service.get", {"id": "searxng"}, methods.method_handlers()
    )
    assert result["available"] is False
    assert result["plan"] is None
    assert result["actions"] == []


@pytest.mark.asyncio
async def test_managed_service_finishes_admission_after_caller_cancellation(
    service_state, monkeypatch
):
    state, service, raw, _ = service_state
    loop = asyncio.get_running_loop()
    admitted = asyncio.Event()
    admit = service.admit

    def report_admission(*args, **kwargs):
        admit(*args, **kwargs)
        loop.call_soon_threadsafe(admitted.set)

    monkeypatch.setattr(service, "admit", report_admission)
    async with state_lock(state, "_settings_mutation_lock"):
        caller = asyncio.create_task(
            dispatch_method(
                state,
                "managed_service.apply",
                {"id": "searxng", "action": "install", "plan_id": "reviewed"},
                methods.method_handlers(),
            )
        )
        await admitted.wait()
        caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    operation = state._managed_service_operations.task
    assert operation is not None
    await operation
    assert service.record["state"] == "completed"
    assert raw["web_search"]["provider"] == "searxng"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["install", "update", "remove"])
async def test_managed_service_refuses_nested_settings_changes_before_admission(
    service_state, action
):
    state, service, raw, patches = service_state
    state.runtime.extension_services = ExtensionServiceCatalog()
    close = bind_application_services(state, METHODS)
    services = state.runtime.extension_services.for_owner(
        ExtensionRegistrationIdentity("nested-service", "one"), lambda: None
    )
    previous_record, previous_settings = deepcopy(service.record), deepcopy(raw)

    async def configuration_callback(_state, _params):
        with pytest.raises(ExtensionServiceError) as refused:
            await services.execute(
                "managed_service.apply",
                {"id": "searxng", "action": action, "plan_id": "reviewed"},
            )
        assert refused.value.code == "mutation_in_progress"
        return {}

    try:
        async with asyncio.timeout(1):
            await serialized_mutation(
                configuration_callback, lock_attribute="_settings_mutation_lock"
            )(state, {})
        assert service.record == previous_record
        assert raw == previous_settings
        assert not patches
        assert not hasattr(state, "_managed_service_operations")
        # Once publication settles, ordinary admission and selection still work.
        assert (await apply(state, "install"))["state"] == "completed"
    finally:
        close()
