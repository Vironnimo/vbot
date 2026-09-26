"""MCP connections use the shared Tool opt-in, including temporary Agents."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from core.tools.availability import ToolAccess, resolve_tool_access
from core.tools.tools import ToolNotAllowedError
from resources.extensions.mcp.client import Invocation
from resources.extensions.mcp.config import validate_connection
from resources.extensions.mcp.extension import remote_tool_name
from tests.resources.extensions.mcp_helpers import context, targets
from tests.resources.extensions.mcp_helpers import context_service as context_service
from tests.resources.extensions.mcp_helpers import host as host


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "agent_id,project_id", [("alice", None), ("alice", "studio"), ("tmp_peer", None)]
)
@pytest.mark.parametrize(
    "policy,enabled",
    [
        (ToolAccess(), False),
        (ToolAccess(mode="selected", allowed=("mcp_example",)), False),
        (ToolAccess(granted=("mcp_example",)), True),
        (ToolAccess(mode="selected", allowed=("mcp_example",), granted=("mcp_example",)), True),
        (ToolAccess(mode="selected", allowed=(), granted=("mcp_example",)), False),
        (ToolAccess(mode="none", granted=("mcp_example",)), False),
        (ToolAccess(granted=("mcp_example",), denied=("mcp_example",)), False),
    ],
)
async def test_connection_policy_controls_definitions_and_real_dispatch(
    context_service, host, agent_id, project_id, policy, enabled
):
    service, registry, runner, calls = context_service
    host.resolve_agent(None, "alice").tool_access = policy
    ctx = context(host, agent_id, project_id)
    allowed = resolve_tool_access(policy, registry.list_tools(), "off").allowed_tools
    assert bool(registry.provider_definitions(allowed)) is enabled
    remote = remote_tool_name("example", "inspect")
    assert (remote in allowed) is enabled
    if not enabled:
        with pytest.raises(ToolNotAllowedError):
            await registry.dispatch(ctx, {"action": "search"}, allowed)
        with pytest.raises(ToolNotAllowedError):
            await registry.dispatch(replace(ctx, tool_name=remote), {"value": "sentinel"}, allowed)
        assert calls == []
        return
    result = await registry.dispatch(ctx, {"action": "search", "kind": "tool"}, allowed)
    target = targets(result)[0]
    result = await registry.dispatch(
        ctx, {"action": "call", "target": target, "arguments": {"value": "sentinel"}}, allowed
    )
    assert result["ok"]
    assert calls == [("tools/call", {"name": "inspect", "arguments": {"value": "sentinel"}})]


@pytest.mark.asyncio
async def test_direct_remote_dispatch_rechecks_revoked_parent(context_service, host):
    service, registry, runner, calls = context_service
    stale_allowed = service._allowed(context(host))
    host.resolve_agent(None, "alice").tool_access = ToolAccess()
    result = await registry.dispatch(
        replace(context(host), tool_name=remote_tool_name("example", "inspect")),
        {"value": "sentinel"},
        stale_allowed,
    )
    assert result["error"]["code"] == "mcp_access_denied"
    assert calls == []


@pytest.mark.asyncio
async def test_same_name_in_other_project_does_not_inherit_opt_in(context_service, host):
    service, registry, runner, calls = context_service
    granted = host.resolve_agent(None, "alice")
    blocked = SimpleNamespace(tool_access=ToolAccess(), memory_prompt_mode="off", workspace="")
    service.host = replace(
        host, resolve_tool_agent=lambda ctx: granted if ctx.project_id is None else blocked
    )
    assert "mcp_example" in service._allowed(context(host))
    assert "mcp_example" not in service._allowed(context(host, project="other"))


def test_connection_config_rejects_removed_agent_list():
    with pytest.raises(ValueError):
        validate_connection(
            {"id": "example", "transport": "stdio", "command": "unused", "agents": []}
        )


@pytest.mark.asyncio
async def test_queued_call_rechecks_access_before_remote_effect(context_service, host, monkeypatch):
    service, registry, runner, calls = context_service
    started = asyncio.Event()
    release = asyncio.Event()

    async def perform(operation, arguments):
        calls.append(arguments)
        started.set()
        await release.wait()
        return {}

    monkeypatch.setattr(runner, "_perform_with_retries", perform)
    runner.state = "disconnected"
    first = asyncio.get_running_loop().create_future()
    second = asyncio.get_running_loop().create_future()
    await runner._queue.put(Invocation("tools/call", {"value": "first"}, context(host), first))
    await runner._queue.put(Invocation("tools/call", {"value": "second"}, context(host), second))
    await runner._queue.put(None)
    worker = asyncio.create_task(runner._serve())
    try:
        await asyncio.wait_for(started.wait(), 2)
        host.resolve_agent(None, "alice").tool_access = ToolAccess()
        release.set()
        assert await asyncio.wait_for(first, 2) == {}
        with pytest.raises(ValueError):
            await asyncio.wait_for(second, 2)
        await asyncio.wait_for(worker, 2)
        assert calls == [{"value": "first"}]
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
