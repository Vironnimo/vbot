"""Live Tools: only the Agents of a Live call get them, and calls reach that call."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

from core.tools import ToolRegistry
from core.tools._tool_context import ToolContext
from core.tools.live import LIVE_TOOL_NAMES, LiveToolHosts, register_live_tools


class FakeCall:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def run_live_tool(self, name: str, arguments: Any) -> dict[str, Any]:
        self.calls.append((name, arguments))
        return {"ok": True, "data": {"content": f"ran {name}"}}

    async def vbot_request(self, request: str | None) -> dict[str, Any]:
        self.calls.append(("vbot_request", request))
        return {"ok": True, "data": {"content": "answered"}}


def _registry() -> tuple[ToolRegistry, LiveToolHosts]:
    registry, hosts = ToolRegistry(), LiveToolHosts()
    register_live_tools(registry, hosts)
    return registry, hosts


async def _run(registry: ToolRegistry, name: str, session_id: str, arguments: Any) -> Any:
    context = ToolContext(
        agent_id="live-voice",
        session_id=session_id,
        run_id="run-1",
        tool_call_id="call-1",
        tool_name=name,
        tool_call_index=0,
        workspace=Path("."),
        vbot_root=Path("."),
        data_root=Path("."),
    )
    result = registry.get(name).handler(context, arguments)
    return await result if inspect.isawaitable(result) else result


def test_live_tools_need_an_opt_in_and_a_live_call_policy() -> None:
    registry, _hosts = _registry()

    for name in (*LIVE_TOOL_NAMES, "vbot_request"):
        tool = registry.get(name)
        assert tool.requires_opt_in
        assert tool.constraints == ("live_call",)
    # Only the Live voice Agent's call adds vbot_request; Settings never lists it.
    assert not registry.get("vbot_request").catalog_visible


@pytest.mark.asyncio
async def test_calls_reach_the_call_their_session_is_bound_to() -> None:
    registry, hosts = _registry()
    call = FakeCall()
    hosts.bind("voice-1", call)

    ran = await _run(registry, "overview", "voice-1", {"agent": "Coder"})
    asked = await _run(registry, "vbot_request", "voice-1", {"request": "  Open s1  "})
    unworded = await _run(registry, "vbot_request", "voice-1", {"request": " "})

    assert (ran, asked) == (
        {"ok": True, "data": {"content": "ran overview"}},
        {"ok": True, "data": {"content": "answered"}},
    )
    assert unworded["ok"] is True
    # Arguments reach the call as the Model wrote them; a blank request is no request.
    assert call.calls == [
        ("overview", {"agent": "Coder"}),
        ("vbot_request", "Open s1"),
        ("vbot_request", None),
    ]


@pytest.mark.asyncio
async def test_a_session_without_a_running_call_gets_a_failure_instead_of_a_result() -> None:
    registry, hosts = _registry()
    call = FakeCall()
    hosts.bind("voice-1", call)
    hosts.unbind("voice-1", call)

    for name in ("overview", "vbot_request"):
        result = await _run(registry, name, "voice-1", {})
        assert result["ok"] is False
        assert result["error"]["code"] == "no_live_call"
    assert call.calls == []
