"""MCP connections use the shared Tool opt-in, including temporary Agents."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from core.tools.availability import ToolAccess, resolve_tool_access
from core.tools.tools import ToolNotAllowedError
from resources.extensions.mcp.client import Invocation
from resources.extensions.mcp.extension import remote_tool_name
from tests.resources.extensions.mcp.mcp_test_support import (
    allowed_tools,
    context,
    dispatch,
    targets,
)

_GRANTED = ("mcp_example",)


# The mode, grant and denial combinations themselves are core Tool access rules
# (tests/core/tools/test_availability.py); these rows check that the connection Tool
# is an opt-in Tool whose remote Tools follow it, for ordinary and temporary Agents.
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_id", "project_id", "policy", "enabled"),
    [
        ("alice", None, ToolAccess(), False),
        ("alice", None, ToolAccess(mode="selected", allowed=_GRANTED), False),
        ("alice", "studio", ToolAccess(granted=_GRANTED), True),
        ("tmp_peer", None, ToolAccess(mode="selected", allowed=_GRANTED, granted=_GRANTED), True),
        ("alice", None, ToolAccess(granted=_GRANTED, denied=_GRANTED), False),
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
async def test_tools_published_later_follow_the_grant_and_explicit_denials_win(context_service):
    service, registry, runner, calls = context_service
    denied = remote_tool_name("example", "denied")
    policy = ToolAccess(mode="selected", allowed=_GRANTED, granted=_GRANTED, denied=(denied,))

    service._publish(
        runner,
        {
            "tools": [
                {"name": name, "inputSchema": {"type": "object"}}
                for name in ("inspect", "new", "denied")
            ]
        },
    )

    allowed = resolve_tool_access(policy, registry.list_tools(), "off").allowed_tools
    assert remote_tool_name("example", "new") in allowed
    assert denied not in allowed


@pytest.mark.asyncio
async def test_direct_remote_dispatch_rechecks_revoked_parent(context_service, host):
    service, registry, runner, calls = context_service
    stale_allowed = allowed_tools(registry, host)
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

    own = await dispatch(registry, host, {"action": "search"})
    other = await dispatch(registry, host, {"action": "search"}, project="other")

    assert own["ok"]
    assert other["error"]["code"] == "mcp_access_denied"


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
