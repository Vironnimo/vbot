"""MCP: connection management keeps Tools and runners consistent while connections change."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace

import pytest

from core.database import write_bootstrap_marker
from core.extensions import ExtensionRegistrationIdentity
from core.extensions.databases import ExtensionDatabases
from core.tools.tools import tool_success
from resources.extensions.mcp.client import ConnectionRunner
from resources.extensions.mcp.config import validate_connection
from resources.extensions.mcp.extension import remote_tool_name
from tests.resources.extensions.mcp.mcp_test_support import dispatch, start_service

_USAGE = "Describe a tool for its arguments schema, then call it."
_NO_TOOLS = "No tools reported yet; search lists them."
_CONNECTION = {"id": "example", "transport": "stdio", "command": "unused"}
_CATALOG = {
    "server_info": {"name": "blender-mcp", "title": "Blender"},
    "tools": [
        {"name": "get_scene_info", "inputSchema": {"type": "object"}},
        {"name": "execute_blender_code", "inputSchema": {"type": "object"}},
    ],
}


def _tool_names(registry) -> list[str]:
    return [tool.name for tool in registry.list_tools()]


def _description(registry) -> str:
    return str(registry.get("mcp_example").description)


@pytest.mark.asyncio
async def test_disabling_a_connection_that_ignores_cancellation_still_removes_its_tools(
    host, monkeypatch, caplog
):
    service, registry = await start_service(host)
    service.connections["example"] = validate_connection(
        {"id": "example", "transport": "stdio", "command": "python"}
    )
    runner = service._runner(service.connections["example"])
    runner.state = "connected"
    service._publish(runner, {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]})
    assert "mcp_example" in _tool_names(registry)
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

    try:
        with caplog.at_level(logging.WARNING, logger="vbot.extensions.mcp"):
            disabling = asyncio.create_task(service.manage("disable", {"id": "example"}))
            done, _ = await asyncio.wait({disabling}, timeout=5)
            assert done, "disabling must not wait forever on a connection ignoring cancellation"
            await disabling

        assert "mcp_example" not in _tool_names(registry)
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
    await service.manage("save", {"connection": connection})
    await service.manage(
        "save", {"connection": {**connection, "id": "other", "command": "other-command"}}
    )
    closing = asyncio.Event()
    release = asyncio.Event()

    async def close():
        closing.set()
        await release.wait()

    monkeypatch.setattr(service.runners["example"], "close", close)
    replacement = {**connection, "command": "new-command"}
    saving = asyncio.create_task(service.manage("save", {"connection": replacement}))
    accessing = None
    try:
        await asyncio.wait_for(closing.wait(), 1)
        entered = asyncio.Event()

        async def concurrent_access():
            entered.set()
            if access == "connect":
                return await service.manage("connect", {"id": "example"})
            return await dispatch(registry, host, {"action": "search"})

        accessing = asyncio.create_task(concurrent_access())
        await asyncio.wait_for(entered.wait(), 1)
        assert not accessing.done()
        other = await asyncio.wait_for(service.manage("connect", {"id": "other"}), 1)
        assert other["configuration"]["command"] == "other-command"
        # Saving another connection does not wait for this one to close.
        changed = {**connection, "id": "other", "command": "changed-command"}
        other = await asyncio.wait_for(service.manage("save", {"connection": changed}), 1)
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
async def test_description_only_save_republishes_without_reconnecting(host, monkeypatch):
    started = []
    monkeypatch.setattr(ConnectionRunner, "start", lambda runner: started.append(runner))
    service, registry = await start_service(host)
    try:
        await service.manage("save", {"connection": _CONNECTION})
        runner = service.runners["example"]
        runner.catalog = dict(_CATALOG)
        service._publish(runner, runner.catalog)
        described = {**_CONNECTION, "description": "  Studio Blender.  "}

        await service.manage("save", {"connection": described})

        assert service.runners["example"] is runner
        assert started == [runner]
        assert service.store.load()["example"]["description"] == "Studio Blender."
        assert _description(registry) == (
            f"MCP connection example: Studio Blender. {_USAGE} "
            "Tools: get_scene_info, execute_blender_code"
        )
        assert registry.get(remote_tool_name("example", "get_scene_info")) is not None

        # Any other change replaces the connection; its Tool names stay until a new catalog.
        await service.manage("save", {"connection": {**_CONNECTION, "command": "other"}})

        assert service.runners["example"] is not runner
        assert _description(registry) == (
            f"MCP connection example: Blender. {_USAGE} Tools: get_scene_info, execute_blender_code"
        )
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
        await service.manage("save", {"connection": _CONNECTION})
        assert _description(registry) == f"MCP connection example. {_USAGE} {_NO_TOOLS}"
        service._publish(service.runners["example"], _CATALOG)
        known = _description(registry)
        assert known.endswith("Tools: get_scene_info, execute_blender_code")
        await stop(service)

        service, registry = await restart()
        assert _description(registry) == known
        await service.manage("remove", {"id": "example"})
        await stop(service)

        service, registry = await restart()
        await service.manage("save", {"connection": _CONNECTION})
        assert _description(registry) == f"MCP connection example. {_USAGE} {_NO_TOOLS}"
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
        await service.manage("save", {"connection": referencing})

        with caplog.at_level(logging.INFO):
            result = await service.manage(
                "credential",
                {"id": "example", "key": "EXAMPLE_TOKEN", "value": "secret-sentinel"},
            )

        assert result["set"] is True
        assert "EXAMPLE_TOKEN" in caplog.text
        assert "secret-sentinel" not in caplog.text
    finally:
        await service.close()
