"""Mcp: execution behavior."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from core.tools.availability import ToolAccess
from resources.extensions.mcp.extension import remote_tool_name
from tests.resources.extensions.mcp_helpers import (
    context,
)
from tests.resources.extensions.mcp_helpers import (
    context_service as context_service,
)
from tests.resources.extensions.mcp_helpers import (
    host as host,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["tool", "operation"])
async def test_transport_failure_does_not_claim_remote_call_was_undone(
    context_service, host, monkeypatch, kind
):
    service, registry, runner, calls = context_service

    async def fail(*args):
        calls.append(args)
        raise ValueError("test-owned-timeout")

    monkeypatch.setattr(runner, "invoke", fail)
    target = (
        service._entries(runner, service._allowed(context(host)))[-1]["target"]
        if kind == "tool"
        else service._operation_target("logging/setLevel")
    )
    result = await registry.dispatch(
        context(host),
        {
            "action": "call",
            "target": target,
            "arguments": {"value": "sentinel"} if kind == "tool" else {"level": "debug"},
        },
    )
    assert result["error"]["code"] == "mcp_call_unconfirmed"
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["tool", "operation"])
@pytest.mark.parametrize("failure", ["storage", "media"])
async def test_result_preparation_failure_preserves_completed_call_state(
    context_service, host, monkeypatch, kind, failure
):
    service, registry, runner, calls = context_service
    if failure == "storage":

        async def unavailable(*args, **kwargs):
            raise OSError("test-owned-storage-failure")

        monkeypatch.setattr(service.content, "present", unavailable)
    else:

        async def malformed(operation, arguments, invocation_context=None):
            calls.append((operation, arguments))
            return {
                "content": [{"type": "image", "mimeType": "image/png", "data": "invalid base64"}]
            }

        monkeypatch.setattr(runner, "invoke", malformed)
    target = (
        service._entries(runner, service._allowed(context(host)))[-1]["target"]
        if kind == "tool"
        else service._operation_target("logging/setLevel")
    )
    result = await registry.dispatch(
        context(host),
        {
            "action": "call",
            "target": target,
            "arguments": {"value": "sentinel"} if kind == "tool" else {"level": "debug"},
        },
    )
    assert result["error"]["code"] == "mcp_result_unavailable"
    assert result["data"] is None
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "inputs,pointer",
    [
        ({}, "/arguments"),
        ({"value": []}, "/arguments/value"),
        ({"value": "valid", "extra": True}, "/arguments"),
    ],
)
async def test_target_validation_identifies_error_before_remote_effects(
    context_service, host, inputs, pointer
):
    service, registry, runner, calls = context_service
    target = service._entries(runner, service._allowed(context(host)))[-1]["target"]
    result = await registry.dispatch(
        context(host), {"action": "call", "target": target, "arguments": inputs}
    )
    assert result["error"]["code"] == "mcp_invalid_arguments"
    assert pointer in result["error"]["message"]
    assert calls == []


@pytest.mark.asyncio
async def test_target_validation_does_not_echo_rejected_argument_values(context_service, host):
    service, registry, runner, calls = context_service
    target = service._entries(runner, service._allowed(context(host)))[-1]["target"]
    value = {"private": 'test-owned-secret\\with"escapes\nand-newlines'}
    result = await registry.dispatch(
        context(host), {"action": "call", "target": target, "arguments": {"value": value}}
    )
    assert result["error"]["code"] == "mcp_invalid_arguments"
    assert "test-owned-secret" not in json.dumps(result)
    assert calls == []


@pytest.mark.asyncio
async def test_discovered_call_uses_validated_remote_tool(context_service, host):
    service, registry, runner, calls = context_service
    target = service._entries(runner, service._allowed(context(host)))[-1]["target"]
    result = await registry.dispatch(
        context(host), {"action": "call", "target": target, "arguments": {"value": "sentinel"}}
    )

    assert result["ok"]
    assert result["data"]["value"]["content"][0]["text"] == "sentinel"
    assert calls == [("tools/call", {"name": "inspect", "arguments": {"value": "sentinel"}})]


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", ["agent", "run", "live"])
async def test_discovered_calls_respect_all_denial_layers(context_service, host, restriction):
    service, registry, runner, calls = context_service
    original_context = context(host)
    target = service._entries(runner, service._allowed(original_context))[-1]["target"]
    remote = remote_tool_name("example", "inspect")
    if restriction == "agent":
        host.resolve_agent(None, "alice").tool_access = ToolAccess(denied=(remote,))
    elif restriction == "run":
        original_context = replace(original_context, tool_restriction=("mcp_example",))
    else:
        original_context = replace(
            original_context, tool_denial_resolver=lambda name: "denied" if name == remote else None
        )

    result = await registry.dispatch(
        original_context, {"action": "call", "target": target, "arguments": {"value": "sentinel"}}
    )

    assert not result["ok"]
    assert calls == []


@pytest.mark.asyncio
async def test_old_target_cannot_call_changed_schema(context_service, host):
    service, registry, runner, calls = context_service
    target = service._entries(runner, service._allowed(context(host)))[-1]["target"]
    runner.catalog["tools"][0]["inputSchema"]["properties"]["value"] = {"type": "integer"}
    service._publish(runner, runner.catalog)

    result = await registry.dispatch(
        context(host), {"action": "call", "target": target, "arguments": {"value": "sentinel"}}
    )

    assert not result["ok"]
    assert calls == []
