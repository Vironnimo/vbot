"""MCP: calls of discovered targets validate, repair and execute exactly once."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import replace

import pytest

from core.tools.availability import ToolAccess
from resources.extensions.mcp import client as mcp_client
from resources.extensions.mcp.client import ConnectionRunner
from resources.extensions.mcp.extension import remote_tool_name
from tests.resources.extensions.mcp.mcp_test_support import (
    allowed_tools,
    context,
    dispatch,
    model_text,
    operation_target,
    tool_target,
)


async def _call(registry, host, target, arguments, ctx=None):
    call = {"action": "call", "target": target, "arguments": arguments}
    if ctx is None:
        return await dispatch(registry, host, call)
    return await registry.dispatch(ctx, call, allowed_tools=["mcp_example"])


@pytest.mark.asyncio
@pytest.mark.parametrize("waiting", ["connection", "queue", "roots"])
async def test_schema_change_before_transmission_never_sends_prepared_call(
    context_service, host, server, monkeypatch, waiting
):
    service, registry, runner, _ = context_service
    monkeypatch.setattr(mcp_client, "CONNECTION_CONCURRENCY", 1)
    entered, release = asyncio.Event(), asyncio.Event()
    effects = []

    @server.tool()
    def mutate(value: str) -> str:
        effects.append(value)
        return value

    @server.tool()
    async def hold() -> str:
        entered.set()
        await release.wait()
        return "done"

    async def transport(stack):
        return server

    monkeypatch.setattr(runner._transport, "open", transport)
    monkeypatch.setattr(runner, "invoke", ConnectionRunner.invoke.__get__(runner))
    pending = []
    try:
        async with asyncio.timeout(5):
            await runner.invoke("catalog", {})
            if waiting == "queue":
                pending.append(asyncio.create_task(runner.invoke("tools/call", {"name": "hold"})))
                await entered.wait()
            elif waiting == "connection":
                connected = runner._connected

                async def connect():
                    entered.set()
                    await release.wait()
                    return await connected()

                monkeypatch.setattr(runner, "_connected", connect)
            else:

                async def roots_changed():
                    entered.set()
                    await release.wait()

                monkeypatch.setattr(runner._protocol, "_notify_roots_changed", roots_changed)

            ctx = replace(context(host), tool_name=remote_tool_name("example", "mutate"))
            call = asyncio.create_task(
                registry.dispatch(ctx, {"value": "prepared"}, allowed_tools(registry, host))
            )
            pending.append(call)
            if waiting == "queue":
                while runner._queue.empty():
                    await asyncio.sleep(0)
            else:
                await entered.wait()
            runner.catalog = deepcopy(runner.catalog)
            tool = next(item for item in runner.catalog["tools"] if item["name"] == "mutate")
            tool["inputSchema"]["properties"]["value"]["description"] = "Changed meaning"
            service._publish(runner, runner.catalog)
            release.set()
            result = await call
            assert result["error"]["code"] == "mcp_tool_changed"
            assert effects == []
            assert (
                await registry.dispatch(ctx, {"value": "fresh"}, allowed_tools(registry, host))
            )["ok"]
            assert effects == ["fresh"]
    finally:
        release.set()
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


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

    detail = {
        "storage": "OSError: test-owned-storage-failure",
        "media": "Error: Only base64 data is allowed",
    }[failure]
    assert model_text(result) == (
        "Error (mcp_result_unavailable): The MCP server answered, but vBot could not save or "
        f"prepare its answer: {detail}. The call ran, and whether it succeeded is unknown. "
        "Before you repeat a call that changes something, check the application's current "
        "state. A call that only reads is safe to repeat."
    )
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
