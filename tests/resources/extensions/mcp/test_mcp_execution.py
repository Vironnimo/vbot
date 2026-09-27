"""MCP: calls of discovered targets validate, repair and execute exactly once."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from core.tools.availability import ToolAccess
from resources.extensions.mcp.extension import remote_tool_name
from tests.resources.extensions.mcp.mcp_test_support import (
    allowed_tools,
    context,
    dispatch,
    operation_target,
    tool_target,
)


async def _call(registry, host, target, arguments, ctx=None):
    call = {"action": "call", "target": target, "arguments": arguments}
    if ctx is None:
        return await dispatch(registry, host, call)
    return await registry.dispatch(ctx, call, allowed_tools=["mcp_example"])


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
    if kind == "tool":
        target, arguments = await tool_target(registry, host), {"value": "sentinel"}
    else:
        target = await operation_target(registry, host, "logging/setLevel")
        arguments = {"level": "debug"}

    result = await _call(registry, host, target, arguments)

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

    result = await _call(registry, host, await tool_target(registry, host), inputs)

    assert result["error"]["code"] == "mcp_invalid_arguments"
    assert pointer in result["error"]["message"]
    assert calls == []


@pytest.mark.asyncio
async def test_target_validation_does_not_echo_rejected_argument_values(context_service, host):
    service, registry, runner, calls = context_service
    value = {"private": 'test-owned-secret\\with"escapes\nand-newlines'}

    result = await _call(registry, host, await tool_target(registry, host), {"value": value})

    assert result["error"]["code"] == "mcp_invalid_arguments"
    assert "test-owned-secret" not in json.dumps(result)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", ["agent", "run", "live"])
async def test_discovered_calls_respect_all_denial_layers(context_service, host, restriction):
    service, registry, runner, calls = context_service
    original_context = context(host)
    target = await tool_target(registry, host)
    remote = remote_tool_name("example", "inspect")
    if restriction == "agent":
        host.resolve_agent(None, "alice").tool_access = ToolAccess(
            granted=("mcp_example",), denied=(remote,)
        )
    elif restriction == "run":
        original_context = replace(original_context, tool_restriction=("mcp_example",))
    else:
        original_context = replace(
            original_context, tool_denial_resolver=lambda name: "denied" if name == remote else None
        )

    result = await _call(registry, host, target, {"value": "sentinel"}, original_context)

    assert not result["ok"]
    assert calls == []


@pytest.mark.asyncio
async def test_target_repairs_recognizable_values_without_changing_the_callers_arguments(
    context_service, host
):
    service, registry, runner, calls = context_service
    runner.catalog["tools"][0]["inputSchema"]["properties"] = {
        "value": {"type": "string"},
        "enabled": {"type": "boolean"},
        "count": {"type": "integer"},
        "labels": {"type": "array", "items": {"type": "string"}},
        "metadata": {"type": "object"},
    }
    service._publish(runner, runner.catalog)
    inputs = {
        "value": 7,
        "enabled": "yes",
        "count": "3.0",
        "labels": "one",
        "metadata": {"Enabled": "FALSE", "request": {"operation": "original"}},
    }
    original = json.dumps(inputs)

    result = await _call(registry, host, await tool_target(registry, host), inputs)

    assert result["ok"], result
    assert calls[0][1]["arguments"] == {
        "value": "7",
        "enabled": True,
        "count": 3,
        "labels": ["one"],
        "metadata": {"Enabled": "FALSE", "request": {"operation": "original"}},
    }
    assert json.dumps(inputs) == original


@pytest.mark.asyncio
async def test_target_conflicting_aliases_do_not_call_remote(context_service, host):
    service, registry, runner, calls = context_service

    result = await _call(
        registry, host, await tool_target(registry, host), {"value": "first", "VALUE": "second"}
    )

    assert result["error"]["code"] == "mcp_invalid_arguments"
    assert calls == []


@pytest.mark.asyncio
async def test_protocol_operation_repairs_enum_spelling(context_service, host):
    service, registry, runner, calls = context_service
    target = await operation_target(registry, host, "logging/setLevel")

    result = await _call(registry, host, target, {"LEVEL": "DEBUG"})

    assert result["ok"], result
    assert calls == [("logging/setLevel", {"level": "debug"})]


@pytest.mark.asyncio
@pytest.mark.parametrize("inputs", [{"request": {"value": "sentinel"}}, {"vlaue": "sentinel"}])
async def test_remote_arguments_need_owner_evidence_for_field_aliases(
    context_service, host, inputs
):
    service, registry, runner, calls = context_service

    result = await _call(registry, host, await tool_target(registry, host), inputs)

    assert result["error"]["code"] == "mcp_invalid_arguments"
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("direct", [False, True])
async def test_remote_valid_application_payload_is_preserved(context_service, host, direct):
    service, registry, runner, calls = context_service
    schema = runner.catalog["tools"][0]["inputSchema"]
    schema["additionalProperties"] = True
    schema["properties"]["settings"] = {
        "type": "object",
        "properties": {"color": {"type": "string"}},
    }
    service._publish(runner, runner.catalog)
    inputs = {
        "value": "first",
        "VALUE": "second",
        "settings": {"colors": "blue", "color": "red"},
        "request": {"operation": "keep"},
    }
    if direct:
        result = await registry.dispatch(
            replace(context(host), tool_name=remote_tool_name(runner.id, "inspect")),
            inputs,
            allowed_tools=allowed_tools(registry, host),
        )
    else:
        result = await _call(registry, host, await tool_target(registry, host), inputs)
    assert result["ok"], result
    assert calls == [("tools/call", {"name": "inspect", "arguments": inputs})]
