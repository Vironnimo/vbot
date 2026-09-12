"""Shared fixtures and fakes for mcp behavior tests."""

from __future__ import annotations

import logging
import sys
from types import SimpleNamespace

import pytest
import pytest_asyncio
from mcp.server import MCPServer

from core.extensions.extensions import ExtensionAPI, ExtensionDeclarations
from core.extensions.operations import ExtensionHost
from core.tools.availability import ToolAccess
from core.tools.tools import ToolContext, ToolRegistry
from resources.extensions.mcp.client import ConnectionRunner
from resources.extensions.mcp.config import validate_connection
from resources.extensions.mcp.extension import MCPService
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
