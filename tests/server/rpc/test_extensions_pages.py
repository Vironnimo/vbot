"""Extension page descriptors carry current, owner-bound attention state."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core.extensions import ExtensionRegistry
from core.extensions.operations import ExtensionHost
from core.extensions.services import ExtensionServiceError
from server.rpc.errors import RpcError
from server.rpc.extension_presentation import _contribution_operation, _contributions, _page_service
from server.rpc.extensions_methods import _extension_pages
from tests.core.extensions.extension_test_support import write_package


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
async def test_contribution_actions_require_current_registration_and_declared_operation(
    tmp_path: Path,
) -> None:
    write_package(
        tmp_path,
        "fixture",
        """
async def status(params):
    return {'items': [{'id': 'one', 'text': 'Current'}]}
def register(api):
    api.operations.register('status', 'Status', {'type': 'object'}, status)
    api.register_contribution('status', surface='shell.status', kind='status',
                              title='Status', source={'operation':'status'})
""",
    )
    registry = ExtensionRegistry.load(tmp_path)
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
        "operation": "status",
    }
    assert (await _contribution_operation(state, params))["items"][0]["text"] == "Current"
    with pytest.raises(RpcError) as refused:
        await _contribution_operation(state, {**params, "operation": "undeclared"})
    assert refused.value.code == "invalid_request"
    registry.retire_registration()
    assert (await _contributions(state, {}))["contributions"] == []
    state.runtime.extensions = ExtensionRegistry.load(tmp_path)
    with pytest.raises(RpcError):
        await _contribution_operation(state, params)


@pytest.mark.asyncio
async def test_page_services_preserve_declaration_scope_and_domain_refusal(tmp_path: Path) -> None:
    package = write_package(
        tmp_path,
        "fixture",
        """
def register(api):
    api.register_page('settings', 'Settings', 'page.html', navigation=False,
                      services=('agent.list',))
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
    assert [item["name"] for item in description["services"]] == ["agent.list"]
    assert description["resources"] == ["agents"]
    with pytest.raises(RpcError):
        await _page_service(state, {**params, "name": "settings.update", "kind": "execute"})
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
        return {"agents": []}

    services.query.side_effect = retire
    with pytest.raises(RpcError):
        await _page_service(state, params)
