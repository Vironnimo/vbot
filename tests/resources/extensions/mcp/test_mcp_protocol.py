"""MCP: the connection runner speaks the protocol over each transport and callback."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import socket
import sys
import warnings
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import override
from urllib.parse import urlparse
from urllib.request import url2pathname

import httpcore2
import httpx2
import mcp.types as types
import pytest
import uvicorn
from mcp.client.auth import OAuthFlowError
from mcp.server import Server
from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler, ResourceUpdated
from mcp.shared.exceptions import MCPDeprecationWarning

from core.extensions.operations import PENDING_INPUTS_RESOURCE
from core.utils.timestamps import parse_canonical_timestamp
from resources.extensions.mcp import _tasks
from resources.extensions.mcp import client as mcp_client
from resources.extensions.mcp._callbacks import sampling_messages
from resources.extensions.mcp._network import DestinationGuard
from resources.extensions.mcp.client import ConnectionRunner, InvocationNotSentError
from resources.extensions.mcp.config import validate_connection
from resources.extensions.mcp.interactions import INPUT_REQUEST_TTL_SECONDS, InputRequests
from tests.resources.extensions.mcp.mcp_test_support import (
    StreamServer,
    context,
    runner_for,
    start_service,
)

# A minimal stdio server: it answers the handshake, one plain Tool and one Tool that
# requires the task protocol, whose task completes at its first status read and hands
# out its result through tasks/result. It also writes a line that is neither UTF-8
# nor JSON.
_STDIO_SERVER = """import json
import sys
task = {"taskId": "task-sentinel", "status": "working", "ttl": 60000, "pollInterval": 100,
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
    elif method == "tasks/get" and params.get("taskId") == "task-sentinel":
        result = {**task, "status": "completed"}
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
async def test_stdio_wire_round_trips_calls_and_task_payloads_then_shuts_down(
    host, tmp_path, monkeypatch
):
    polls = []

    async def poll(seconds):
        polls.append(seconds)

    monkeypatch.setattr(_tasks, "_sleep", poll)
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
            # A Tool that requires tasks runs as one and returns the task's result.
            result = await runner.invoke("tools/call", {"name": "long", "arguments": {}})
            # The explicit task operations reach the same task.
            payload = await runner.invoke("tasks/result", {"taskId": "task-sentinel"})
        assert echoed["content"][0]["text"] == "wire-sentinel"
        assert [event["kind"] for event in runner.events()["events"]].count("transport_error") == 1
        assert polls == [0.1]
        for answer in (result, payload):
            assert answer["content"][0]["text"] == "payload-sentinel"
            assert answer["structuredContent"] == {"nested": [1, 2, 3]}
            assert answer["_meta"] == {"preserved": True}
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


class _HTTPFaults:
    """ASGI middleware that ends sessions the way a restarted legacy server does.

    It also refuses the next POST of each method in ``refuse`` with that HTTP status
    and a JSON-RPC error, without passing it on.
    """

    def __init__(self, app):
        self.app = app
        self.expired: set[bytes] = set()
        self.current: bytes | None = None
        self.refuse: dict[str, int] = {}
        self.passed: list[str] = []

    async def __call__(self, scope, receive, send):
        session = dict(scope.get("headers", [])).get(b"mcp-session-id")
        if session is not None:
            self.current = session
            if session in self.expired:
                await self._error(send, 404, None, "Session not found")
                return
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        messages = [await receive()]
        while messages[-1].get("more_body"):
            messages.append(await receive())
        message = json.loads(b"".join(item.get("body", b"") for item in messages))
        status = self.refuse.pop(message.get("method"), None)
        if status is not None:
            await self._error(send, status, message.get("id"), "test-owned-refusal")
            return
        self.passed.append(message.get("method"))

        async def replay():
            return messages.pop(0) if messages else await receive()

        await self.app(scope, replay, send)

    @staticmethod
    async def _error(send, status, request_id, text):
        body = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32600, "message": text}}
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": json.dumps(body).encode()})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "fault"),
    [
        ("tools/call", "ended"),
        ("resources/read", "ended"),
        ("tools/call", 403),
        ("resources/read", 429),
    ],
    ids=["ended-call", "ended-read", "refused-call", "rate-limited-read"],
)
async def test_an_unprocessed_http_request_is_not_run_and_only_a_read_runs_again(
    host, server, monkeypatch, operation, fault
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
    sessions = _HTTPFaults(server.streamable_http_app())
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
            session = sessions.current
            if fault == 403:
                # Refused unprocessed: never sent, and the connection stays.
                sessions.refuse["tools/call"] = 403
                with pytest.raises(InvocationNotSentError) as refused:
                    await runner.invoke("tools/call", echo, context(host))
                assert str(refused.value) == "HTTP 403 Forbidden: test-owned-refusal"
                assert refused.value.refused == 403
                assert sessions.passed.count("tools/call") == 1
            elif fault == 429:
                # A read the server refused for its rate runs again.
                sessions.refuse["resources/read"] = 429
                result = await runner.invoke("resources/read", {"uri": "test://scene"})
                assert result["contents"][0]["text"] == "test-owned-scene"
                kinds = [event["kind"] for event in runner.events()["events"]]
                assert kinds.count("read_retry") == 1
            if fault != "ended":
                assert sessions.current == session
                assert "connection_failed" not in [
                    event["kind"] for event in runner.events()["events"]
                ]
                return
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


class _RecordingBackend(httpcore2.AsyncNetworkBackend):
    def __init__(self) -> None:
        self.connected: list[str] = []

    @override
    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connected.append(host)
        return httpcore2.AsyncMockStream([])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "server, target, connected",
    [
        # A public server's metadata cannot point at metadata services or the private network.
        ("mcp.example", "169.254.169.254", None),
        ("mcp.example", "::ffff:169.254.169.254", None),
        ("mcp.example", "auth.internal", None),
        ("mcp.example", "auth.example", ["93.184.216.35"]),
        ("mcp.example", "split.example", ["93.184.216.36"]),
        # A server whose name answers differently on a later lookup stays public.
        ("rebind.example", "auth.internal", None),
        # A server on this machine or the private network may use private addresses ...
        ("mcp.internal", "auth.internal", ["10.0.0.5"]),
        ("127.0.0.1", "localhost", ["127.0.0.1"]),
        # ... but never metadata services.
        ("127.0.0.1", "fd00:ec2::254", None),
        ("mcp.internal", "169.254.169.254", None),
    ],
)
async def test_connections_reach_only_permitted_addresses(server, target, connected):
    names = {
        "mcp.example": ["93.184.216.34"],
        "auth.example": ["93.184.216.35"],
        "split.example": ["10.0.0.6", "93.184.216.36"],
        "mcp.internal": ["192.168.1.10"],
        "auth.internal": ["10.0.0.5"],
        "localhost": ["127.0.0.1"],
    }
    rebinding = iter([["93.184.216.37"], ["192.168.1.11"]])

    async def resolve(host, port):
        return next(rebinding) if host == "rebind.example" else names[host]

    inner = _RecordingBackend()
    guard = DestinationGuard(server, resolve=resolve, inner=inner)
    # The MCP traffic reaches the configured server first.
    await guard.connect_tcp(server, 443)
    inner.connected.clear()

    if connected is None:
        with pytest.raises(httpcore2.ConnectError, match="refused to reach"):
            await guard.connect_tcp(target, 443)
        assert inner.connected == []
    else:
        await guard.connect_tcp(target, 443)
        assert inner.connected == connected


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


# A local server that fails at startup after writing coloured, partly secret stderr.
_FAILING_SERVER = (
    "import sys\n"
    "sys.stderr.write('\\x1b[33mstarting\\x1b[0m\\n')\n"
    "sys.stderr.write('fatal: token secret-sentinel was rejected')\n"
    "sys.exit(3)\n"
)


def _http_answer(status: int | None) -> type[httpx2.AsyncClient]:
    """A connection's HTTP client class whose server answers *status*, or is unreachable."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if status is None:
            raise httpx2.ConnectError("connection refused", request=request)
        return httpx2.Response(status, json={"error": "refused"})

    class Answering(mcp_client._ObservedHTTPClient):
        def __init__(self, observer: ConnectionRunner, **options: object) -> None:
            super().__init__(observer, **{**options, "transport": httpx2.MockTransport(handler)})

    return Answering


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "expected"),
    [
        pytest.param(
            "missing-program", {"code": "command_not_found", "requirement": "Node.js"}, id="program"
        ),
        pytest.param("missing-directory", {"code": "directory_not_found"}, id="directory"),
        pytest.param(
            "missing-credential",
            {"code": "credential_missing", "credential": "UNSET_TOKEN"},
            id="credential",
        ),
        pytest.param("server-exits", {"code": "process_exited"}, id="process-exit"),
        pytest.param(401, {"code": "unauthorized", "status": 401}, id="http-401"),
        pytest.param(404, {"code": "endpoint_not_found", "status": 404}, id="http-404"),
        pytest.param(None, {"code": "server_unreachable", "host": "mcp.test"}, id="unreachable"),
    ],
)
async def test_a_failed_connection_reports_its_cause(host, tmp_path, monkeypatch, case, expected):
    host.set_credential("TEST_TOKEN", "secret-sentinel")
    script = tmp_path / "server.py"
    script.write_text(_FAILING_SERVER)
    connection: dict[str, object] = {
        "id": "example",
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(script)],
        "credential_environment": {"TOKEN": "TEST_TOKEN"},
    }
    if case == "missing-program":
        connection["command"] = str(tmp_path / "npx")
    elif case == "missing-directory":
        connection["cwd"] = str(tmp_path / "missing")
    elif case == "missing-credential":
        connection["credential_environment"] = {"TOKEN": "UNSET_TOKEN"}
    elif case != "server-exits":
        connection = {"id": "example", "transport": "http", "url": "https://mcp.test/mcp"}
        monkeypatch.setattr(mcp_client, "_ObservedHTTPClient", _http_answer(case))
    service, _registry = await start_service(host)
    try:
        async with asyncio.timeout(10):
            await service.manage("save", {"connection": connection})
            with pytest.raises(InvocationNotSentError):
                await service.runners["example"].invoke("catalog", {})
            status = await service.manage("status", {"id": "example"})
            # The stderr of a process that ended can arrive after its failure.
            while case == "server-exits" and "rejected" not in "".join(status["stderr_tail"]):
                await asyncio.sleep(0.01)
                status = await service.manage("status", {"id": "example"})
    finally:
        await service.close()

    assert status["state"] == "failed"
    assert {key: status["problem"][key] for key in expected} == expected
    assert status["problem"]["message"]
    # A credential without a value is named before connecting again.
    assert status["missing_credentials"] == (
        [expected["credential"]] if case == "missing-credential" else []
    )
    # Its last stderr lines, without terminal colours or the connection's credentials.
    assert status["stderr_tail"] == (
        ["starting", "fatal: token [redacted] was rejected"] if case == "server-exits" else []
    )


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
    runner = runner_for(host, server, monkeypatch, sampling="allow", roots="workspace")
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
@pytest.mark.parametrize(
    ("policy", "samples", "answer", "outcome", "sent_count"),
    [
        # The default: the server is never offered sampling or roots.
        (None, 1, None, "not offered", 0),
        ("ask", 1, "decline", "The user declined the sampling request", 0),
        ("ask", 1, "accept", "sampled", 1),
        ("allow", 1, None, "sampled", 1),
        # A burst beyond the connection's budget is refused.
        ("allow", 6, None, "Too many sampling requests", 5),
    ],
)
async def test_sampling_follows_the_connection_policy_within_its_limits(
    host, monkeypatch, policy, samples, answer, outcome, sent_count
):
    sent = []

    async def sample(invocation, request):
        sent.append(request)
        return {"model": "test/model", "content": "sampled"}

    request = {
        "method": "sampling/createMessage",
        "params": {
            "messages": [{"role": "user", "content": {"type": "text", "text": "prompt-sentinel"}}],
            "maxTokens": 100000,
        },
    }

    async def call(server_context, params):
        capabilities = server_context.session.client_capabilities
        if capabilities is None or capabilities.sampling is None:
            assert capabilities is None or capabilities.roots is None
            text = "not offered"
        elif params.input_responses:
            text = params.input_responses["sample0"].content.text
        else:
            return types.InputRequiredResult.model_validate(
                {
                    "resultType": "input_required",
                    "inputRequests": {f"sample{index}": request for index in range(samples)},
                }
            )
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)])

    async def list_tools(server_context, params):
        return types.ListToolsResult(
            tools=[types.Tool(name="callbacks", input_schema={"type": "object"})]
        )

    server = Server("callbacks", on_call_tool=call, on_list_tools=list_tools)
    config = {} if policy is None else {"sampling": policy}
    runner = runner_for(replace(host, sample=sample), server, monkeypatch, **config)
    task = asyncio.create_task(runner.invoke("tools/call", {"name": "callbacks"}, context(host)))
    try:
        async with asyncio.timeout(10):
            if answer is not None:
                while not runner.inputs.list():
                    await asyncio.sleep(0)
                pending = runner.inputs.list()[0]
                assert pending["kind"] == "sampling"
                assert pending["session_id"] == "session"
                # The user sees the prompt and the capped reply size before deciding.
                assert "prompt-sentinel" in pending["payload"]["message"]
                assert "up to 4096 tokens" in pending["payload"]["message"]
                runner.inputs.respond(pending["id"], {"action": answer})
            try:
                result = await task
                observed = result["content"][0]["text"]
            except Exception as error:  # noqa: BLE001 - the refusal reaches the Tool call
                observed = str(error)
        assert outcome in observed
        # The reply is capped whatever the server asks for.
        assert [request["max_tokens"] for request in sent] == [4096] * sent_count
        events = runner.events()["events"]
        assert "prompt-sentinel" not in json.dumps(events)
        if policy is not None:
            recorded = [e["payload"] for e in events if e["kind"] == "sampling_request"]
            assert {"requested_max_tokens": 100000, "max_tokens": 4096}.items() <= recorded[
                -1
            ].items()
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
    runner = runner_for(host, legacy, monkeypatch, sampling="allow", roots="workspace")
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


def _notified(runner: ConnectionRunner) -> list[str]:
    """The kinds of the events *runner* recorded for server notifications."""
    kinds = {"log", "progress", "notification", "resource_changed", "catalog_changed"}
    return [event["kind"] for event in runner.events()["events"] if event["kind"] in kinds]


@pytest.mark.asyncio
async def test_requests_carry_a_log_level_only_after_one_is_set(host, monkeypatch):
    levels = []

    async def call(server_context, params):
        levels.append((params.meta or {}).get(types.LOG_LEVEL_META_KEY))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", MCPDeprecationWarning)
            # Sent only on a request that carries a level.
            await server_context.session.send_log_message("info", "test-owned-log")
        await server_context.session.report_progress(1, 2)
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
            while len(_notified(runner)) < 3:
                await asyncio.sleep(0)
    finally:
        await runner.close()

    # Without an explicit level the server applies its own default.
    assert levels == [None, "debug"]
    # Each notification is recorded once, as its own kind of event.
    assert sorted(_notified(runner)) == ["log", "progress", "progress"]


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
    remaining = parse_canonical_timestamp(pending["expires_at"]) - datetime.now(UTC)
    assert INPUT_REQUEST_TTL_SECONDS - 10 <= remaining.total_seconds() <= INPUT_REQUEST_TTL_SECONDS
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("format_", "accepted", "refused", "expected"),
    [
        (
            "email",
            ["name@example.com"],
            ["name.example.com", "name@example", "two words@example.com"],
            "an email address such as name@example.com",
        ),
        (
            "uri",
            ["https://example.com/page?q=1", "mailto:name@example.com", "urn:isbn:0451450523"],
            ["example.com/page", "https://exa mple.com", "http://[::1"],
            "an absolute URI with a scheme, such as https://example.com/page",
        ),
        (
            "date",
            ["2024-02-29"],
            ["2026-02-29", "20261002", "2026-10-02T14:30:00Z"],
            "a date as YYYY-MM-DD, such as 2026-10-02",
        ),
        (
            "date-time",
            ["2026-10-02T14:30:00Z", "2026-10-02t14:30:00.25+02:00", "2016-12-31T23:59:60Z"],
            ["2026-10-02T14:30:00", "2026-10-02 14:30:00Z", "2026-10-02T24:00:00Z", "2026-10-02"],
            "a date and time with a time zone (RFC 3339), such as 2026-10-02T14:30:00Z",
        ),
    ],
    ids=["email", "uri", "date", "date-time"],
)
async def test_input_answers_must_have_their_requested_string_format(
    format_, accepted, refused, expected
):
    inputs = InputRequests()
    schema = {"type": "object", "properties": {"when": {"type": "string", "format": format_}}}
    for value in [*refused, *accepted]:
        task = asyncio.create_task(
            inputs.request("example", "elicitation", {"requestedSchema": schema})
        )
        await asyncio.sleep(0)
        identifier = inputs.list()[0]["id"]
        if value in refused:
            with pytest.raises(ValueError) as problem:
                inputs.respond(identifier, {"action": "accept", "content": {"when": value}})
            assert str(problem.value) == (
                f"MCP input response does not satisfy the requested schema: field 'when' must "
                f"be {expected}"
            )
            inputs.respond(identifier, {"action": "cancel"})
        else:
            inputs.respond(identifier, {"action": "accept", "content": {"when": value}})
        await task


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
async def test_unanswered_server_requests_expire_but_a_sign_in_keeps_its_own_deadline(host):
    inputs = InputRequests(ttl=0)
    sign_in = asyncio.create_task(
        inputs.request("example", "oauth", {"url": "test-owned"}, expires_in=600)
    )
    try:
        elicited = await inputs.request("example", "elicitation", {"message": "test-owned"})
        sampled = await inputs.request("example", "sampling", {"message": "test-owned"})

        assert elicited == sampled == {"action": "cancel"}
        assert [item["kind"] for item in inputs.list()] == ["oauth"]
        # Listed with the deadline its caller keeps.
        remaining = parse_canonical_timestamp(inputs.list()[0]["expires_at"]) - datetime.now(UTC)
        assert 590 <= remaining.total_seconds() <= 600
    finally:
        sign_in.cancel()
        await asyncio.gather(sign_in, return_exceptions=True)


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
        host,
        StreamServer(server) if transport == "stream" else server,
        monkeypatch,
        roots="workspace",
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
            while (
                runner.catalog["tools"][-1]["name"] != "added"
                or not runner._subscriptions["catalog-refresh"].done()
                or len(_notified(runner)) < 3
            ):
                await asyncio.sleep(0)
        # Republished once: refreshes that find the same catalog publish nothing.
        assert published == [["add"], ["add", "added"]]
        assert _notified(runner) == ["catalog_changed"] * 3
    finally:
        await runner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True], ids=["listen", "legacy-subscribe"])
async def test_resource_subscriptions_outlive_a_reconnect_until_unsubscribed(
    host, monkeypatch, legacy
):
    bus = InMemorySubscriptionBus()
    listen = ListenHandler(bus)
    # The server's subscribe and unsubscribe requests, in order; legacy sessions to notify.
    requests: list[tuple[str, str]] = []
    sessions = []

    async def listened(server_context, params):
        for uri in params.notifications.resource_subscriptions or ():
            requests.append(("subscribe", uri))
        try:
            return await listen(server_context, params)
        finally:
            for uri in params.notifications.resource_subscriptions or ():
                requests.append(("unsubscribe", uri))

    async def subscribe(server_context, params):
        requests.append(("subscribe", str(params.uri)))
        sessions.append(server_context.session)
        return types.EmptyResult()

    async def unsubscribe(server_context, params):
        requests.append(("unsubscribe", str(params.uri)))
        return types.EmptyResult()

    async def list_resources(server_context, params):
        return types.ListResourcesResult(resources=[])

    async def list_templates(server_context, params):
        return types.ListResourceTemplatesResult(resource_templates=[])

    async def change():
        if legacy:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", MCPDeprecationWarning)
                await sessions[-1].send_resource_updated("test://watched")
        else:
            await bus.publish(ResourceUpdated(uri="test://watched"))

    def changes():
        return [
            event["payload"]["uri"]
            for event in runner.events()["events"]
            if event["kind"] == "resource_changed"
        ]

    async def sleep(delay):
        await asyncio.sleep(0)

    monkeypatch.setattr(mcp_client, "_sleep", sleep)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", MCPDeprecationWarning)
        server = Server(
            "watch",
            on_list_resources=list_resources,
            on_list_resource_templates=list_templates,
            on_subscribe_resource=subscribe if legacy else None,
            on_unsubscribe_resource=unsubscribe if legacy else None,
            on_subscriptions_listen=None if legacy else listened,
        )
    stream = StreamServer(server)
    runner = runner_for(host, stream, monkeypatch)
    if legacy:
        runner.config["transport"] = "sse"
    try:
        async with asyncio.timeout(10):
            await runner.invoke("resources/subscribe", {"uri": "test://watched"})
            await change()
            while changes() != ["test://watched"]:
                await asyncio.sleep(0)
            # The new session subscribes again, before the connection serves calls.
            await stream.kill()
            while requests.count(("subscribe", "test://watched")) < 2:
                await asyncio.sleep(0)
            await change()
            while changes() != ["test://watched"] * 2:
                await asyncio.sleep(0)
            await runner.invoke("resources/unsubscribe", {"uri": "test://watched"})
            while requests[-1] != ("unsubscribe", "test://watched"):
                await asyncio.sleep(0)
            await stream.kill()
            while stream.connections < 3:
                await asyncio.sleep(0)
            await runner.invoke("resources/subscribe", {"uri": "test://other"})
        assert stream.connections == 3
        assert _notified(runner) == ["resource_changed"] * 2
        assert requests == [
            ("subscribe", "test://watched"),
            *([] if legacy else [("unsubscribe", "test://watched")]),
            ("subscribe", "test://watched"),
            ("unsubscribe", "test://watched"),
            ("subscribe", "test://other"),
        ]
    finally:
        await runner.close()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "win32", reason="Windows runs batch files through cmd.exe")
@pytest.mark.parametrize(
    ("args", "refused_argument"),
    [
        (["--database", "postgres://host/db?user=a&mode=b"], "argument 2 contains '&'"),
        # Expanded even inside quotes; two '%' may enclose a name across arguments.
        (["--data", r"C:\Users\%USERNAME%\my data"], "argument 2 contains '%'"),
        (["--low", "5%", "--high", "9%"], "argument 2 contains '%'"),
        # Delayed expansion, where it is on, removes it.
        (["--greeting", "hello!"], "argument 2 contains '!'"),
    ],
    ids=["metacharacter", "variable", "variable-across-arguments", "delayed-expansion"],
)
async def test_batch_file_arguments_that_cmd_would_reinterpret_are_refused(
    host, tmp_path, args, refused_argument
):
    marker = tmp_path / "started"
    shim = tmp_path / "server.cmd"
    shim.write_text(f'@echo off\r\necho started> "{marker}"\r\n')
    runner = ConnectionRunner(
        validate_connection(
            {
                "id": "shim",
                "transport": "stdio",
                "command": str(shim),
                "args": args,
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
        f"ValueError: MCP server not started: {refused_argument}, which cmd.exe interprets "
        "when Windows runs server.cmd; start the server's program directly or pass the value "
        "through an environment variable"
    )
    assert not marker.exists()
