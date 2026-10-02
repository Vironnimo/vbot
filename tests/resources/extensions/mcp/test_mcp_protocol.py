"""MCP: the connection runner speaks the protocol over each transport and callback."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import socket
import sys
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

import mcp.types as types
import pytest
import uvicorn
from mcp.client.auth import OAuthFlowError
from mcp.server import Server
from mcp.shared.exceptions import MCPDeprecationWarning

from core.extensions.operations import PENDING_INPUTS_RESOURCE
from resources.extensions.mcp import client as mcp_client
from resources.extensions.mcp._callbacks import sampling_messages
from resources.extensions.mcp.client import ConnectionRunner, InvocationNotSentError
from resources.extensions.mcp.config import validate_connection
from resources.extensions.mcp.interactions import InputRequests
from tests.resources.extensions.mcp.mcp_test_support import (
    StreamServer,
    context,
    runner_for,
    start_service,
)

# A minimal stdio server: it answers the handshake, one plain Tool and one Tool that
# requires the task protocol, whose result it hands out through tasks/result. It
# also writes a line that is neither UTF-8 nor JSON.
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
        # A stray line that is neither UTF-8 nor JSON, as from a noisy server.
        sys.stdout.buffer.write(b"\\xff stray output\\n")
        sys.stdout.flush()
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
        assert [event["kind"] for event in runner.events()["events"]].count("transport_error") == 1
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


class _ExpiringSessions:
    """ASGI middleware that ends sessions the way a restarted legacy server does."""

    def __init__(self, app):
        self.app = app
        self.expired: set[bytes] = set()
        self.current: bytes | None = None

    async def __call__(self, scope, receive, send):
        session = dict(scope.get("headers", [])).get(b"mcp-session-id")
        if session is not None:
            self.current = session
            if session in self.expired:
                body = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "Session not found"},
                }
                await send(
                    {
                        "type": "http.response.start",
                        "status": 404,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send({"type": "http.response.body", "body": json.dumps(body).encode()})
                return
        await self.app(scope, receive, send)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["tools/call", "resources/read"])
async def test_an_ended_http_session_reconnects_without_replaying_a_mutation(
    host, server, monkeypatch, operation
):
    from mcp import Client

    def legacy_client(*args, **kwargs):
        kwargs["mode"] = "legacy"
        return Client(*args, **kwargs)

    async def sleep(delay):
        pass

    monkeypatch.setattr("resources.extensions.mcp.client.Client", legacy_client)
    monkeypatch.setattr(mcp_client, "_sleep", sleep)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    sessions = _ExpiringSessions(server.streamable_http_app())
    http_server = uvicorn.Server(
        uvicorn.Config(sessions, log_config=None, access_log=False, timeout_graceful_shutdown=1)
    )
    serving = asyncio.create_task(http_server.serve(sockets=[listener]))
    runner = ConnectionRunner(
        validate_connection(
            {"id": "http", "transport": "http", "url": f"http://127.0.0.1:{port}/mcp"}
        ),
        host,
        InputRequests(),
        lambda *args: None,
    )
    echo = {"name": "echo", "arguments": {"value": "after"}}
    try:
        async with asyncio.timeout(10):
            while not http_server.started:
                if serving.done():
                    await serving
                await asyncio.sleep(0)
            await runner.invoke("tools/call", echo)
            assert sessions.current is not None
            sessions.expired.add(sessions.current)
            if operation == "tools/call":
                # Refused without being processed: reported as never sent, not repeated.
                with pytest.raises(InvocationNotSentError) as refused:
                    await runner.invoke("tools/call", echo, context(host))
                assert str(refused.value) == (
                    "Connection lost: the MCP server ended the session of this connection"
                )
                result = await runner.invoke("tools/call", echo)
                assert json.loads(result["content"][0]["text"]) == {"value": "after"}
            else:
                # A read runs again on the new session.
                result = await runner.invoke("resources/read", {"uri": "test://scene"})
                assert result["contents"][0]["text"] == "test-owned-scene"
        assert sessions.current not in sessions.expired
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
@pytest.mark.parametrize(
    "failure",
    [OSError("server unreachable"), OAuthFlowError("Protected resource metadata request failed")],
)
async def test_a_failing_connection_logs_once_until_it_recovers(
    host, server, monkeypatch, caplog, failure
):
    runner = runner_for(host, server, monkeypatch)
    attempts = 0

    async def flaky_transport(stack):
        nonlocal attempts
        attempts += 1
        if attempts <= 3:
            raise failure
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
async def test_legacy_server_sampling_and_roots(host, monkeypatch, caplog, recwarn):
    from mcp import Client

    async def call(server_context, params):
        # The test server's own SDK flags these legacy requests; vBot's client must not.
        with pytest.warns(MCPDeprecationWarning):
            listing = server_context.session.list_roots()
        roots = await listing
        with pytest.warns(MCPDeprecationWarning):
            sampling = server_context.session.create_message(
                [
                    types.SamplingMessage(
                        role="user", content=types.TextContent(type="text", text="sample")
                    )
                ],
                max_tokens=20,
            )
        sample = await sampling
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

    async def set_level(server_context, params):
        return types.EmptyResult()

    def legacy_client(*args, **kwargs):
        kwargs["mode"] = "legacy"
        return Client(*args, **kwargs)

    monkeypatch.setattr("resources.extensions.mcp.client.Client", legacy_client)
    with pytest.warns(MCPDeprecationWarning):
        legacy = Server(
            "legacy", on_call_tool=call, on_list_tools=list_tools, on_set_logging_level=set_level
        )
    runner = runner_for(host, legacy, monkeypatch)
    try:
        with caplog.at_level(logging.INFO, logger="vbot.extensions.mcp"):
            result = await asyncio.wait_for(
                runner.invoke("tools/call", {"name": "callbacks"}, context(host)), 10
            )
            await asyncio.wait_for(runner.invoke("ping", {}), 10)
            await asyncio.wait_for(runner.invoke("logging/setLevel", {"level": "debug"}), 10)
        assert json.loads(result["content"][0]["text"]) == {
            "root": host.data_dir.as_uri(),
            "sample": "sampled",
        }
    finally:
        await runner.close()
    # Roots changes went out before and after the call: each deprecated feature the
    # legacy connection uses is logged once, and none surfaces as a warning.
    features = [
        match[1]
        for record in caplog.records
        if (match := re.search(r"deprecated since protocol .* feature=(\w+)", record.getMessage()))
    ]
    assert features == ["roots", "ping", "logging"]
    assert [f"{item.filename}:{item.lineno}: {item.message}" for item in recwarn] == []


@pytest.mark.asyncio
async def test_requests_carry_a_log_level_only_after_one_is_set(host, monkeypatch):
    levels = []

    async def call(server_context, params):
        levels.append((params.meta or {}).get(types.LOG_LEVEL_META_KEY))
        return types.CallToolResult(content=[])

    async def list_tools(server_context, params):
        return types.ListToolsResult(
            tools=[types.Tool(name="levels", input_schema={"type": "object"})]
        )

    runner = runner_for(
        host, Server("levels", on_call_tool=call, on_list_tools=list_tools), monkeypatch
    )
    try:
        async with asyncio.timeout(10):
            await runner.invoke("tools/call", {"name": "levels"}, context(host))
            await runner.invoke("logging/setLevel", {"level": "debug"})
            await runner.invoke("tools/call", {"name": "levels"}, context(host))
    finally:
        await runner.close()

    # Without an explicit level the server applies its own default.
    assert levels == [None, "debug"]


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


class _Hanging:
    """A transport whose connection never comes up."""

    def __init__(self, entered):
        self.entered = entered

    async def __aenter__(self):
        await self.entered()
        await asyncio.Event().wait()

    async def __aexit__(self, *exc):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sign_in, expected",
    [
        (False, "MCP connection did not connect within 0.05 seconds"),
        (
            True,
            "MCP connection did not connect within 0.05 seconds: its sign-in is waiting for "
            "the user",
        ),
    ],
)
async def test_a_call_waits_for_its_connection_only_until_the_timeout(
    host, server, monkeypatch, sign_in, expected
):
    runner = runner_for(host, server, monkeypatch)
    runner.config["timeout"] = 0.05

    async def entered():
        if sign_in:
            await runner.inputs.request("example", "oauth", {"authorization_url": "test"})

    async def transport(stack):
        return _Hanging(entered)

    monkeypatch.setattr(runner, "_transport", transport)
    try:
        with pytest.raises(InvocationNotSentError) as refused:
            await asyncio.wait_for(runner.invoke("ping", {}), 5)
        assert str(refused.value) == expected
        assert runner.state == "connecting"
    finally:
        await runner.close()


@pytest.mark.asyncio
async def test_unanswered_inputs_expire(host):
    inputs = InputRequests(ttl=0)

    elicited = await inputs.request("example", "elicitation", {"message": "test-owned"})
    with pytest.raises(ValueError, match="sign-in was not completed in time"):
        await inputs.request("example", "oauth", {})

    assert elicited == {"action": "cancel"}
    assert inputs.list() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transport, keys, expected",
    [
        # The transport carries each call into its server requests.
        pytest.param("memory", ["a", "b"], {"a": ["a"], "b": ["b"]}, id="carried"),
        # A stream does not; a server request belongs to the only call in flight ...
        pytest.param("stream", ["a"], {"a": ["a"]}, id="only-call"),
        # ... and to no Agent while several run.
        pytest.param("stream", ["a", "b"], {"a": [], "b": []}, id="several-calls"),
    ],
)
async def test_calls_run_concurrently_and_server_requests_belong_to_their_call(
    host, tmp_path, monkeypatch, recwarn, transport, keys, expected
):
    entered = {key: asyncio.Event() for key in keys}
    release = asyncio.Event()
    listed: list[str] = []
    all_listed = asyncio.Event()

    async def call(server_context, params):
        key = params.arguments["key"]
        entered[key].set()
        await release.wait()
        roots = await server_context.session.list_roots()
        listed.append(key)
        if len(listed) == len(keys):
            all_listed.set()
        # No call ends before every call has its roots.
        await all_listed.wait()
        names = [Path(url2pathname(urlparse(str(root.uri)).path)).name for root in roots.roots]
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(names))]
        )

    async def list_tools(server_context, params):
        return types.ListToolsResult(
            tools=[types.Tool(name="roots", input_schema={"type": "object"})]
        )

    server = Server("roots", on_call_tool=call, on_list_tools=list_tools)
    runner = runner_for(
        host, StreamServer(server) if transport == "stream" else server, monkeypatch
    )
    # The legacy protocol, where servers send requests of their own.
    runner.config["transport"] = "sse"
    calls = {
        key: asyncio.create_task(
            runner.invoke(
                "tools/call",
                {"name": "roots", "arguments": {"key": key}},
                replace(context(host), cwd=tmp_path / key),
            )
        )
        for key in keys
    }
    try:
        async with asyncio.timeout(10):
            # Every call runs before any of them finishes.
            await asyncio.gather(*(event.wait() for event in entered.values()))
            release.set()
            results = {key: await task for key, task in calls.items()}
        assert {
            key: json.loads(result["content"][0]["text"]) for key, result in results.items()
        } == (expected)
    finally:
        for task in calls.values():
            task.cancel()
        await asyncio.gather(*calls.values(), return_exceptions=True)
        await runner.close()


@pytest.mark.asyncio
async def test_legacy_list_changed_refreshes_and_republishes_a_changed_catalog(host, monkeypatch):
    tools = ["add"]
    published: list[list[str]] = []

    async def call(server_context, params):
        tools.append("added")
        # A burst of changes: one refresh follows.
        for _ in range(3):
            await server_context.session.send_tool_list_changed()
        return types.CallToolResult(content=[])

    async def list_tools(server_context, params):
        return types.ListToolsResult(
            tools=[types.Tool(name=name, input_schema={"type": "object"}) for name in tools]
        )

    async def sleep(delay):
        await asyncio.sleep(0)

    monkeypatch.setattr(mcp_client, "_sleep", sleep)
    server = Server("changes", on_call_tool=call, on_list_tools=list_tools)
    runner = runner_for(host, StreamServer(server), monkeypatch)
    runner.config["transport"] = "sse"
    runner.publish = lambda _, catalog: published.append(
        [tool["name"] for tool in catalog["tools"]]
    )
    try:
        async with asyncio.timeout(10):
            await runner.invoke("tools/call", {"name": "add"})
            while runner.catalog["tools"][-1]["name"] != "added" or not (
                runner._subscriptions["catalog-refresh"].done()
            ):
                await asyncio.sleep(0)
        # Republished once: refreshes that find the same catalog publish nothing.
        assert published == [["add"], ["add", "added"]]
    finally:
        await runner.close()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "win32", reason="Windows runs batch files through cmd.exe")
async def test_batch_file_arguments_that_cmd_would_reinterpret_are_refused(host, tmp_path):
    marker = tmp_path / "started"
    shim = tmp_path / "server.cmd"
    shim.write_text(f'@echo off\r\necho started> "{marker}"\r\n')
    runner = ConnectionRunner(
        validate_connection(
            {
                "id": "shim",
                "transport": "stdio",
                "command": str(shim),
                "args": ["--database", "postgres://host/db?user=a&mode=b"],
            }
        ),
        host,
        InputRequests(),
        lambda *args: None,
    )
    try:
        with pytest.raises(InvocationNotSentError) as refused:
            await asyncio.wait_for(runner.invoke("ping", {}), 10)
    finally:
        await runner.close()

    assert str(refused.value) == (
        "ValueError: MCP server not started: argument 2 contains '&', which cmd.exe interprets "
        "when Windows runs server.cmd; start the server's program directly or pass the value "
        "through an environment variable"
    )
    assert not marker.exists()
