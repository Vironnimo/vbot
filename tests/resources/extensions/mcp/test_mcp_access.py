"""MCP connections use the shared Tool opt-in, including temporary Agents."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from core.tools.availability import ToolAccess, resolve_tool_access
from core.tools.tools import ToolNotAllowedError
from resources.extensions.mcp import client as mcp_client
from resources.extensions.mcp.client import InvocationNotSentError
from resources.extensions.mcp.extension import remote_tool_name
from tests.resources.extensions.mcp.mcp_test_support import (
    allowed_tools,
    context,
    dispatch,
    model_text,
    runner_for,
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


def _disable(service):
    service.connections["example"] = {**service.connections["example"], "enabled": False}


def _remove(service):
    del service.connections["example"]


def _stop(service):
    service._closed = True


_DISABLED = (
    "Error (mcp_access_denied): The MCP connection example is disabled, so nothing was "
    "run. Tell the user to enable it in Settings -> Integrations -> Extensions -> MCP "
    "connections if it is needed.\nretryable: false"
)
_REMOVED = (
    "Error (mcp_request_failed): The MCP connection example was removed, so nothing was run. "
    "Tell the user if it is needed.\nretryable: false"
)
_STOPPED = (
    "Error (mcp_request_failed): The MCP Extension is not running, so nothing was run. Try "
    "once more, and if it fails again, tell the user that the MCP Extension is not running."
    "\nretryable: true"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "while_connecting", "expected"),
    [
        pytest.param(_disable, True, _DISABLED, id="disabled-while-connecting"),
        pytest.param(_remove, True, _REMOVED, id="removed-while-connecting"),
        pytest.param(_stop, True, _STOPPED, id="stopped-while-connecting"),
        pytest.param(_stop, False, _STOPPED, id="stopped-before-the-call"),
    ],
)
async def test_connection_gone_before_any_effect_names_what_the_user_can_do(
    context_service, host, monkeypatch, change, while_connecting, expected
):
    # A connection disabled or removed, or an Extension stopped, runs nothing, so
    # none of them may read as an access denial or an unknown outcome.
    service, registry, runner, calls = context_service
    runner.state = "connecting"

    async def connect(*args):
        change(service)
        runner.state = "connected"

    monkeypatch.setattr(runner, "invoke", connect)
    if not while_connecting:
        change(service)
    result = await dispatch(registry, host, {"action": "search"})

    assert model_text(result) == expected
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
async def test_queued_call_rechecks_access_before_remote_effect(
    context_service, host, server, monkeypatch
):
    service, registry, _, _ = context_service
    # One call at a time, so the second waits in the queue while access changes.
    monkeypatch.setattr(mcp_client, "CONNECTION_CONCURRENCY", 1)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    @server.tool()
    async def hold(value: str) -> str:
        calls.append(value)
        started.set()
        await release.wait()
        return value

    runner = runner_for(host, server, monkeypatch)
    runner._authorize = service._authorize

    def call(value):
        return asyncio.create_task(
            runner.invoke(
                "tools/call", {"name": "hold", "arguments": {"value": value}}, context(host)
            )
        )

    first = call("first")
    second = None
    try:
        async with asyncio.timeout(5):
            await started.wait()
            second = call("second")
            while runner._queue.empty():
                await asyncio.sleep(0)
            host.resolve_agent(None, "alice").tool_access = ToolAccess()
            release.set()
            assert (await first)["content"][0]["text"] == "first"
            with pytest.raises(InvocationNotSentError) as denied:
                await second
        assert denied.value.denied
        assert calls == ["first"]
    finally:
        for task in (first, second):
            if task is not None:
                task.cancel()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
        await runner.close()
