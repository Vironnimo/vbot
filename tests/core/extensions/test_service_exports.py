"""Extension-owned services share the catalog without crossing owner lifetimes."""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from core.extensions import ExtensionRegistry
from core.extensions import _callbacks as extension_callbacks
from core.extensions.operations import ExtensionHost
from core.extensions.services import (
    ExtensionService,
    ExtensionServiceCatalog,
    ExtensionServiceError,
    ExtensionServices,
    ServiceKind,
)
from core.tools import ToolRegistry
from tests.core.extensions.extension_test_support import record, write_extension

_EXPORT_SOURCE = """
import threading

values = [1]
schema = {
    "type": "object",
    "properties": {"id": {"type": "string", "minLength": 1}},
    "required": ["id"],
    "additionalProperties": False,
}

def read(params):
    params["private"] = True
    return {"id": params["id"], "values": values, "thread": threading.get_ident()}

async def read_async(params):
    return read(params)

def register(api):
    api.register_service("read", "query", schema, read, resources=["items"])
    api.register_service("read_async", "query", schema, read_async)
    api.register_service("missing", "query", schema, read, resources=["not_declared"])
    api.register_resource("items", description="Changed items", scope_schema=schema)
    schema["additionalProperties"] = True
    schema["properties"]["id"]["type"] = "integer"
"""


def _owner(
    catalog: ExtensionServiceCatalog, registry: ExtensionRegistry, name: str
) -> ExtensionServices:
    identity = registry.registration_identity(name)

    def require_current() -> None:
        if not registry.is_registration_current(identity):
            raise ExtensionServiceError("service_unavailable", "Test registration retired")

    return catalog.for_owner(identity, require_current)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_export_declarations_resolve_late_resources_and_detach_schema_and_values(
    tmp_path: Path, asynchronous: bool
) -> None:
    write_extension(tmp_path, "provider", _EXPORT_SOURCE)
    write_extension(tmp_path, "consumer", "def register(api): pass\n")
    registry = await ExtensionRegistry.aload(tmp_path)
    catalog = ExtensionServiceCatalog()
    catalog.install_exports(registry)
    services = _owner(catalog, registry, "consumer")
    description = services.describe()
    names = [item["name"] for item in description["services"]]
    assert names == ["extension:provider:read", "extension:provider:read_async"]
    assert record(registry, "provider").capability_errors
    read = description["services"][0]
    assert read["resources"] == ["extension:provider:items"]
    assert read["parameters"]["additionalProperties"] is False
    assert read["parameters"]["properties"]["id"]["type"] == "string"
    read["parameters"]["properties"]["id"]["type"] = "number"
    assert services.describe()["services"][0]["parameters"]["properties"]["id"]["type"] == "string"
    assert description["resource_definitions"][0]["scope_schema"]["additionalProperties"] is False

    name = "extension:provider:read_async" if asynchronous else "extension:provider:read"
    arguments = {"id": "fixture"}
    result = await services.query(name, arguments)
    assert arguments == {"id": "fixture"}
    assert result["id"] == "fixture"
    assert (result["thread"] == threading.get_ident()) is asynchronous
    result["values"].append(2)
    assert (await services.query(name, arguments))["values"] == [1]
    with pytest.raises(ExtensionServiceError) as invalid:
        await services.query(name, {"id": 42})
    assert invalid.value.code == "invalid_request"
    assert invalid.value.data["path"] == ["id"]
    with pytest.raises(ExtensionServiceError) as wrong_kind:
        await services.execute(name, arguments)
    assert wrong_kind.value.code == "service_kind_mismatch"
    catalog.close()


@pytest.mark.asyncio
async def test_custom_resources_are_owner_scoped_validated_and_detached(tmp_path: Path) -> None:
    for name in ("one", "two"):
        write_extension(tmp_path, name, _EXPORT_SOURCE)
    registry = await ExtensionRegistry.aload(tmp_path)
    catalog = ExtensionServiceCatalog()
    catalog.install_exports(registry)
    one, two = (_owner(catalog, registry, name) for name in ("one", "two"))
    observed: list[dict[str, Any]] = []
    delivered = asyncio.Event()

    async def receive(event: dict[str, Any]) -> None:
        observed.append(event)
        delivered.set()

    two.subscribe(["extension:one:items"], receive)
    try:
        with pytest.raises(ExtensionServiceError) as invalid:
            one.publish("items", {"id": 42})
        assert invalid.value.code == "invalid_request"
        assert invalid.value.data["path"] == ["id"]
        with pytest.raises(ExtensionServiceError) as invalid_name:
            two.publish("extension:one:items")
        assert invalid_name.value.code == "invalid_request"
        with pytest.raises(ExtensionServiceError) as missing:
            two.publish("undeclared")
        assert missing.value.code == "resource_not_found"
        two.publish("items", {"id": "other-owner"})
        scope = {"id": "fixture"}
        await asyncio.to_thread(one.publish, "items", scope)
        scope["id"] = "modified"
        await delivered.wait()
        assert len(observed) == 1
        assert observed[0]["kind"] == "extension:one:items"
        assert observed[0]["scope"] == {"id": "fixture"}
        delivered.clear()
        one.publish("items")
        await delivered.wait()
        assert "scope" not in observed[-1]
    finally:
        await catalog.release_owner(registry.registration_identity("two"))
        catalog.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["query", "execute"])
@pytest.mark.parametrize("retired_owner", ["provider", "consumer"])
async def test_export_retirement_fences_reads_and_preserves_admitted_action_outcomes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: ServiceKind, retired_owner: str
) -> None:
    write_extension(
        tmp_path,
        "provider",
        "entered = None\nfinish = None\n"
        "async def operation(params):\n"
        "    entered.set()\n"
        "    await finish.wait()\n"
        "    return {'committed': True}\n"
        "def register(api):\n"
        f"    api.register_service('operation', {kind!r}, {{'type': 'object'}}, operation)\n",
    )
    write_extension(tmp_path, "consumer", "def register(api): pass\n")
    registry = await ExtensionRegistry.aload(tmp_path)
    entered, finish = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(sys.modules["vbot_ext.provider"], "entered", entered)
    monkeypatch.setattr(sys.modules["vbot_ext.provider"], "finish", finish)
    catalog = ExtensionServiceCatalog()
    catalog.install_exports(registry)
    services = _owner(catalog, registry, "consumer")
    pending = asyncio.create_task(getattr(services, kind)("extension:provider:operation"))
    await entered.wait()
    await registry.deactivate(retired_owner, ToolRegistry())
    finish.set()
    if kind == "query":
        with pytest.raises(ExtensionServiceError) as retired:
            await pending
        assert retired.value.code == "service_unavailable"
    else:
        assert await pending == {"committed": True}
    with pytest.raises(ExtensionServiceError) as unavailable:
        await getattr(services, kind)("extension:provider:operation")
    assert unavailable.value.code == "service_unavailable"
    catalog.close()


@pytest.mark.asyncio
async def test_startup_discovers_other_exports_before_their_own_startup(tmp_path: Path) -> None:
    write_extension(
        tmp_path,
        "a_consumer",
        "observed = []\n"
        "async def start(host):\n"
        "    observed.append(host.services.describe())\n"
        "    observed.append(await host.services.query(\n"
        "        'extension:z_provider:read', {'id': 'start'}))\n"
        "def register(api):\n"
        "    api.operations.startup.append(start)\n",
    )
    write_extension(tmp_path, "z_provider", _EXPORT_SOURCE)
    registry = await ExtensionRegistry.aload(tmp_path)
    catalog = ExtensionServiceCatalog()
    catalog.install_exports(registry)
    registry.bind_host(
        cast(
            ExtensionHost,
            SimpleNamespace(
                for_owner=lambda identity: SimpleNamespace(
                    services=_owner(catalog, registry, identity.name)
                )
            ),
        )
    )
    await registry.fire_startup()
    observed = sys.modules["vbot_ext.a_consumer"].observed
    assert observed[0]["available"] is True
    assert observed[1]["id"] == "start"
    catalog.close()


@pytest.mark.asyncio
async def test_old_epoch_release_and_builtin_rebinding_preserve_current_exports(
    tmp_path: Path,
) -> None:
    write_extension(tmp_path, "provider", _EXPORT_SOURCE)
    old_registry = await ExtensionRegistry.aload(tmp_path)
    old_identity = old_registry.registration_identity("provider")
    catalog = ExtensionServiceCatalog()
    catalog.install_exports(old_registry)
    old_services = _owner(catalog, old_registry, "provider")
    new_registry = await ExtensionRegistry.aload(tmp_path)
    catalog.install_exports(new_registry)
    new_services = _owner(catalog, new_registry, "provider")
    old_registry.retire_registration()
    await catalog.release_owner(old_identity)
    with pytest.raises(ExtensionServiceError):
        old_services.publish("items")
    assert (await new_services.query("extension:provider:read", {"id": "new"}))["id"] == "new"

    builtin = ExtensionService(
        "builtin.read", "query", {"type": "object"}, AsyncMock(return_value={})
    )
    first = catalog.bind([builtin], resources=["builtin"])
    second = catalog.bind([builtin], resources=["replacement"])
    first()
    assert await new_services.query("builtin.read") == {}
    second()
    description = new_services.describe()
    assert description["available"] is True
    assert "builtin" not in description["resources"]
    assert "replacement" not in description["resources"]
    assert "extension:provider:items" in description["resources"]
    assert (await new_services.query("extension:provider:read", {"id": "after"}))["id"] == "after"
    await catalog.release_owner(new_registry.registration_identity("provider"))
    assert new_services.describe()["available"] is False
    catalog.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("lifecycle", ["deactivate", "shutdown"])
async def test_shutdown_refuses_new_exports_but_keeps_owner_cleanup_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lifecycle: str
) -> None:
    write_extension(
        tmp_path,
        "provider",
        "cleanup = None\n"
        "writes = []\n"
        "async def write(params):\n"
        "    writes.append(params)\n"
        "    return {'written': True}\n"
        "async def shutdown(): await cleanup()\n"
        "def register(api):\n"
        "    api.register_service('write', 'execute', {'type': 'object'}, write)\n"
        "    api.register_resource('items')\n"
        "    api.on_shutdown(shutdown)\n",
    )
    registry = await ExtensionRegistry.aload(tmp_path)
    catalog = ExtensionServiceCatalog()
    catalog.install_exports(registry)
    owner = _owner(catalog, registry, "provider")
    catalog.bind(
        [ExtensionService("cleanup.read", "query", {"type": "object"}, AsyncMock(return_value={}))]
    )
    registry.bind_host(
        cast(ExtensionHost, SimpleNamespace(release_owner=catalog.release_owner, for_owner=None))
    )
    entered, finish = asyncio.Event(), asyncio.Event()

    async def cleanup() -> None:
        assert await owner.query("cleanup.read") == {}
        owner.publish("items", {"phase": "shutdown"})
        entered.set()
        await finish.wait()

    module = sys.modules["vbot_ext.provider"]
    monkeypatch.setattr(module, "cleanup", cleanup)
    stopping = asyncio.create_task(
        registry.deactivate("provider", ToolRegistry())
        if lifecycle == "deactivate"
        else registry.fire_shutdown()
    )
    try:
        await entered.wait()
        assert registry.is_registration_current(registry.registration_identity("provider"))
        with pytest.raises(ExtensionServiceError) as retired:
            await owner.execute("extension:provider:write")
        assert retired.value.code == "service_unavailable"
        assert module.writes == []
    finally:
        finish.set()
        await stopping
        catalog.close()


@pytest.mark.asyncio
async def test_waiting_export_rechecks_retirement_after_worker_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_extension(
        tmp_path,
        "provider",
        "write = None\n"
        "def invoke(params): return write(params)\n"
        "def register(api):\n"
        "    api.register_service('write', 'execute', {'type': 'object'}, invoke)\n",
    )
    registry = await ExtensionRegistry.aload(tmp_path)
    catalog = ExtensionServiceCatalog()
    catalog.install_exports(registry)
    owner = _owner(catalog, registry, "provider")
    entered, queued = asyncio.Event(), asyncio.Event()
    finish = threading.Event()
    loop = asyncio.get_running_loop()
    writes = []

    def write(params: dict[str, Any]) -> dict[str, Any]:
        writes.append(params)
        loop.call_soon_threadsafe(entered.set)
        finish.wait()
        return {"written": True}

    monkeypatch.setattr(sys.modules["vbot_ext.provider"], "write", write)
    monkeypatch.setattr(extension_callbacks, "_OWNER_WORKER_LIMIT", 1)
    real_wait = extension_callbacks._wait  # noqa: SLF001 - isolate the admitted-call boundary
    waits = 0

    async def track_wait(
        tasks: set[asyncio.Task[Any]], *, timeout: float | None = None
    ) -> tuple[set[asyncio.Task[Any]], set[asyncio.Task[Any]]]:
        nonlocal waits
        waits += 1
        if waits == 2:
            queued.set()
        return await real_wait(tasks, timeout=timeout)

    monkeypatch.setattr(extension_callbacks, "_wait", track_wait)
    running = asyncio.create_task(owner.execute("extension:provider:write", {"id": "running"}))
    waiting: asyncio.Task[dict[str, Any]] | None = None
    try:
        await entered.wait()
        waiting = asyncio.create_task(owner.execute("extension:provider:write", {"id": "queued"}))
        await queued.wait()
        assert await registry.deactivate("provider", ToolRegistry()) is True
        finish.set()
        assert await running == {"written": True}
        with pytest.raises(ExtensionServiceError) as retired:
            await waiting
        assert retired.value.code == "service_unavailable"
        assert writes == [{"id": "running"}]
    finally:
        finish.set()
        await asyncio.gather(running, *([waiting] if waiting else []), return_exceptions=True)
        catalog.close()
