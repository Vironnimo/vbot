from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from cli.application._service_prerequisites import ActionRequiredError
from server.rpc import managed_service_methods as methods
from server.rpc import settings_methods
from server.rpc.dispatcher import dispatch_method


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
