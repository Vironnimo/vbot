"""Shared fixtures and fakes for mcp behavior tests."""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import pytest
import pytest_asyncio
from mcp.server import MCPServer

from core.extensions.extensions import ExtensionAPI, ExtensionDeclarations
from core.extensions.operations import ExtensionHost
from core.tools.availability import ToolAccess
from core.tools.tools import ToolContext, ToolRegistry
from core.utils.ids import new_id
from resources.extensions.mcp.client import ConnectionRunner
from resources.extensions.mcp.config import validate_connection
from resources.extensions.mcp.extension import MCPService
from resources.extensions.mcp.interactions import InputRequests


class SessionPayloads:
    """Result payloads kept with Tool Results, visible in the Session that received them.

    Stands in for Chat staging and the Session store: the real visibility rule
    (own and inherited history) is covered by the Session and host tests.
    """

    def __init__(self) -> None:
        self.rows: dict[str, tuple[tuple[str | None, str, str], str]] = {}

    def attach(self, context: ToolContext, payload: Any) -> str:
        identifier = new_id("res")
        self.rows[identifier] = (_address(context), json.dumps(payload))
        return identifier

    async def load(self, context: ToolContext, payload_id: str) -> Any:
        row = self.rows.get(payload_id)
        if not context.result_payloads_available or row is None or row[0] != _address(context):
            return None
        return json.loads(row[1])


def _address(context: ToolContext) -> tuple[str | None, str, str]:
    return (context.project_id, context.agent_id, context.session_id)


def model_text(result: dict[str, Any]) -> str:
    """Return the plain text the Model reads for a Tool Result."""
    from core.providers.adapter import tool_result_text

    return str(tool_result_text(json.dumps(result)))


def targets(result: dict[str, Any]) -> list[str]:
    """Return the targets a search result lists, in order."""
    lines = str(result["data"].get("content", "")).splitlines()
    kinds = ("tool:", "resource:", "template:", "prompt:", "operation:", "connection:")
    return [line.split(": ", 1)[0] for line in lines if line.startswith(kinds)]


@pytest.fixture
def host(tmp_path):
    async def sample(context, request):
        return {"model": "test/model", "content": "sampled", "usage": {"input_tokens": 2}}

    credentials = {}
    agent = SimpleNamespace(
        tool_access=ToolAccess(granted=("mcp_example",)),
        memory_prompt_mode="off",
        workspace=str(tmp_path),
    )
    payloads = SessionPayloads()
    state_dir = tmp_path / "extension-data" / "mcp"
    state_dir.mkdir(parents=True)
    return ExtensionHost(
        data_dir=tmp_path,
        sample=sample,
        resolve_agent=lambda project, name: agent,
        resolve_tool_agent=lambda context: agent,
        store_attachment=lambda name, data: SimpleNamespace(
            id="blob", file_path=tmp_path / name, filename=name, media_type="image/png"
        ),
        resolve_credential=lambda key: credentials.get(key, ""),
        set_credential=lambda key, value: credentials.__setitem__(key, value),
        state_dir=state_dir,
        load_result_payload=payloads.load,
    )


def payloads(host) -> SessionPayloads:
    """The fake payload store behind *host*."""
    return cast(SessionPayloads, host.load_result_payload.__self__)


def context(host, agent="alice", project=None, session: str | None = "session"):
    """A Tool call in *session*; ``None`` is a call outside any Session."""
    call = ToolContext(
        agent_id=agent,
        project_id=project,
        session_id=session or "mcp-management",
        run_id="run",
        tool_call_id="call",
        tool_name="mcp_example",
        tool_call_index=0,
        workspace=host.data_dir,
        vbot_root=host.data_dir,
        data_root=host.data_dir,
    )
    if session is None:
        return call
    store = payloads(host)
    return replace(
        call,
        result_payload_hook=lambda _call_id, _tool_name, payload: store.attach(call, payload),
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
        validate_connection({"id": "example", "transport": "stdio", "command": sys.executable}),
        host,
        InputRequests(),
        lambda *args: None,
    )

    async def transport(stack):
        return server

    monkeypatch.setattr(runner, "_transport", transport)
    return runner


@pytest_asyncio.fixture
async def context_service(host, monkeypatch):
    api = ExtensionAPI("mcp", ExtensionDeclarations(), config={}, logger=logging.getLogger("test"))
    registry = ToolRegistry()
    api.operations.bind(registry)
    service = MCPService(api)
    await service.start(host)
    service.connections["example"] = validate_connection(
        {"id": "example", "transport": "stdio", "command": "unused"}
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
