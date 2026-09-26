"""Mcp: protocol behavior."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import socket
import sys
from dataclasses import replace
from types import SimpleNamespace

import mcp_types as types
import pytest
import uvicorn
from mcp.server import Server

from core.attachments import AttachmentTooLargeError
from core.extensions.extensions import ExtensionAPI, ExtensionDeclarations
from core.tools.availability import ToolAccess, resolve_tool_access
from core.tools.tools import ToolRegistry
from resources.extensions.mcp.client import ConnectionRunner, sampling_messages
from resources.extensions.mcp.config import ConnectionStore, validate_connection
from resources.extensions.mcp.content import ContentStore
from resources.extensions.mcp.extension import MCPService, remote_tool_name
from resources.extensions.mcp.interactions import InputRequests
from tests.resources.extensions.mcp_helpers import (
    context,
    runner_for,
)
from tests.resources.extensions.mcp_helpers import (
    host as host,
)
from tests.resources.extensions.mcp_helpers import (
    server as server,
)


@pytest.mark.asyncio
async def test_real_protocol_catalog_tools_resources_and_prompts(host, server, monkeypatch):
    runner = runner_for(host, server, monkeypatch)
    try:
        catalog = await asyncio.wait_for(runner.invoke("catalog", {}), 10)
        result = await runner.invoke(
            "tools/call", {"name": "echo", "arguments": {"value": "sentinel"}}, context(host)
        )
        resource = await runner.invoke("resources/read", {"uri": "test://scene"})
        prompt = await runner.invoke(
            "prompts/get", {"name": "workflow", "arguments": {"subject": "scene"}}
        )

        assert catalog["instructions"] == "test-owned-server-instructions"
        assert [tool["name"] for tool in catalog["tools"]] == ["echo"]
        assert len(catalog["resource_templates"]) == 1
        assert json.loads(result["content"][0]["text"]) == {"value": "sentinel"}
        assert resource["contents"][0]["text"] == "test-owned-scene"
        assert prompt["messages"][0]["content"]["text"] == "test-owned-workflow:scene"
    finally:
        await runner.close()


@pytest.mark.asyncio
async def test_stdio_server_round_trip_and_shutdown(host, tmp_path):
    script = tmp_path / "server.py"
    script.write_text(
        'from mcp.server import MCPServer\ns = MCPServer("stdio-test")\n'
        "@s.tool()\ndef echo(value: str) -> str:\n    return value\ns.run()\n"
    )
    runner = ConnectionRunner(
        validate_connection(
            {"id": "stdio", "transport": "stdio", "command": sys.executable, "args": [str(script)]}
        ),
        host,
        InputRequests(),
        lambda *args: None,
    )
    try:
        result = await asyncio.wait_for(
            runner.invoke("tools/call", {"name": "echo", "arguments": {"value": "wire-sentinel"}}),
            15,
        )
        assert result["content"][0]["text"] == "wire-sentinel"
    finally:
        await runner.close()
    assert runner.state == "disconnected"


@pytest.mark.asyncio
async def test_cancelled_mutation_is_not_replayed(host, server, monkeypatch):
    entered = asyncio.Event()
    calls = []

    @server.tool()
    async def mutate() -> str:
        calls.append("mutated")
        entered.set()
        await asyncio.Event().wait()
        return "unreachable"

    runner = runner_for(host, server, monkeypatch)
    try:
        task = asyncio.create_task(runner.invoke("tools/call", {"name": "mutate"}, context(host)))
        await asyncio.wait_for(entered.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(runner.invoke("ping", {}), 5)
        assert calls == ["mutated"]
    finally:
        await runner.close()


@pytest.mark.asyncio
async def test_input_response_is_validated_and_not_retained():
    inputs = InputRequests()
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


@pytest.mark.asyncio
async def test_unknown_metadata_and_media_are_preserved(host):
    payload = {
        "content": [
            {
                "type": "image",
                "mimeType": "image/png",
                "data": base64.b64encode(b"image-bytes").decode(),
                "_meta": {"detail": "original"},
            }
        ],
        "structuredContent": {"answer": 42},
        "_meta": {"vendor": {"future": True}},
    }
    result, artifacts = await ContentStore(host).preserve(payload)
    assert result["_meta"] == payload["_meta"]
    assert result["structuredContent"] == payload["structuredContent"]
    assert result["content"][0]["_meta"] == {"detail": "original"}
    assert result["content"][0]["size_bytes"] == len(b"image-bytes")
    assert len(artifacts) == 1
    assert "data" in payload["content"][0]


@pytest.mark.asyncio
async def test_media_shaped_application_data_and_metadata_are_not_rewritten(host):
    media_shape = {"type": "image", "data": "not base64", "mimeType": "image/png"}
    resource_shape = {"uri": "app://item", "blob": "ordinary application data"}
    payload = {
        "structuredContent": {"rows": [media_shape, resource_shape]},
        "_meta": media_shape,
        "content": [{"type": "text", "text": "sentinel", "_meta": resource_shape}],
        "tools": [{"name": "example", "inputSchema": {"examples": [media_shape]}}],
    }
    result, artifacts = await ContentStore(host).preserve(payload)
    assert result == payload
    assert artifacts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("position", ["content", "contents", "messages", "message_list"])
async def test_protocol_resource_and_prompt_media_positions_are_preserved(host, position):
    raw = b"test-owned-media"
    resource = {
        "uri": "test://blob",
        "mimeType": "image/png",
        "blob": base64.b64encode(raw).decode(),
    }
    block = {"type": "resource", "resource": resource}
    if position == "contents":
        payload = {"contents": [resource]}
    elif position == "content":
        payload = {"content": [block]}
    else:
        payload = {
            "messages": [
                {"role": "user", "content": [block] if position == "message_list" else block}
            ]
        }
    result, artifacts = await ContentStore(host).preserve(payload)
    if position == "contents":
        preserved = result["contents"][0]
    elif position == "content":
        preserved = result["content"][0]["resource"]
    else:
        content = result["messages"][0]["content"]
        preserved = (content[0] if isinstance(content, list) else content)["resource"]
    assert preserved["size_bytes"] == len(raw)
    assert "path" in preserved
    assert "blob" not in preserved
    assert "blob" in resource
    assert artifacts == []


@pytest.mark.parametrize(
    "value",
    [
        {"id": "../bad", "transport": "stdio", "command": "python"},
        {"id": "example", "transport": "http", "url": "https://user:secret@example.com"},
        {"id": "example", "transport": "stdio"},
        {"id": "example", "transport": "stdio", "command": "python", "cwd": "relative"},
    ],
)
def test_invalid_configuration_is_rejected(value):
    with pytest.raises(ValueError):
        validate_connection(value)


def test_corrupt_connection_store_is_not_overwritten(tmp_path):
    store = ConnectionStore(tmp_path)
    store.path.write_text("corrupt")
    with pytest.raises(ValueError):
        store.save({})
    assert store.path.read_text() == "corrupt"


def test_remote_names_are_stable_unique_and_provider_safe():
    assert remote_tool_name("a" * 32, "b" * 200) == remote_tool_name("a" * 32, "b" * 200)
    assert len(remote_tool_name("a" * 32, "b" * 200)) == 64
    assert remote_tool_name("example", "a/b") != remote_tool_name("example", "a_b")


def test_sampling_rejects_unknown_content_instead_of_losing_it():
    with pytest.raises(ValueError):
        sampling_messages({"messages": [{"role": "user", "content": {"type": "future-data"}}]})


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
async def test_future_tools_follow_connection_grant_and_explicit_denials_win(host):
    api = ExtensionAPI("mcp", ExtensionDeclarations(), config={}, logger=logging.getLogger("test"))
    registry = ToolRegistry()
    api.operations.bind(registry)
    service = MCPService(api)
    await service.start(host)
    service.connections["example"] = validate_connection(
        {"id": "example", "transport": "stdio", "command": "python"}
    )
    runner = service._runner(service.connections["example"])
    runner.state = "connected"
    service._publish(runner, {"tools": [{"name": "original", "inputSchema": {"type": "object"}}]})
    denied = remote_tool_name("example", "denied")
    policy = ToolAccess(
        mode="selected", allowed=("mcp_example",), granted=("mcp_example",), denied=(denied,)
    )
    service._publish(
        runner,
        {
            "tools": [
                {"name": name, "inputSchema": {"type": "object"}}
                for name in ("original", "new", "denied")
            ]
        },
    )

    allowed = resolve_tool_access(policy, registry.list_tools(), "off").allowed_tools

    assert remote_tool_name("example", "new") in allowed
    assert denied not in allowed
    await service.close()


@pytest.mark.asyncio
async def test_stopping_a_connection_that_ignores_cancellation_still_removes_its_tools(
    host, monkeypatch, caplog
):
    api = ExtensionAPI("mcp", ExtensionDeclarations(), config={}, logger=logging.getLogger("test"))
    registry = ToolRegistry()
    api.operations.bind(registry)
    service = MCPService(api)
    await service.start(host)
    service.connections["example"] = validate_connection(
        {"id": "example", "transport": "stdio", "command": "python"}
    )
    runner = service._runner(service.connections["example"])
    runner.state = "connected"
    service._publish(runner, {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]})
    assert "mcp_example" in [tool.name for tool in registry.list_tools()]
    release = asyncio.Event()

    async def stuck() -> None:
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    runner._task = asyncio.create_task(stuck())
    await asyncio.sleep(0)  # enter the loop so cancellation is actually ignored
    monkeypatch.setattr("resources.extensions.mcp.client.CONNECTION_CLOSE_TIMEOUT_SECONDS", 0.05)

    try:
        with caplog.at_level(logging.WARNING, logger="vbot.extensions.mcp"):
            stop = asyncio.create_task(service._stop("example"))
            done, _ = await asyncio.wait({stop}, timeout=5)
            assert done, "stopping must not wait forever on a connection ignoring cancellation"
            await stop

        assert "mcp_example" not in [tool.name for tool in registry.list_tools()]
        assert runner.state == "disconnected"
        assert "did not stop within" in caplog.text
    finally:
        release.set()
        await runner._task
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("access", ["connect", "tool"])
async def test_save_serializes_runner_access_until_replacement_is_ready(host, monkeypatch, access):
    api = ExtensionAPI("mcp", ExtensionDeclarations(), config={}, logger=logging.getLogger("test"))
    registry = ToolRegistry()
    api.operations.bind(registry)
    service = MCPService(api)
    await service.start(host)
    started_commands = []

    def start(runner):
        started_commands.append((runner.id, runner.config["command"]))
        runner.state = "connected"

    async def browse(runner, context, arguments):
        return {"command": runner.config["command"]}

    monkeypatch.setattr(ConnectionRunner, "start", start)
    monkeypatch.setattr(service, "_browse", browse)
    connection = {"id": "example", "transport": "stdio", "command": "old-command"}
    await service.manage("save", {"connection": connection})
    await service.manage(
        "save", {"connection": {**connection, "id": "other", "command": "other-command"}}
    )
    closing = asyncio.Event()
    release = asyncio.Event()

    async def close():
        closing.set()
        await release.wait()

    monkeypatch.setattr(service.runners["example"], "close", close)
    replacement = {**connection, "command": "new-command"}
    saving = asyncio.create_task(service.manage("save", {"connection": replacement}))
    accessing = None
    try:
        await asyncio.wait_for(closing.wait(), 1)
        entered = asyncio.Event()

        async def concurrent_access():
            entered.set()
            if access == "connect":
                return await service.manage("connect", {"id": "example"})
            return await service._handler("example")(context(host), {"action": "search"})

        accessing = asyncio.create_task(concurrent_access())
        await asyncio.wait_for(entered.wait(), 1)
        assert not accessing.done()
        other = await asyncio.wait_for(service.manage("connect", {"id": "other"}), 1)
        assert other["configuration"]["command"] == "other-command"
        release.set()
        _, result = await asyncio.wait_for(asyncio.gather(saving, accessing), 1)
        assert service.store.load()["example"]["command"] == "new-command"
        assert service.connections["example"]["command"] == "new-command"
        assert service.runners["example"].config["command"] == "new-command"
        example_commands = [
            command for identifier, command in started_commands if identifier == "example"
        ]
        assert example_commands[0] == "old-command"
        assert set(example_commands[1:]) == {"new-command"}
        assert "mcp_example" in [tool.name for tool in registry.list_tools()]
        if access == "tool":
            assert result == {"command": "new-command"}
    finally:
        release.set()
        tasks = [saving, *([accessing] if accessing is not None else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await service.close()


@pytest.mark.asyncio
async def test_media_attachment_delivery_refuses_is_omitted_with_a_marker(host):
    def reject(name, data):
        raise AttachmentTooLargeError("test-owned-size-limit")

    host = replace(host, store_attachment=reject)
    raw = b"media-sentinel"
    before = set(host.data_dir.rglob("*"))
    result, artifacts = await ContentStore(host).preserve(
        {
            "content": [
                {"type": "image", "mimeType": "image/png", "data": base64.b64encode(raw).decode()}
            ]
        }
    )

    # No unmanaged copy: the bytes are gone and the marker says why.
    assert result["content"][0] == {
        "type": "image",
        "mimeType": "image/png",
        "content_omitted": True,
        "media_delivery_error": "test-owned-size-limit",
        "size_bytes": len(raw),
    }
    assert set(host.data_dir.rglob("*")) == before
    assert artifacts == []


@pytest.mark.asyncio
async def test_required_task_tools_use_task_protocol_and_preserve_handle(host):
    calls = []

    async def send_request(method, arguments, options):
        calls.append(arguments)
        return types.CreateTaskResult.model_validate(
            {
                "task": {
                    "taskId": "task-sentinel",
                    "status": "working",
                    "createdAt": "2026-01-01T00:00:00Z",
                    "lastUpdatedAt": "2026-01-01T00:00:00Z",
                    "ttl": 1000,
                }
            }
        ).model_dump(by_alias=True, exclude_none=True)

    runner = ConnectionRunner(
        validate_connection({"id": "example", "transport": "stdio", "command": "python"}),
        host,
        InputRequests(),
        lambda *args: None,
    )
    runner.client = SimpleNamespace(
        session=SimpleNamespace(
            _dispatcher=SimpleNamespace(send_raw_request=send_request), _stamp=lambda *args: None
        )
    )
    runner.catalog = {"tools": [{"name": "long", "execution": {"taskSupport": "required"}}]}

    result = await runner._perform("tools/call", {"name": "long", "arguments": {}})

    assert calls[0]["task"] == {}
    assert result["task"]["taskId"] == "task-sentinel"


@pytest.mark.asyncio
async def test_bundled_package_entrypoint_registers_management(tmp_path):
    from pathlib import Path

    from core.extensions import ExtensionRegistry

    registry = await ExtensionRegistry.aload(
        tmp_path, bundled_dir=Path(__file__).parents[3] / "resources" / "extensions"
    )

    names = {operation["name"] for operation in registry.management("mcp").describe()}

    assert {"save", "test", "invoke", "respond", "cancel-job"} <= names

    for extension in ("mcp", "swarm"):
        descriptions = registry.management(extension).describe()
        assert descriptions
        assert all(item["description"] != item["name"] for item in descriptions)
        assert len({item["description"] for item in descriptions}) == len(descriptions)

    from resources.extensions.swarm._store_values import _validate_profile

    profile_save = next(
        item for item in registry.management("swarm").describe() if item["name"] == "profiles.save"
    )
    for example in profile_save["parameters"]["properties"]["profile"]["examples"]:
        assert _validate_profile(example)["participants"]


@pytest.mark.asyncio
async def test_task_payload_survives_the_real_wire(host, tmp_path):
    script = tmp_path / "task_server.py"
    script.write_text("""import json
import sys
task = {"taskId": "task-sentinel", "status": "completed", "ttl": 1000,
    "createdAt": "2026-01-01T00:00:00Z", "lastUpdatedAt": "2026-01-01T00:00:00Z"}
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request["method"]
    result = None
    if method == "initialize":
        result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {},
            "tasks": {"requests": {"tools": {"call": {}}}}},
            "serverInfo": {"name": "tasks", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "long", "inputSchema": {"type": "object"},
            "execution": {"taskSupport": "required"}}]}
    elif method == "tools/call" and "task" in request["params"]:
        result = {"task": task}
    elif method == "tasks/result" and request["params"].get("taskId") == "task-sentinel":
        result = {"content": [{"type": "text", "text": "payload-sentinel"}],
            "structuredContent": {"nested": [1, 2, 3]}, "_meta": {"preserved": True}}
    response = {"jsonrpc": "2.0", "id": request["id"]}
    if result is None:
        response["error"] = {"code": -32601, "message": "Method not found"}
    else:
        response["result"] = result
    print(json.dumps(response), flush=True)
""")
    runner = ConnectionRunner(
        validate_connection(
            {"id": "tasks", "transport": "stdio", "command": sys.executable, "args": [str(script)]}
        ),
        host,
        InputRequests(),
        lambda *args: None,
    )
    try:
        async with asyncio.timeout(10):
            started = await runner.invoke("tools/call", {"name": "long", "arguments": {}})
            result = await runner.invoke("tasks/result", {"taskId": started["task"]["taskId"]})
        assert result["content"][0]["text"] == "payload-sentinel"
        assert result["structuredContent"] == {"nested": [1, 2, 3]}
        assert result["_meta"] == {"preserved": True}
    finally:
        await runner.close()
