"""MCP interoperability through the real SDK, plus access and data preservation."""

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
import pytest_asyncio
import uvicorn
from mcp.server import MCPServer, Server
from mcp.shared.auth import OAuthToken

from core.attachments import AttachmentTooLargeError
from core.extensions.extensions import ExtensionAPI, ExtensionDeclarations
from core.extensions.operations import ExtensionHost
from core.tools.availability import ToolAccess, resolve_tool_access
from core.tools.contracts import ToolContractError
from core.tools.tools import ToolContext, ToolDefinitionProfileContext, ToolRegistry
from resources.extensions.mcp.client import ConnectionRunner, OAuthStorage, sampling_messages
from resources.extensions.mcp.config import ConnectionStore, validate_connection
from resources.extensions.mcp.content import ContentStore
from resources.extensions.mcp.extension import MCPService, remote_tool_name
from resources.extensions.mcp.interactions import InputRequests


@pytest.fixture
def host(tmp_path):
    async def sample(context, request):
        return {"model": "test/model", "content": "sampled", "usage": {"input_tokens": 2}}

    credentials = {}
    agent = SimpleNamespace(
        tool_access=ToolAccess(), memory_prompt_mode="off", workspace=str(tmp_path)
    )
    return ExtensionHost(
        data_dir=tmp_path,
        sample=sample,
        resolve_agent=lambda project, name: agent,
        store_attachment=lambda name, data: SimpleNamespace(
            id="blob", file_path=tmp_path / name, filename=name, media_type="image/png"
        ),
        resolve_credential=lambda key: credentials.get(key, ""),
        set_credential=lambda key, value: credentials.__setitem__(key, value),
    )


def context(host, agent="alice", project=None):
    return ToolContext(
        agent_id=agent,
        project_id=project,
        session_id="session",
        run_id="run",
        tool_call_id="call",
        tool_name="mcp_example",
        tool_call_index=0,
        workspace=host.data_dir,
        vbot_root=host.data_dir,
        data_root=host.data_dir,
    )


@pytest.fixture
def server():
    server = MCPServer("test", instructions="test-owned-server-instructions")

    @server.tool()
    def echo(value: str) -> dict:
        return {"value": value}

    @server.resource("test://scene")
    def scene() -> str:
        return "test-owned-scene"

    @server.resource("test://items/{name}")
    def item(name: str) -> str:
        return name

    @server.prompt()
    def workflow(subject: str) -> str:
        return f"test-owned-workflow:{subject}"

    return server


def runner_for(host, server, monkeypatch):
    runner = ConnectionRunner(
        validate_connection(
            {"id": "example", "transport": "stdio", "command": sys.executable, "agents": ["alice"]}
        ),
        host,
        InputRequests(),
        lambda *args: None,
    )

    async def transport(stack):
        return server

    monkeypatch.setattr(runner, "_transport", transport)
    return runner


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
    result, artifacts = await ContentStore(host, host.data_dir).preserve(payload)
    assert result["_meta"] == payload["_meta"]
    assert result["structuredContent"] == payload["structuredContent"]
    assert result["content"][0]["_meta"] == {"detail": "original"}
    assert result["content"][0]["size_bytes"] == len(b"image-bytes")
    assert len(artifacts) == 1
    assert "data" in payload["content"][0]


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


@pytest.mark.asyncio
async def test_connection_grant_does_not_leak_between_projects(host):
    api = ExtensionAPI("mcp", ExtensionDeclarations(), config={}, logger=logging.getLogger("test"))
    registry = ToolRegistry()
    api.operations.bind(registry)
    service = MCPService(api)
    await service.start(host)
    service.connections["example"] = validate_connection(
        {"id": "example", "transport": "stdio", "command": "python", "agents": ["alice"]}
    )
    runner = service._runner(service.connections["example"])
    runner.state = "connected"
    service._publish(runner, {"tools": []})
    profile = registry.get("mcp_example").definition_profile_resolver
    assert profile(ToolDefinitionProfileContext(agent_id="alice")) is not None
    assert profile(ToolDefinitionProfileContext(agent_id="alice", project_id="other")) is None
    await service.close()


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
        {"id": "example", "transport": "stdio", "command": "python", "agents": ["alice"]}
    )
    runner = service._runner(service.connections["example"])
    runner.state = "connected"
    service._publish(runner, {"tools": [{"name": "original", "inputSchema": {"type": "object"}}]})
    denied = remote_tool_name("example", "denied")
    policy = ToolAccess(mode="selected", allowed=("mcp_example",), denied=(denied,))
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
async def test_large_media_is_available_as_a_file_when_attachment_delivery_is_unavailable(host):
    def reject(name, data):
        raise AttachmentTooLargeError("test-owned-size-limit")

    host = replace(host, store_attachment=reject)
    raw = b"media-sentinel"
    result, artifacts = await ContentStore(host, host.data_dir / "content").preserve(
        {
            "content": [
                {"type": "image", "mimeType": "image/png", "data": base64.b64encode(raw).decode()}
            ]
        }
    )
    from pathlib import Path

    assert Path(result["content"][0]["path"]).read_bytes() == raw
    assert result["content"][0]["media_delivery_error"] == "test-owned-size-limit"
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

    assert {"save", "grant", "test", "invoke", "respond", "cancel-job"} <= names


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


@pytest.mark.asyncio
async def test_oauth_tokens_use_the_host_store_and_are_redacted(host):
    storage = OAuthStorage(host, "example")
    token = OAuthToken(access_token="secret-access-sentinel", token_type="Bearer")
    await storage.set_tokens(token)
    runner = ConnectionRunner(
        validate_connection({"id": "example", "transport": "http", "url": "https://example.com"}),
        host,
        InputRequests(),
        lambda *args: None,
    )

    loaded = await storage.get_tokens()
    runner._record("log", {"text": "secret-access-sentinel"})

    assert loaded.access_token == token.access_token
    assert "secret-access-sentinel" not in json.dumps(runner.events())
    assert "secret-access-sentinel" not in json.dumps(runner.status())


@pytest.mark.asyncio
async def test_cancelled_oauth_does_not_leave_a_pending_request(host):
    runner = ConnectionRunner(
        validate_connection({"id": "example", "transport": "http", "url": "https://example.com"}),
        host,
        InputRequests(),
        lambda *args: None,
    )
    task = asyncio.create_task(runner._oauth_callback())
    await asyncio.sleep(0)
    pending = runner.inputs.list()[0]

    runner.inputs.respond(pending["id"], {"action": "cancel"})

    with pytest.raises(ValueError):
        await task
    assert runner.inputs.list() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation, expected_calls", [("resources/read", 4), ("tools/call", 1)])
async def test_read_failures_retry_but_mutations_are_not_replayed(
    host, monkeypatch, operation, expected_calls
):
    runner = ConnectionRunner(
        validate_connection({"id": "example", "transport": "stdio", "command": "unused"}),
        host,
        InputRequests(),
        lambda *args: None,
    )
    calls = []

    async def fail(*args):
        calls.append(args)
        raise TimeoutError("test-owned-timeout")

    async def sleep(delay):
        pass

    monkeypatch.setattr(runner, "_perform", fail)
    monkeypatch.setattr(asyncio, "sleep", sleep)

    with pytest.raises(TimeoutError):
        await runner._perform_with_retries(operation, {})

    assert len(calls) == expected_calls


@pytest.mark.asyncio
async def test_owner_cancellation_exits_an_active_request(host):
    from resources.extensions.mcp.client import Invocation

    runner = ConnectionRunner(
        validate_connection({"id": "example", "transport": "stdio", "command": "unused"}),
        host,
        InputRequests(),
        lambda *args: None,
    )
    entered = asyncio.Event()

    async def wait(*args):
        entered.set()
        await asyncio.Event().wait()

    runner._perform_with_retries = wait
    result = asyncio.get_running_loop().create_future()
    await runner._queue.put(Invocation("tools/call", {}, None, result))
    owner = asyncio.create_task(runner._serve())
    await entered.wait()

    owner.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(owner, 1)
    assert result.cancelled()


@pytest_asyncio.fixture
async def context_service(host, monkeypatch):
    api = ExtensionAPI("mcp", ExtensionDeclarations(), config={}, logger=logging.getLogger("test"))
    registry = ToolRegistry()
    api.operations.bind(registry)
    service = MCPService(api)
    await service.start(host)
    service.connections["example"] = validate_connection(
        {"id": "example", "transport": "stdio", "command": "unused", "agents": ["alice"]}
    )
    runner = service._runner(service.connections["example"])
    runner.state = "connected"
    runner.catalog = {
        "tools": [
            {
                "name": "inspect",
                "description": "test-owned-inspection",
                "inputSchema": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            }
        ],
        "resources": [],
        "resource_templates": [],
        "prompts": [],
        "instructions": "test-owned-guidance",
    }
    service._publish(runner, runner.catalog)
    calls = []

    async def invoke(operation, arguments, invocation_context=None):
        calls.append((operation, arguments))
        if operation == "catalog":
            return runner.catalog
        return {
            "content": [
                {"type": "text", "text": arguments.get("arguments", {}).get("value", "done")}
            ],
            "structuredContent": {"sentinel": True},
            "_meta": {"retained": True},
        }

    monkeypatch.setattr(runner, "invoke", invoke)
    yield service, registry, runner, calls
    await service.close()


@pytest.mark.asyncio
async def test_deferred_catalog_keeps_definitions_identical(context_service, host):
    service, registry, runner, calls = context_service
    profile = ToolDefinitionProfileContext(agent_id="alice")
    before = registry.provider_definitions(profile_context=profile)
    runner.catalog["tools"].extend(
        {
            "name": f"tool_{index}",
            "description": "long external description " * 100,
            "inputSchema": {"type": "object", "properties": {"field": {"type": "string"}}},
        }
        for index in range(500)
    )
    service._publish(runner, runner.catalog)
    after = registry.provider_definitions(profile_context=profile)

    assert after == before
    assert [entry["name"] for entry in after] == ["mcp_example"]
    assert len(registry.list_tools()) == 502
    assert registry.prompt_definitions(profile_context=profile) == [
        {"name": before[0]["name"], "description": before[0]["description"]}
    ]


@pytest.mark.asyncio
async def test_search_and_describe_load_only_the_requested_definition(context_service, host):
    service, registry, runner, calls = context_service
    result = await service._browse(
        runner, context(host), {"action": "search", "query": "inspection", "kind": "tool"}
    )
    match = result["data"]["preview"]["matches"][0]
    detail = await service._browse(runner, context(host), match["describe"])

    assert detail["data"]["value"]["arguments_schema"] == runner.catalog["tools"][0]["inputSchema"]
    assert "inputSchema" not in detail["data"]["value"]["definition"]
    assert "resources/read" not in json.dumps(detail)
    assert calls == []


@pytest.mark.asyncio
async def test_discovery_leads_with_tools_and_delivers_guidance(context_service, host):
    service, registry, runner, calls = context_service
    runner.catalog["prompts"] = [{"name": "workflow", "description": "test-owned-workflow"}]
    result = await registry.dispatch(context(host), {"action": "search"})
    preview = result["data"]["preview"]
    assert preview["matches"][0]["kind"] == "tool"
    assert preview["server_guidance"]["instructions"] == "test-owned-guidance"
    prompt = preview["server_guidance"]["prompts"][0]
    detail = await registry.dispatch(context(host), prompt["describe"])
    assert detail["data"]["value"]["definition"]["name"] == "workflow"
    assert detail["data"]["value"]["call"]["target"] == prompt["target"]
    assert calls == []


@pytest.mark.asyncio
async def test_no_match_provides_a_working_capability_browse(context_service, host):
    service, registry, runner, calls = context_service
    result = await registry.dispatch(context(host), {"action": "search", "query": "rendern"})
    preview = result["data"]["preview"]
    assert preview["matches"] == []
    assert preview["available"]["tool"] == 1
    fallback = await registry.dispatch(context(host), preview["next"])
    assert [item["name"] for item in fallback["data"]["preview"]["matches"]] == ["inspect"]


@pytest.mark.asyncio
async def test_search_ranks_partial_matches_and_paginates(context_service, host):
    service, registry, runner, calls = context_service
    runner.catalog["tools"] = [
        {
            "name": f"scene_{index:02d}",
            "description": "material" if index == 12 else "scene",
            "inputSchema": {"type": "object"},
        }
        for index in range(14)
    ]
    service._publish(runner, runner.catalog)
    result = await registry.dispatch(
        context(host), {"action": "search", "query": "scene material", "kind": "tool"}
    )
    preview = result["data"]["preview"]
    assert preview["total"] == 14
    assert preview["matches"][0]["name"] == "scene_12"
    following = await registry.dispatch(context(host), preview["next"])
    names = [item["name"] for item in preview["matches"] + following["data"]["preview"]["matches"]]
    assert len(set(names)) == 14
    assert "next" not in following["data"]["preview"]


@pytest.mark.asyncio
async def test_long_server_guidance_is_explicitly_incomplete_and_readable(context_service, host):
    service, registry, runner, calls = context_service
    runner.catalog["instructions"] = "test-owned-guidance " * 500
    result = await registry.dispatch(context(host), {"action": "search"})
    guidance = result["data"]["preview"]["server_guidance"]
    assert guidance["complete"] is False
    text = guidance["instructions"]
    next_read = result["data"]["guidance_read"]
    while next_read:
        part = await registry.dispatch(context(host), next_read)
        text += part["data"]["value"]
        next_read = part["data"].get("next")
    assert text == runner.catalog["instructions"]


@pytest.mark.asyncio
async def test_first_discovery_includes_tools_published_during_connect(
    context_service, host, monkeypatch
):
    service, registry, runner, calls = context_service
    catalog = runner.catalog
    runner.catalog = {}
    runner.state = "connecting"
    service._publish(runner, {"tools": []})

    async def connect(*args):
        runner.catalog = catalog
        runner.state = "connected"
        service._publish(runner, catalog)

    monkeypatch.setattr(runner, "invoke", connect)
    result = await registry.dispatch(context(host), {"action": "search", "kind": "tool"})
    assert [item["name"] for item in result["data"]["preview"]["matches"]] == ["inspect"]


@pytest.mark.asyncio
async def test_inspection_reports_policy_blocks_without_executing_tools(context_service, host):
    service, registry, runner, calls = context_service
    runner.catalog["tools"][0]["description"] = "test-owned-long-description " * 30
    agent = host.resolve_agent(None, "alice")
    agent.tool_access = ToolAccess(mode="selected", allowed=[])
    result = await service.manage("inspect", {"id": "example"})
    assert result["agent_access"] == [{"agent": "alice", "access": "blocked", "tool_count": 0}]
    assert result["tools"][0]["name"] == "inspect"
    assert result["tools"][0]["description"] == runner.catalog["tools"][0]["description"]
    agent.tool_access = ToolAccess()
    result = await service.manage("inspect", {"id": "example"})
    assert result["agent_access"] == [{"agent": "alice", "access": "allowed", "tool_count": 1}]
    agent.tool_access = ToolAccess(denied=[remote_tool_name("example", "inspect")])
    result = await service.manage("inspect", {"id": "example"})
    assert result["agent_access"][0]["tool_count"] == 0
    assert calls == []


@pytest.mark.asyncio
async def test_grant_revoked_during_connection_does_not_disclose_catalog(
    context_service, host, monkeypatch
):
    service, registry, runner, calls = context_service
    runner.state = "connecting"

    async def connect(*args):
        service.connections[runner.id]["agents"] = []
        runner.state = "connected"

    monkeypatch.setattr(runner, "invoke", connect)
    result = await registry.dispatch(context(host), {"action": "search"})
    assert result["ok"] is False
    assert result["data"] is None


@pytest.mark.asyncio
async def test_no_match_fallback_does_not_reveal_denied_tools(context_service, host):
    service, registry, runner, calls = context_service
    host.resolve_agent(None, "alice").tool_access = ToolAccess(
        denied=[remote_tool_name("example", "inspect")]
    )
    result = await registry.dispatch(context(host), {"action": "search", "query": "missing"})
    assert result["data"]["preview"]["available"]["tool"] == 0
    fallback = await registry.dispatch(context(host), result["data"]["preview"]["next"])
    assert fallback["data"]["preview"]["matches"] == []


@pytest.mark.asyncio
async def test_transport_failure_does_not_claim_remote_call_was_undone(
    context_service, host, monkeypatch
):
    service, registry, runner, calls = context_service

    async def fail(*args):
        calls.append(args)
        raise ValueError("test-owned-timeout")

    monkeypatch.setattr(runner, "invoke", fail)
    target = service._entries(runner, service._allowed(context(host)))[-1]["target"]
    result = await registry.dispatch(
        context(host), {"action": "call", "target": target, "arguments": {"value": "sentinel"}}
    )
    assert result["error"]["code"] == "mcp_call_unconfirmed"
    assert len(calls) == 1


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


@pytest.mark.asyncio
async def test_large_result_is_durable_and_exactly_readable_in_chunks(host):
    from resources.extensions.mcp.content import RESULT_VIEW_CHARACTERS

    store = ContentStore(host, host.data_dir / "content")
    payload = {
        "content": [{"type": "text", "text": "test-owned-text-ä" * 2000}],
        "_meta": {"keep": True},
    }
    receipt, _ = await store.present(payload, context(host), "example")
    restored = ContentStore(host, host.data_dir / "content")
    document = await restored.load_result(receipt["result_id"], context(host), "example")
    arguments = {
        "action": "read",
        "result_id": receipt["result_id"],
        "pointer": "/content/0/text",
        "limit": 997,
    }
    pieces = []
    while True:
        page = restored.read_result(document, arguments)
        pieces.append(page["value"])
        if "next" not in page:
            break
        arguments = page["next"]

    assert not receipt["complete"]
    assert len(json.dumps(receipt)) < RESULT_VIEW_CHARACTERS
    assert "".join(pieces) == payload["content"][0]["text"]
    assert document["payload"]["_meta"] == payload["_meta"]


@pytest.mark.asyncio
async def test_result_reader_preserves_agent_and_project_ownership(host):
    store = ContentStore(host, host.data_dir / "content")
    receipt, _ = await store.present({"sentinel": True}, context(host), "example")

    with pytest.raises(ValueError):
        await store.load_result(receipt["result_id"], context(host, "bob"), "example")
    with pytest.raises(ValueError):
        await store.load_result(receipt["result_id"], context(host, project="other"), "example")
    with pytest.raises(ValueError):
        await store.load_result("../outside", context(host), "example")


@pytest.mark.asyncio
async def test_result_reader_filters_rows_and_paginates_without_losing_values(host):
    store = ContentStore(host, host.data_dir / "content")
    receipt, _ = await store.present(
        {"rows": [{"id": index, "private": "unused"} for index in range(31)]},
        context(host),
        "example",
    )
    document = await store.load_result(receipt["result_id"], context(host), "example")
    first = store.read_result(
        document,
        {"action": "read", "result_id": receipt["result_id"], "pointer": "/rows", "fields": ["id"]},
    )
    second = store.read_result(document, first["next"])

    assert [entry["value"] for entry in first["entries"] + second["entries"]] == [
        {"id": index} for index in range(31)
    ]
    assert "next" not in second


@pytest.mark.asyncio
async def test_revoke_prevents_reading_a_saved_remote_result(context_service, host):
    service, registry, runner, calls = context_service
    receipt, _ = await service.content.present(
        {"sentinel": True}, context(host), "example", source="inspect"
    )
    host.resolve_agent(None, "alice").tool_access = ToolAccess(
        denied=(remote_tool_name("example", "inspect"),)
    )

    result = await registry.dispatch(context(host), receipt["read"])

    assert not result["ok"]


@pytest.mark.asyncio
async def test_connection_disconnect_keeps_the_fixed_model_definition(context_service):
    service, registry, runner, calls = context_service
    profile = ToolDefinitionProfileContext(agent_id="alice")
    before = registry.provider_definitions(profile_context=profile)

    await service.manage("disconnect", {"id": "example"})

    assert registry.provider_definitions(profile_context=profile) == before


@pytest.mark.asyncio
async def test_mcp_discovery_preserves_the_chat_prefix(context_service, host):
    from tests.core.chat.chat_loop_support import (
        StubAdapter,
        StubAgent,
        StubRuntime,
        build_chat_loop,
    )

    service, registry, runner, calls = context_service
    target = service._entries(runner, service._allowed(context(host)))[-1]["target"]
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "search",
                        "name": "mcp_example",
                        "arguments": {"action": "search", "query": "inspect"},
                    }
                ],
            },
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "describe",
                        "name": "mcp_example",
                        "arguments": {"action": "describe", "target": target},
                    }
                ],
            },
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call",
                        "name": "mcp_example",
                        "arguments": {
                            "action": "call",
                            "target": target,
                            "arguments": {"value": "sentinel"},
                        },
                    }
                ],
            },
            {"content": "finished"},
        ]
    )
    agent = StubAgent(id="alice", model="openai/gpt-5.2", allowed_tools=["*"])
    runtime = StubRuntime(data_dir=host.data_dir, agent=agent, adapter=adapter, tools=registry)
    runtime.chat_sessions.create("alice", session_id="session-one")
    run = await build_chat_loop(runtime).start_run("alice", "inspect", session_id="session-one")
    await run.wait()

    assert len(calls) == 1
    assert len(adapter.requests) == 4
    for previous, current in zip(adapter.requests, adapter.requests[1:], strict=False):
        assert current["kwargs"]["tools"] == previous["kwargs"]["tools"]
        assert current["messages"][: len(previous["messages"])] == previous["messages"]


@pytest.mark.asyncio
async def test_fixed_entry_point_uses_real_tools_resources_and_prompts(host, server, monkeypatch):
    api = ExtensionAPI("mcp", ExtensionDeclarations(), config={}, logger=logging.getLogger("test"))
    registry = ToolRegistry()
    api.operations.bind(registry)
    service = MCPService(api)
    await service.start(host)
    runner = runner_for(host, server, monkeypatch)
    service.connections[runner.id] = runner.config
    service.runners[runner.id] = runner
    runner.publish = service._publish
    try:
        await runner.invoke("catalog", {})
        before = registry.provider_definitions(
            profile_context=ToolDefinitionProfileContext(agent_id="alice")
        )
        for kind, inputs in (
            ("tool", {"value": "sentinel"}),
            ("resource", {}),
            ("prompt", {"subject": "scene"}),
        ):
            search = await service._browse(
                runner, context(host), {"action": "search", "kind": kind}
            )
            target = search["data"]["preview"]["matches"][0]["target"]
            detail = await service._browse(
                runner, context(host), {"action": "describe", "target": target}
            )
            result = await service._browse(
                runner, context(host), {"action": "call", "target": target, "arguments": inputs}
            )
            assert detail["ok"] and result["ok"]
            assert result["data"]["complete"]
        after = registry.provider_definitions(
            profile_context=ToolDefinitionProfileContext(agent_id="alice")
        )
        assert before == after
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "search", "unknown": True},
        {"action": "call"},
        {"action": "describe", "target": "connection", "query": "wrong"},
        {"action": "read", "result_id": "../invalid"},
    ],
)
async def test_browse_rejects_invalid_arguments_without_calling_server(
    context_service, host, arguments
):
    service, registry, runner, calls = context_service

    result = await registry.dispatch(context(host), arguments)

    assert not result["ok"]
    assert calls == []


@pytest.mark.asyncio
async def test_negative_page_limit_fails_contract_before_server_call(context_service, host):
    service, registry, runner, calls = context_service

    with pytest.raises(ToolContractError, match="minimum"):
        await registry.dispatch(context(host), {"action": "search", "limit": -1})

    assert calls == []


@pytest.mark.asyncio
async def test_cli_reads_saved_discovery_without_reconnecting(context_service, host):
    service, registry, runner, calls = context_service
    receipt, _ = await service.content.present({"items": [1, 2]}, context(host), "example")
    runner.state = "disconnected"

    result = await service._invoke_for_agent(
        runner,
        {"id": "example", "agent": "alice", "action": "read", "result_id": receipt["result_id"]},
    )

    assert result["ok"]
    assert result["data"]["entries"][0]["value"] == [1, 2]
    assert calls == []


@pytest.mark.asyncio
async def test_large_error_keeps_full_payload_and_bounded_receipt(context_service, host):
    service, registry, runner, calls = context_service
    payload = {"isError": True, "content": [{"type": "text", "text": "failure" * 3000}]}

    result = await service._present(runner, context(host), payload)
    receipt = json.loads(result["error"]["message"])
    saved = await service.content.load_result(receipt["result_id"], context(host), "example")

    assert not result["ok"]
    assert len(result["error"]["message"]) < 6000
    assert saved["payload"] == payload


@pytest.mark.asyncio
async def test_oversized_object_keys_return_file_without_unbounded_context(host):
    store = ContentStore(host, host.data_dir)
    payload = {"key" * 3000: "retained"}
    receipt, _ = await store.present(payload, context(host), "example")
    saved = await store.load_result(receipt["result_id"], context(host), "example")

    result = store.read_result(saved, receipt["read"])

    assert len(json.dumps(result)) < 6000
    assert result["result_file"] == receipt["result_file"]
    assert saved["payload"] == payload
