"""MCP: connection management keeps Tools and runners consistent while connections change."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest

import resources.extensions.mcp
from core.database import write_bootstrap_marker
from core.extensions import ExtensionRegistrationIdentity
from core.extensions.databases import ExtensionDatabases
from core.extensions.extensions import ExtensionAPI, ExtensionDeclarations
from core.tools.tools import ToolRegistry, tool_success
from resources.extensions.mcp._connectors import load_connectors
from resources.extensions.mcp.client import ConnectionRunner
from resources.extensions.mcp.config import validate_connection
from resources.extensions.mcp.extension import remote_tool_name
from tests.resources.extensions.mcp.mcp_test_support import (
    context,
    dispatch,
    model_text,
    start_service,
)

_USAGE = "Describe tools for their arguments schemas, then call them."
_NO_TOOLS = "No tools reported yet; search lists them."
_CONNECTION = {"id": "example", "transport": "stdio", "command": "unused"}
_CATALOG = {
    "server_info": {"name": "blender-mcp", "title": "Blender"},
    "tools": [
        {"name": "get_scene_info", "inputSchema": {"type": "object"}},
        {"name": "execute_blender_code", "inputSchema": {"type": "object"}},
    ],
}


def _tool_names(registry, *, ready_only: bool = False) -> list[str]:
    return [tool.name for tool in registry.list_tools(ready_only=ready_only)]


def _description(registry) -> str:
    return str(registry.get("mcp_example").description)


def _tools(registry) -> str:
    """What the connection Tool's ``target`` lists after its fixed text."""
    target = registry.get("mcp_example").parameters["properties"]["target"]["description"]
    return str(target).split("Required for call. ", 1)[1]


_DISABLED = (
    "Error (tool_not_ready): The MCP connection example is disabled, so nothing was run. "
    "Tell the user to enable it in Settings -> Integrations -> Extensions -> MCP connections "
    "if it is needed.\nretryable: false"
)
_DISCONNECTED = (
    "Error (tool_not_ready): The MCP connection example is not connected, so nothing was run. "
    "Call this tool again through mcp_example, which reconnects first. If it cannot connect, "
    "tell the user that the MCP server example cannot be reached.\nretryable: true"
)
_REMOVED = (
    "Error (tool_not_ready): The MCP connection example was removed, so nothing was run. "
    "Tell the user if it is needed.\nretryable: false"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["disable", "remove"])
async def test_stopping_a_connection_that_ignores_cancellation_still_retires_its_tools(
    host, monkeypatch, caplog, operation
):
    service, registry = await start_service(host)
    service.connections["example"] = validate_connection(
        {"id": "example", "transport": "stdio", "command": "python"}
    )
    runner = service._runner(service.connections["example"])
    runner.state = "connected"
    runner.catalog = {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]}
    service._publish(runner, runner.catalog)
    names = ["mcp_example", remote_tool_name("example", "echo")]
    assert set(names) <= set(_tool_names(registry, ready_only=True))
    # While the connection is down, its remote Tools are hidden and say how to reconnect.
    runner.state = "failed"
    assert names[1] not in _tool_names(registry, ready_only=True)
    result = await registry.dispatch(
        replace(context(host), tool_name=names[1]), {}, allowed_tools=names
    )
    assert model_text(result) == _DISCONNECTED
    runner.state = "connected"
    release = asyncio.Event()

    async def stuck() -> None:
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    runner._task = asyncio.create_task(stuck())
    await asyncio.sleep(0)  # enter the loop so cancellation is actually ignored
    monkeypatch.setattr("resources.extensions.mcp.client.CONNECTION_CLOSE_TIMEOUT_SECONDS", 0.05)
    close = runner.close

    async def inspect_before_close():
        if operation == "remove":
            # Removal has committed, but the Tools stay registered until close ends.
            assert "example" not in service.connections
            assert not set(names) & set(_tool_names(registry, ready_only=True))
            for name in names:
                result = await registry.dispatch(
                    replace(context(host), tool_name=name), {}, allowed_tools=names
                )
                assert model_text(result) == _REMOVED
        await close()

    monkeypatch.setattr(runner, "close", inspect_before_close)

    try:
        with caplog.at_level(logging.WARNING, logger="vbot.extensions.mcp"):
            stopping = asyncio.create_task(service.management.manage(operation, {"id": "example"}))
            done, _ = await asyncio.wait({stopping}, timeout=5)
            assert done, "stopping must not wait forever on a connection ignoring cancellation"
            await stopping

        # The Agent no longer sees the connection's Tools; naming one says why.
        assert not set(names) & set(_tool_names(registry, ready_only=True))
        if operation == "disable":
            for name in names:
                result = await registry.dispatch(
                    replace(context(host), tool_name=name), {}, allowed_tools=names
                )
                assert model_text(result) == _DISABLED
        else:
            assert not set(names) & set(_tool_names(registry))
        assert runner.state == "disconnected"
        assert "did not stop within" in caplog.text
    finally:
        release.set()
        await runner._task
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("access", ["connect", "tool"])
async def test_save_serializes_runner_access_until_replacement_is_ready(host, monkeypatch, access):
    service, registry = await start_service(host)
    started_commands = []

    def start(runner):
        started_commands.append((runner.id, runner.config["command"]))
        runner.state = "connected"

    async def browse(runner, context, arguments):
        return tool_success({"command": runner.config["command"]})

    monkeypatch.setattr(ConnectionRunner, "start", start)
    monkeypatch.setattr(service, "_browse", browse)
    connection = {"id": "example", "transport": "stdio", "command": "old-command"}
    await service.management.manage("save", {"connection": connection})
    await service.management.manage(
        "save", {"connection": {**connection, "id": "other", "command": "other-command"}}
    )
    closing = asyncio.Event()
    release = asyncio.Event()

    async def close():
        closing.set()
        await release.wait()

    monkeypatch.setattr(service.runners["example"], "close", close)
    replacement = {**connection, "command": "new-command"}
    saving = asyncio.create_task(service.management.manage("save", {"connection": replacement}))
    accessing = None
    try:
        await asyncio.wait_for(closing.wait(), 1)
        entered = asyncio.Event()

        async def concurrent_access():
            entered.set()
            if access == "connect":
                return await service.management.manage("connect", {"id": "example"})
            return await dispatch(registry, host, {"action": "search"})

        accessing = asyncio.create_task(concurrent_access())
        await asyncio.wait_for(entered.wait(), 1)
        assert not accessing.done()
        other = await asyncio.wait_for(service.management.manage("connect", {"id": "other"}), 1)
        assert other["configuration"]["command"] == "other-command"
        # Saving another connection does not wait for this one to close.
        changed = {**connection, "id": "other", "command": "changed-command"}
        other = await asyncio.wait_for(
            service.management.manage("save", {"connection": changed}), 1
        )
        assert other["configuration"]["command"] == "changed-command"
        release.set()
        _, result = await asyncio.wait_for(asyncio.gather(saving, accessing), 1)
        assert service.store.load()["other"]["command"] == "changed-command"
        assert service.store.load()["example"]["command"] == "new-command"
        assert service.connections["example"]["command"] == "new-command"
        assert service.runners["example"].config["command"] == "new-command"
        example_commands = [
            command for identifier, command in started_commands if identifier == "example"
        ]
        assert example_commands[0] == "old-command"
        assert set(example_commands[1:]) == {"new-command"}
        assert "mcp_example" in _tool_names(registry)
        if access == "tool":
            assert result["data"] == {"command": "new-command"}
    finally:
        release.set()
        tasks = [saving, *([accessing] if accessing is not None else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["save", "disable", "remove"])
@pytest.mark.parametrize("shutdown", [False, True])
async def test_admitted_connection_changes_settle_before_cancellation_and_shutdown(
    host, monkeypatch, operation, shutdown
):
    service, registry = await start_service(host)
    monkeypatch.setattr(ConnectionRunner, "start", lambda runner: None)
    await service.management.manage("save", {"connection": _CONNECTION})
    original_runner = service.runners["example"]
    original_runner.state = "connected"
    original_runner.catalog = _CATALOG
    service._publish(original_runner, _CATALOG)
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    save = service.store.save

    def held_save(records):
        save(records)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=5)

    monkeypatch.setattr(service.store, "save", held_save)
    arguments = (
        {"connection": {**_CONNECTION, "command": "replacement"}}
        if operation == "save"
        else {"id": "example"}
    )
    changing = asyncio.create_task(service.management.manage(operation, arguments))
    closing = None
    waiting = None
    try:
        await entered.wait()
        # A request still waiting for this connection can be cancelled without
        # changing its configuration later.
        waiting = asyncio.create_task(
            service.management.manage(
                "save", {"connection": {**_CONNECTION, "command": "cancelled"}}
            )
        )
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        changing.cancel()
        await asyncio.sleep(0)
        changing.cancel()
        assert not changing.done()
        if shutdown:
            closing = asyncio.create_task(service.close())
            await asyncio.sleep(0)
            assert not closing.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await changing
        if closing is not None:
            await closing
        saved = service.store.load()
        assert saved == service.connections
        assert original_runner.state == "disconnected"
        if operation == "remove":
            assert "example" not in saved
        elif operation == "save":
            assert saved["example"]["command"] == "replacement"
        else:
            assert saved["example"]["enabled"] is False
        if shutdown:
            assert service.runners == {}
        elif operation == "remove":
            assert "mcp_example" not in _tool_names(registry)
        else:
            assert service.runners["example"] is not original_runner
            if operation == "disable":
                assert not _tool_names(registry, ready_only=True)
    finally:
        release.set()
        await asyncio.gather(changing, *([waiting] if waiting else []), return_exceptions=True)
        if closing is not None:
            await closing
        await service.close()


@pytest.mark.asyncio
async def test_description_only_save_republishes_without_reconnecting(host, monkeypatch):
    started = []
    monkeypatch.setattr(ConnectionRunner, "start", lambda runner: started.append(runner))
    service, registry = await start_service(host)
    try:
        await service.management.manage("save", {"connection": _CONNECTION})
        runner = service.runners["example"]
        runner.catalog = dict(_CATALOG)
        service._publish(runner, runner.catalog)
        described = {**_CONNECTION, "description": "  Studio Blender.  "}

        await service.management.manage("save", {"connection": described})

        assert service.runners["example"] is runner
        assert started == [runner]
        assert service.store.load()["example"]["description"] == "Studio Blender."
        assert _description(registry) == f"MCP connection example: Studio Blender. {_USAGE}"
        assert _tools(registry) == "Tools: get_scene_info, execute_blender_code"
        assert registry.get(remote_tool_name("example", "get_scene_info")) is not None

        # Any other change replaces the connection; its Tool names stay until a new catalog.
        await service.management.manage("save", {"connection": {**_CONNECTION, "command": "other"}})

        assert service.runners["example"] is not runner
        assert _description(registry) == f"MCP connection example: Blender. {_USAGE}"
        assert _tools(registry) == "Tools: get_scene_info, execute_blender_code"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_known_tool_names_survive_restarts_and_leave_with_the_connection(host, monkeypatch):
    monkeypatch.setattr(ConnectionRunner, "start", lambda runner: None)
    write_bootstrap_marker(host.data_dir)
    databases = ExtensionDatabases(host.data_dir)
    owners = []

    async def restart():
        owner = ExtensionRegistrationIdentity("mcp", f"registration-{len(owners)}")
        owners.append(owner)
        return await start_service(replace(host, open_database=databases.opener(owner)))

    async def stop(service):
        await service.close()
        await databases.release(owners[-1])

    try:
        service, registry = await restart()
        await service.management.manage("save", {"connection": _CONNECTION})
        assert _tools(registry) == _NO_TOOLS
        written = asyncio.Event()
        write = service.catalogs._write

        async def write_before_cleanup(connection):
            await write(connection)
            written.set()

        monkeypatch.setattr(service.catalogs, "_write", write_before_cleanup)
        service._publish(service.runners["example"], _CATALOG)
        known = _tools(registry)
        assert known == "Tools: get_scene_info, execute_blender_code"
        # Resume before the completed write's discard callback. Closing must
        # remove settled Tasks itself instead of starving that callback.
        await written.wait()
        await service.catalogs.close()
        await stop(service)

        service, registry = await restart()
        assert _tools(registry) == known
        await service.management.manage("remove", {"id": "example"})
        await stop(service)

        service, registry = await restart()
        await service.management.manage("save", {"connection": _CONNECTION})
        assert _tools(registry) == _NO_TOOLS
        await stop(service)
    finally:
        databases.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "referencing",
    [
        {**_CONNECTION, "credential_environment": {"TOKEN": "EXAMPLE_TOKEN"}},
        {
            "id": "example",
            "transport": "http",
            "url": "https://mcp.example.com/mcp",
            "oauth": True,
            "oauth_client_id": "vbot",
            "oauth_client_secret": "EXAMPLE_TOKEN",
        },
    ],
    ids=["environment", "oauth-client-secret"],
)
async def test_setting_a_credential_logs_its_variable_name_but_never_its_value(
    host, monkeypatch, caplog, referencing
):
    monkeypatch.setattr(ConnectionRunner, "start", lambda runner: None)
    service, _registry = await start_service(host)
    try:
        await service.management.manage("save", {"connection": referencing})
        status = await service.management.manage("status", {"id": "example"})
        assert status["missing_credentials"] == ["EXAMPLE_TOKEN"]

        with caplog.at_level(logging.INFO):
            result = await service.management.manage(
                "credential",
                {"id": "example", "key": "EXAMPLE_TOKEN", "value": "secret-sentinel"},
            )

        assert result["set"] is True
        assert (await service.management.manage("status", {"id": "example"}))[
            "missing_credentials"
        ] == []
        assert "EXAMPLE_TOKEN" in caplog.text
        assert "secret-sentinel" not in caplog.text
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_reconnect_restarts_an_enabled_connection_and_refuses_a_disabled_one(
    host, monkeypatch, caplog
):
    started: list[ConnectionRunner] = []
    monkeypatch.setattr(ConnectionRunner, "start", lambda runner: started.append(runner))
    service, registry = await start_service(host)
    try:
        await service.management.manage("save", {"connection": _CONNECTION})
        first = service.runners["example"]
        first.catalog = dict(_CATALOG)
        service._publish(first, first.catalog)
        remote = remote_tool_name("example", "get_scene_info")

        with caplog.at_level(logging.INFO):
            await service.management.manage("reconnect", {"id": "example"})

        # A new client, and for a local server a new process, replaces the old one.
        assert started == [first, service.runners["example"]]
        assert service.runners["example"] is not first
        assert "MCP connection restarted (connection=example)" in caplog.text
        # Its Tools stay registered meanwhile, not ready until it is connected again.
        assert registry.get(remote) is not None
        assert remote not in _tool_names(registry, ready_only=True)
        await service.management.manage("save", {"connection": {**_CONNECTION, "enabled": False}})
        with pytest.raises(ValueError, match="disabled"):
            await service.management.manage("reconnect", {"id": "example"})
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_connection_and_job_changes_reach_accessors_in_revision_order(host, monkeypatch):
    changes: list[tuple[str, list[str], int]] = []
    monkeypatch.setattr(ConnectionRunner, "start", lambda runner: None)
    service, _registry = await start_service(
        replace(host, publish_change=lambda *change: changes.append(change))
    )
    try:
        await service.management.manage("save", {"connection": _CONNECTION})
        runner = service.runners["example"]

        async def invoke(operation, arguments, invocation_context=None):
            return {"tools": []} if operation == "catalog" else {}

        monkeypatch.setattr(runner, "invoke", invoke)
        runner.state = "connected"
        runner.state = "connected"
        job = await service.management.manage("test", {"id": "example"})
        assert (await service.jobs.wait(job["job_id"]))["state"] == "completed"
    finally:
        await service.close()
    closed = len(changes)
    runner.state = "failed"

    # A saved connection, a new state (not a repeated one) and a finished job each
    # tell accessors what to read again; a closed service publishes nothing.
    assert [(resource, ids) for resource, ids, _revision in changes] == [
        ("connections", ["example"]),
        ("connections", ["example"]),
        ("jobs", [job["job_id"]]),
    ]
    assert [revision for *_change, revision in changes] == [1, 2, 3]
    assert len(changes) == closed


def _entry(identifier: str, **fields) -> dict:
    return {
        "id": identifier,
        "name": identifier.title(),
        "description": f"test-owned {identifier} service",
        "category": "development",
        "url": f"https://{identifier}.example.com/mcp",
        "auth": "oauth",
        **fields,
    }


def test_the_shipped_catalog_loads_completely_and_a_broken_entry_is_left_out(tmp_path):
    shipped = load_connectors()
    assert shipped.issues == ()
    assert shipped.entries

    path = tmp_path / "catalog.json"
    broken = [
        _entry("plain", url="http://plain.example.com/mcp"),
        _entry("twice"),
        _entry("twice"),
        _entry("lines", description="first\nsecond"),
        {**_entry("extra"), "unknown": True},
        # A redirect host belongs to an OAuth sign-in.
        _entry("local", auth="none", redirect_host="localhost"),
    ]
    path.write_text(json.dumps({"entries": [_entry("docs", auth="none"), *broken]}))
    catalog = load_connectors(path)

    assert [entry["id"] for entry in catalog.entries] == ["docs"]
    assert [(issue["entry"], issue["code"]) for issue in catalog.issues] == [
        ("plain", "invalid_entry"),
        ("twice", "duplicate_id"),
        ("twice", "duplicate_id"),
        ("lines", "invalid_entry"),
        ("extra", "invalid_entry"),
        ("local", "invalid_entry"),
    ]
    path.write_text("[]")
    assert load_connectors(path).issues[0]["code"] == "invalid_catalog"


@pytest.mark.asyncio
async def test_adding_from_the_catalog_creates_a_connection_and_never_replaces_one(
    host, monkeypatch, tmp_path
):
    monkeypatch.setattr(ConnectionRunner, "start", lambda runner: None)
    # A rejected record keeps its id: adding never overwrites it either.
    (host.state_dir / "connections.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "connections": [{"id": "docs_2", "transport": "http", "url": "ftp://x"}],
            }
        )
    )
    path = tmp_path / "catalog.json"
    path.write_text(
        json.dumps(
            {
                "entries": [
                    _entry("docs", auth="none"),
                    _entry(
                        "tracker",
                        read_only_url="https://tracker.example.com/mcp/readonly",
                        redirect_host="localhost",
                    ),
                ]
            }
        )
    )
    service, _registry = await start_service(host)
    service.connectors = load_connectors(path)
    mine = {"id": "docs", "transport": "http", "url": "https://docs.example.com/mcp/"}
    try:
        await service.management.manage("save", {"connection": mine})

        added = await service.management.manage("add_from_catalog", {"entry": "docs"})
        read_only = await service.management.manage(
            "add_from_catalog", {"entry": "tracker", "read_only": True}
        )

        # The first free id; the saved connection under the entry's id is untouched.
        assert added["configuration"] == {
            "id": "docs_3",
            "transport": "http",
            "url": "https://docs.example.com/mcp",
            "description": "test-owned docs service",
            "enabled": True,
            "timeout": 120,
            "sampling": "off",
            "roots": "off",
        }
        assert added["pending_requests"] == []
        assert service.connections["docs"] == validate_connection(mine)
        assert read_only["configuration"]["url"] == "https://tracker.example.com/mcp/readonly"
        assert read_only["configuration"]["oauth"] is True
        assert read_only["configuration"]["oauth_redirect_host"] == "localhost"
        assert read_only["oauth"]["signed_in"] is False
        listing = {
            entry["id"]: entry["connections"]
            for entry in (await service.management.manage("catalog", {}))["entries"]
        }
        # A trailing slash still names the same server.
        assert listing == {"docs": ["docs", "docs_3"], "tracker": ["tracker"]}

        with pytest.raises(ValueError, match="No MCP catalog entry is named missing"):
            await service.management.manage("add_from_catalog", {"entry": "missing"})
        with pytest.raises(ValueError, match="has no read-only variant"):
            await service.management.manage(
                "add_from_catalog", {"entry": "docs", "read_only": True}
            )
        assert sorted(service.connections) == ["docs", "docs_3", "tracker"]
    finally:
        await service.close()


# The Extension's modules that import the MCP SDK or its HTTP stack.
_PROTOCOL_MODULES = ("client", "_tasks", "_oauth", "_events", "_callbacks", "_network")


@pytest.mark.asyncio
async def test_the_protocol_client_loads_only_once_a_connection_exists(host):
    # A fresh copy of the Extension's package, so modules other tests loaded do not count.
    package = "mcp_lazy_load_probe"
    root = Path(resources.extensions.mcp.__file__).parent
    spec = importlib.util.spec_from_file_location(
        package, root / "__init__.py", submodule_search_locations=[str(root)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[package] = module

    def loaded() -> list[str]:
        return [name for name in _PROTOCOL_MODULES if f"{package}.{name}" in sys.modules]

    try:
        spec.loader.exec_module(module)
        assert loaded() == []
        api = ExtensionAPI(
            "mcp", ExtensionDeclarations(), config={}, logger=logging.getLogger("test.mcp")
        )
        api.operations.bind(ToolRegistry())
        service = module.extension.MCPService(api)
        await service.start(host)
        try:
            assert loaded() == []
            await service.management.manage(
                "save", {"connection": {**_CONNECTION, "enabled": False}}
            )
            assert "client" in loaded()
            assert service.runners["example"].state == "disconnected"
        finally:
            await service.close()
    finally:
        for name in [name for name in sys.modules if name.split(".")[0] == package]:
            del sys.modules[name]
