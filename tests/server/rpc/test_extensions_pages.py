"""Extension page descriptors carry current, owner-bound attention state."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from core.extensions import ExtensionRegistry, _callbacks
from core.extensions.operations import ExtensionHost
from core.extensions.services import ExtensionServiceError
from server.rpc.errors import RpcError
from server.rpc.extension_presentation import (
    _contribution_operation,
    _contribution_sources,
    _contributions,
    _page_service,
)
from server.rpc.extensions_methods import _extension_pages, _extensions_payload
from tests.core.extensions.extension_test_support import write_package


@pytest.mark.asyncio
async def test_missing_page_and_dependent_views_recover_in_the_catalog_without_reload(
    tmp_path: Path,
) -> None:
    package = write_package(
        tmp_path,
        "fixture",
        "def register(api):\n"
        "    api.register_page('board', 'Board', 'index.html')\n"
        "    for name in ('first', 'second'):\n"
        "        api.register_contribution(name, surface='settings.sections', kind='view', "
        "title=name, page='board')\n",
    )
    registry = ExtensionRegistry.load(tmp_path)
    identity = registry.registration_identity("fixture")
    state = SimpleNamespace(
        runtime=SimpleNamespace(
            extensions=registry,
            storage=SimpleNamespace(
                load_extensions_settings=lambda: {"config": {}, "disabled": []}
            ),
        ),
        file_delivery=SimpleNamespace(open_extension_page=Mock(return_value={"url": "/page"})),
    )
    try:
        assert (await _contributions(state, {}))["contributions"] == []
        unavailable = (await _extensions_payload(state))["extensions"][0]["capability_errors"]
        assert len(unavailable) == 3
        assert all("unavailable" in message for message in unavailable)
        assert any("contribution 'first'" in message for message in unavailable)
        assert any("contribution 'second'" in message for message in unavailable)
        (package / "index.html").write_text("<!doctype html>", encoding="utf-8")
        assert (await _extensions_payload(state))["extensions"][0]["capability_errors"] == []
        assert len((await _contributions(state, {}))["contributions"]) == 2
        assert registry.registration_identity("fixture") == identity
        (package / "index.html").unlink()
        assert (await _extensions_payload(state))["extensions"][0][
            "capability_errors"
        ] == unavailable
        assert (await _contributions(state, {}))["contributions"] == []
    finally:
        registry.retire_registration()


@pytest.mark.asyncio
async def test_page_descriptors_read_badges_on_every_reconnect_and_drop_retired_owners(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extensions"
    package = write_package(
        root,
        "fixture",
        "def register(api):\n    api.register_page('board', 'Board', 'index.html')\n",
    )
    (package / "index.html").write_text("<!doctype html>", encoding="utf-8")
    registry = ExtensionRegistry.load(root)
    identity = registry.registration_identity("fixture")
    state = SimpleNamespace(
        runtime=SimpleNamespace(extensions=registry),
        file_delivery=SimpleNamespace(
            open_extension_page=Mock(return_value={"url": "/api/files/fixture"})
        ),
    )
    assert (await _extension_pages(state, {}))["pages"][0]["badge"] == 0
    registry.set_page_badge(identity, "board", 7)
    descriptor = (await _extension_pages(state, {}))["pages"][0]
    assert descriptor["badge"] == 7
    assert descriptor["route"] == "extension:fixture:board"
    assert "root" not in descriptor and "entry" not in descriptor
    registry.retire_registration()
    assert await _extension_pages(state, {}) == {"pages": []}
    state.runtime.extensions = ExtensionRegistry.load(root)
    assert (await _extension_pages(state, {}))["pages"][0]["badge"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["status", "save", "dynamic-save"])
async def test_contribution_actions_require_current_registration_and_declared_operation(
    tmp_path: Path, operation: str
) -> None:
    write_package(
        tmp_path,
        "fixture",
        """
def register(api):
    async def handler(params):
        if params.get('retire'):
            api.get_config()['retire']()
        if params.get('oversized'):
            return {'value': 'x' * (256 * 1024)}
        return {'items': [{'id': 'one', 'text': 'Current'}]}
    for operation in ('status', 'save', 'dynamic-save'):
        api.operations.register(operation, operation, {'type': 'object'}, handler)
    api.register_contribution('status', surface='shell.status', kind='status',
                              title='Status', source={'operation':'status'},
                              action={'operation':'save'}, operations=['dynamic-save'])
""",
    )
    retired_calls = []

    def retire() -> None:
        retired_calls.append(operation)
        registry.retire_registration()
        state.runtime.extensions = ExtensionRegistry.load(tmp_path)

    registry = ExtensionRegistry.load(tmp_path, config={"fixture": {"retire": retire}})
    state = SimpleNamespace(
        runtime=SimpleNamespace(extensions=registry), file_delivery=SimpleNamespace()
    )
    descriptors = await _contributions(state, {})
    descriptor = descriptors["contributions"][0]
    assert descriptor["id"] == "extension:fixture:status"
    assert "root" not in descriptor
    params = {
        "extension": "fixture",
        "id": "status",
        "epoch": descriptor["epoch"],
        "operation": operation,
    }
    assert (await _contribution_operation(state, params))["items"][0]["text"] == "Current"
    with pytest.raises(RpcError) as oversized:
        await _contribution_operation(state, {**params, "params": {"oversized": True}})
    assert oversized.value.code == "invalid_request"
    batch = (await _contribution_sources(state, {"requests": [params, None]}))["results"]
    assert batch[0]["ok"] is (operation == "status")
    assert batch[1]["error"]["code"] == "invalid_request"
    if operation == "status":
        assert batch[0]["result"]["items"][0]["text"] == "Current"
    else:
        assert batch[0]["error"]["code"] == "invalid_request"
    with pytest.raises(RpcError) as refused:
        await _contribution_operation(state, {**params, "operation": "undeclared"})
    assert refused.value.code == "invalid_request"
    if operation == "status":
        with pytest.raises(RpcError) as stale_read:
            await _contribution_operation(state, {**params, "params": {"retire": True}})
        assert stale_read.value.code == "invalid_request"
    else:
        result = await _contribution_operation(state, {**params, "params": {"retire": True}})
        assert result["items"][0]["text"] == "Current"
    assert retired_calls == [operation]
    assert registry.contribution_declarations() == []
    with pytest.raises(RpcError):
        await _contribution_operation(state, params)
    stale_batch = await _contribution_sources(state, {"requests": [params]})
    assert stale_batch["results"][0]["error"]["code"] == "invalid_request"
    assert retired_calls == [operation]
    state.runtime.extensions.retire_registration()
    assert (await _contributions(state, {}))["contributions"] == []


@pytest.mark.asyncio
async def test_contribution_source_batches_share_bounded_admission_and_keep_entry_outcomes(
    tmp_path: Path,
) -> None:
    for name in ("fixture", "other"):
        write_package(
            tmp_path,
            name,
            "def register(api):\n"
            "    api.operations.register('read', 'Read', {'type':'object'}, "
            "api.get_config()['read'])\n"
            "    api.register_contribution('row', surface='chat.message', kind='status', "
            "title='Row', source={'operation':'read'})\n",
        )
    entered: asyncio.Queue[int] = asyncio.Queue()
    release = asyncio.Event()
    active = peak = 0

    async def read(params):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        entered.put_nowait(params["index"])
        try:
            await release.wait()
            if params["index"] == 3:
                raise RuntimeError("test-private-internal-detail")
            if params["index"] == 4:
                return {"items": ["x" * 262_144]}
            return {"index": params["index"]}
        finally:
            active -= 1

    registry = ExtensionRegistry.load(
        tmp_path, config={name: {"read": read} for name in ("fixture", "other")}
    )
    state = SimpleNamespace(runtime=SimpleNamespace(extensions=registry))
    request = {
        "extension": "fixture",
        "id": "row",
        "operation": "read",
        "epoch": registry.registration_identity("fixture").epoch,
    }
    batches = []
    try:
        bad_requests: tuple[Any, ...] = ([], [{}] * 33, {})
        for bad in bad_requests:
            with pytest.raises(RpcError) as refused:
                await _contribution_sources(state, {"requests": bad})
            assert refused.value.code == "invalid_request"
        for offset in (0, 32):
            batches.append(
                asyncio.create_task(
                    _contribution_sources(
                        state,
                        {
                            "requests": [
                                {
                                    **request,
                                    "extension": "fixture" if index % 2 else "other",
                                    "params": {"index": offset + index},
                                }
                                for index in range(32)
                            ]
                        },
                    )
                )
            )
        for _ in range(4):
            await asyncio.wait_for(entered.get(), 1)
        assert active == peak == 4
        release.set()
        results = [entry for batch in await asyncio.gather(*batches) for entry in batch["results"]]
        assert peak == 4
        assert len(results) == 64
        for index, outcome in enumerate(results):
            if index == 3:
                assert outcome["error"]["code"] == "internal_error"
                assert "test-private-internal-detail" not in str(outcome)
            elif index == 4:
                assert outcome["error"]["code"] == "invalid_request"
            else:
                assert outcome == {"ok": True, "result": {"index": index}}
    finally:
        release.set()
        await asyncio.gather(*batches, return_exceptions=True)
        registry.retire_registration()


@pytest.mark.asyncio
async def test_timed_out_source_work_does_not_hold_shared_slots_or_restart_unsettled_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("slow", "healthy"):
        write_package(
            tmp_path,
            name,
            "def register(api):\n"
            "    api.operations.register('read', 'Read', {'type':'object'}, "
            "api.get_config()['read'])\n"
            "    api.register_contribution('row', surface='chat.message', kind='status', "
            "title='Row', source={'operation':'read'})\n",
        )
    entered = 0
    all_entered, expire, release = (asyncio.Event() for _ in range(3))

    async def slow(_params):
        nonlocal entered
        entered += 1
        if entered == 2:
            all_entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()  # Real Extension code can ignore cancellation.
        return {"done": True}

    async def healthy(_params):
        return {"healthy": True}

    original_wait = _callbacks._wait
    waits = 0

    async def deadline(tasks, *, timeout):
        nonlocal waits
        waits += 1
        if waits <= 2:
            await expire.wait()
            return set(), set(tasks)
        return await original_wait(tasks, timeout=timeout)

    monkeypatch.setattr(_callbacks, "_wait", deadline)
    registry = ExtensionRegistry.load(
        tmp_path,
        config={
            "slow": {"read": slow},
            "healthy": {"read": healthy},
        },
    )
    state = SimpleNamespace(runtime=SimpleNamespace(extensions=registry))

    def request(owner):
        return {
            "extension": owner,
            "id": "row",
            "operation": "read",
            "epoch": registry.registration_identity(owner).epoch,
        }

    batches = []
    try:
        batches.append(
            asyncio.create_task(
                _contribution_sources(
                    state,
                    {
                        "requests": [request("slow")] * 4,
                    },
                )
            )
        )
        await asyncio.wait_for(all_entered.wait(), 1)
        batches.append(
            asyncio.create_task(
                _contribution_sources(
                    state,
                    {
                        "requests": [request("healthy")],
                    },
                )
            )
        )
        # A slow owner's queued rows do not take every global permit, even
        # before its deadline expires.
        working = await asyncio.wait_for(batches[1], 1)
        assert working["results"] == [{"ok": True, "result": {"healthy": True}}]
        expire.set()
        failed = await asyncio.wait_for(batches[0], 1)
        assert [row["error"]["code"] for row in failed["results"]] == [
            "callback_timeout",
            "callback_timeout",
            "callback_unavailable",
            "callback_unavailable",
        ]
        blocked = await _contribution_sources(state, {"requests": [request("slow")]})
        assert blocked["results"][0]["error"]["code"] == "callback_unavailable"
        assert entered == 2
        release.set()
        await _callbacks.wait_for_extension_callbacks("slow")
        recovered = await _contribution_sources(state, {"requests": [request("slow")]})
        assert recovered["results"] == [{"ok": True, "result": {"done": True}}]
    finally:
        expire.set()
        release.set()
        await asyncio.gather(*batches, return_exceptions=True)
        await _callbacks.wait_for_extension_callbacks("slow")
        registry.retire_registration()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["query", "execute"])
async def test_page_services_preserve_scope_refusal_and_admitted_results(
    tmp_path: Path, kind: str
) -> None:
    package = write_package(
        tmp_path,
        "fixture",
        """
def register(api):
    api.register_page('settings', 'Settings', 'page.html', navigation=False,
                      services=('agent.list', 'settings.update'))
""",
    )
    (package / "page.html").write_text("<!doctype html>", encoding="utf-8")
    registry = ExtensionRegistry.load(tmp_path)
    identity = registry.registration_identity("fixture")
    services = SimpleNamespace(
        describe=lambda: {
            "available": True,
            "services": [
                {"name": "agent.list", "resources": ["agents"]},
                {"name": "settings.update", "resources": ["settings"]},
                {"name": "agent.delete", "resources": ["agents"]},
            ],
            "resource_definitions": [
                {"name": "agents", "description": "Agents"},
                {"name": "unrelated", "description": "Another service's resource"},
            ],
        },
        query=AsyncMock(return_value={"agents": []}),
        execute=AsyncMock(),
    )
    registry.bind_host(Mock(spec=ExtensionHost, for_owner=None, services=services))
    state = SimpleNamespace(runtime=SimpleNamespace(extensions=registry))
    params = {
        "extension": "fixture",
        "page": "settings",
        "epoch": identity.epoch,
        "kind": "query",
        "name": "agent.list",
    }
    assert await _page_service(state, params) == {"agents": []}
    description = await _page_service(state, {**params, "kind": "describe"})
    assert [item["name"] for item in description["services"]] == ["agent.list", "settings.update"]
    assert description["resources"] == ["agents", "settings"]
    assert description["resource_definitions"] == [{"name": "agents", "description": "Agents"}]
    with pytest.raises(RpcError):
        await _page_service(state, {**params, "name": "agent.delete", "kind": "execute"})
    services.execute.assert_not_awaited()
    services.query.side_effect = ExtensionServiceError(
        "fixture_conflict", "Conflict", {"revision": 2}
    )
    with pytest.raises(RpcError) as refused:
        await _page_service(state, params)
    assert (refused.value.code, refused.value.data) == ("fixture_conflict", {"revision": 2})
    services.query.side_effect = None

    async def retire(_name, _params):
        registry.retire_registration()
        state.runtime.extensions = ExtensionRegistry.load(tmp_path)
        return {"committed": True}

    getattr(services, kind).side_effect = retire
    retiring_params = {
        **params,
        "kind": kind,
        "name": "agent.list" if kind == "query" else "settings.update",
    }
    if kind == "query":
        with pytest.raises(RpcError) as stale_read:
            await _page_service(state, retiring_params)
        assert stale_read.value.code == "invalid_request"
    else:
        assert await _page_service(state, retiring_params) == {"committed": True}
        services.execute.assert_awaited_once_with("settings.update", {})
    with pytest.raises(RpcError):
        await _page_service(state, retiring_params)
    state.runtime.extensions.retire_registration()
