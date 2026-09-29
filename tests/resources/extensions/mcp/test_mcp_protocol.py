"""MCP: the connection runner speaks the protocol over each transport and callback."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import sys
from dataclasses import replace

import mcp_types as types
import pytest
import uvicorn
from mcp.server import Server

from core.extensions.operations import PENDING_INPUTS_RESOURCE
from resources.extensions.mcp.client import (
    ConnectionRunner,
    InvocationNotSentError,
    sampling_messages,
)
from resources.extensions.mcp.config import validate_connection
from resources.extensions.mcp.interactions import InputRequests
from tests.resources.extensions.mcp.mcp_test_support import context, runner_for, start_service

# A minimal stdio server: it answers the handshake, one plain Tool and one Tool that
# requires the task protocol, whose result it hands out through tasks/result.
_STDIO_SERVER = """import json
import sys
task = {"taskId": "task-sentinel", "status": "completed", "ttl": 1000,
    "createdAt": "2026-01-01T00:00:00Z", "lastUpdatedAt": "2026-01-01T00:00:00Z"}
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request["method"]
    params = request.get("params", {})
    result = None
    if method == "initialize":
        result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {},
            "tasks": {"requests": {"tools": {"call": {}}}}},
            "serverInfo": {"name": "tasks", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "inputSchema": {"type": "object"}},
            {"name": "long", "inputSchema": {"type": "object"},
            "execution": {"taskSupport": "required"}}]}
    elif method == "tools/call" and params["name"] == "echo":
        result = {"content": [{"type": "text", "text": params["arguments"]["value"]}]}
    elif method == "tools/call" and "task" in params:
        result = {"task": task}
    elif method == "tasks/result" and params.get("taskId") == "task-sentinel":
        result = {"content": [{"type": "text", "text": "payload-sentinel"}],
            "structuredContent": {"nested": [1, 2, 3]}, "_meta": {"preserved": True}}
    response = {"jsonrpc": "2.0", "id": request["id"]}
    if result is None:
        response["error"] = {"code": -32601, "message": "Method not found"}
    else:
        response["result"] = result
    print(json.dumps(response), flush=True)
"""


@pytest.mark.asyncio
async def test_stdio_wire_round_trips_calls_and_task_payloads_then_shuts_down(host, tmp_path):
    script = tmp_path / "server.py"
    script.write_text(_STDIO_SERVER)
    runner = ConnectionRunner(
        validate_connection(
            {"id": "stdio", "transport": "stdio", "command": sys.executable, "args": [str(script)]}
        ),
        host,
        InputRequests(),
        lambda *args: None,
    )
    try:
        async with asyncio.timeout(10):
            echoed = await runner.invoke(
                "tools/call", {"name": "echo", "arguments": {"value": "wire-sentinel"}}
            )
            # A Tool that requires tasks is started as a task; its handle stays usable.
            started = await runner.invoke("tools/call", {"name": "long", "arguments": {}})
            result = await runner.invoke("tasks/result", {"taskId": started["task"]["taskId"]})
        assert echoed["content"][0]["text"] == "wire-sentinel"
        assert result["content"][0]["text"] == "payload-sentinel"
        assert result["structuredContent"] == {"nested": [1, 2, 3]}
        assert result["_meta"] == {"preserved": True}
    finally:
        await runner.close()
    assert runner.state == "disconnected"


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "sse"])
async def test_http_transports(host, server, transport):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    app = server.streamable_http_app() if transport == "http" else server.sse_app()
    http_server = uvicorn.Server(
        uvicorn.Config(app, log_config=None, access_log=False, timeout_graceful_shutdown=1)
    )
    serving = asyncio.create_task(http_server.serve(sockets=[listener]))
    runner = ConnectionRunner(
        validate_connection(
            {
                "id": "http",
                "transport": transport,
                "url": f"http://127.0.0.1:{port}/{'mcp' if transport == 'http' else 'sse'}",
            }
        ),
        host,
        InputRequests(),
        lambda *args: None,
    )
    try:
        async with asyncio.timeout(10):
            while not http_server.started:
                if serving.done():
                    await serving
                await asyncio.sleep(0)
            result = await runner.invoke(
                "tools/call", {"name": "echo", "arguments": {"value": transport}}
            )
        assert json.loads(result["content"][0]["text"]) == {"value": transport}
    finally:
        await runner.close()
        http_server.should_exit = True
        await asyncio.wait_for(serving, 5)
        listener.close()
        await _stop_sse_shutdown_watcher()


async def _stop_sse_shutdown_watcher() -> None:
    """Cancel the shutdown watcher sse-starlette leaves behind after an in-process server.

    sse-starlette runs one watcher per Event Loop that polls until a running uvicorn
    server exits. Since 3.5.0 it looks the server up again on every poll, and once
    ``serve()`` has returned there is none, so the watcher would keep polling on the
    worker's shared Event Loop during every later test.
    """
    watchers = [
        task
        for task in asyncio.all_tasks()
        if getattr(task.get_coro(), "__qualname__", None) == "_shutdown_watcher"
    ]
    for task in watchers:
        task.cancel()
    await asyncio.gather(*watchers, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_failing_connection_logs_once_until_it_recovers(host, server, monkeypatch, caplog):
    runner = runner_for(host, server, monkeypatch)
    attempts = 0

    async def flaky_transport(stack):
        nonlocal attempts
        attempts += 1
        if attempts <= 3:
            raise OSError("server unreachable")
        return server

    monkeypatch.setattr(runner, "_transport", flaky_transport)
    caplog.set_level(logging.DEBUG, logger="vbot.extensions.mcp")
    try:
        async with asyncio.timeout(10):
            # Each call reconnects a failed connection once.
            for _ in range(3):
                with pytest.raises(InvocationNotSentError):
                    await runner.invoke("ping", {})
            await runner.invoke("ping", {})
    finally:
        await runner.close()

    # One WARNING opens the failing stretch, repeats stay at DEBUG, and the first
    # connection that comes up logs one INFO with the failure count.
    records = [record for record in caplog.records if record.name == "vbot.extensions.mcp"]
    assert [record.levelno for record in records] == [
        logging.WARNING,
        logging.DEBUG,
        logging.DEBUG,
        logging.INFO,
    ]
    assert "failures=3" in records[-1].getMessage()


@pytest.mark.asyncio
async def test_modern_input_required_round_trips_all_callbacks(host, monkeypatch):
    responses = []

    async def call(server_context, params):
        if params.input_responses:
            responses.append(params.input_responses)
            return types.CallToolResult(content=[types.TextContent(type="text", text="completed")])
        return types.InputRequiredResult.model_validate(
            {
                "resultType": "input_required",
                "inputRequests": {
                    "sample": {
                        "method": "sampling/createMessage",
                        "params": {
                            "messages": [
                                {"role": "user", "content": {"type": "text", "text": "sample-this"}}
                            ],
                            "maxTokens": 20,
                        },
                    },
                    "roots": {"method": "roots/list", "params": {}},
                    "input": {
                        "method": "elicitation/create",
                        "params": {
                            "message": "test-owned-question",
                            "requestedSchema": {
                                "type": "object",
                                "properties": {"name": {"type": "string"}},
                                "required": ["name"],
                            },
                        },
                    },
                },
            }
        )

    async def list_tools(server_context, params):
        return types.ListToolsResult(
            tools=[types.Tool(name="callbacks", input_schema={"type": "object"})]
        )

    server = Server("callbacks", on_call_tool=call, on_list_tools=list_tools)
    runner = runner_for(host, server, monkeypatch)
    task = asyncio.create_task(runner.invoke("tools/call", {"name": "callbacks"}, context(host)))
    try:
        async with asyncio.timeout(10):
            while not runner.inputs.list():
                if task.done():
                    await task
                await asyncio.sleep(0)
            pending = runner.inputs.list()[0]
            runner.inputs.respond(
                pending["id"], {"action": "accept", "content": {"name": "user-sentinel"}}
            )
            result = await task
        assert result["content"][0]["text"] == "completed"
        assert responses[0]["sample"].content.text == "sampled"
        assert str(responses[0]["roots"].roots[0].uri) == host.data_dir.as_uri()
        assert responses[0]["input"].content == {"name": "user-sentinel"}
    finally:
        task.cancel()
        await runner.close()


@pytest.mark.asyncio
async def test_legacy_server_sampling_and_roots(host, monkeypatch):
    from mcp import Client

    async def call(server_context, params):
        roots = await server_context.session.list_roots()
        sample = await server_context.session.create_message(
            [
                types.SamplingMessage(
                    role="user", content=types.TextContent(type="text", text="sample")
                )
            ],
            max_tokens=20,
        )
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text",
                    text=json.dumps(
                        {"root": str(roots.roots[0].uri), "sample": sample.content.text}
                    ),
                )
            ]
        )

    async def list_tools(server_context, params):
        return types.ListToolsResult(
            tools=[types.Tool(name="callbacks", input_schema={"type": "object"})]
        )

    def legacy_client(*args, **kwargs):
        kwargs["mode"] = "legacy"
        return Client(*args, **kwargs)

    monkeypatch.setattr("resources.extensions.mcp.client.Client", legacy_client)
    runner = runner_for(
        host, Server("legacy", on_call_tool=call, on_list_tools=list_tools), monkeypatch
    )
    try:
        result = await asyncio.wait_for(
            runner.invoke("tools/call", {"name": "callbacks"}, context(host)), 10
        )
        assert json.loads(result["content"][0]["text"]) == {
            "root": host.data_dir.as_uri(),
            "sample": "sampled",
        }
    finally:
        await runner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("retired", [False, True])
async def test_input_response_is_validated_and_not_retained(host, retired):
    changes = []

    def publish(*change):
        if retired:
            raise ValueError("extension change is unavailable")
        changes.append(change)

    service, _ = await start_service(replace(host, publish_change=publish))
    inputs = service.inputs
    task = asyncio.create_task(
        inputs.request(
            "example",
            "elicitation",
            {
                "requestedSchema": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                }
            },
            "session",
        )
    )
    await asyncio.sleep(0)
    pending = inputs.list()[0]
    with pytest.raises(ValueError):
        inputs.respond(pending["id"], {"action": "accept", "content": {}})
    inputs.respond(pending["id"], {"action": "accept", "content": {"name": "answer"}})
    assert (await task)["content"] == {"name": "answer"}
    assert inputs.list() == []
    # Accessors read the pending inputs again when one is added and when it leaves;
    # a retired registration publishes nothing and still answers.
    expected = [(PENDING_INPUTS_RESOURCE, [pending["id"]], revision) for revision in (1, 2)]
    assert changes == ([] if retired else expected)
    await service.close()


def test_sampling_rejects_unknown_content_instead_of_losing_it():
    with pytest.raises(ValueError):
        sampling_messages({"messages": [{"role": "user", "content": {"type": "future-data"}}]})
