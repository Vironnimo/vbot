"""MCP: connection management keeps Tools and runners consistent while connections change."""

from __future__ import annotations

import asyncio
import logging

import pytest

from core.tools.tools import tool_success
from resources.extensions.mcp.client import ConnectionRunner
from resources.extensions.mcp.config import validate_connection
from tests.resources.extensions.mcp.mcp_test_support import dispatch, start_service


def _tool_names(registry) -> list[str]:
    return [tool.name for tool in registry.list_tools()]


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
        release.set()
        _, result = await asyncio.wait_for(asyncio.gather(saving, accessing), 1)
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
