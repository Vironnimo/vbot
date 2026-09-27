"""MCP fixtures: an Extension host, an in-memory server and a connected service."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from mcp.server import MCPServer

from core.extensions.operations import ExtensionHost
from core.tools.availability import ToolAccess
from resources.extensions.mcp.config import validate_connection
from tests.resources.extensions.mcp.mcp_test_support import SessionPayloads, start_service


@pytest.fixture
def host(tmp_path: Path) -> ExtensionHost:
    async def sample(context: Any, request: Any) -> dict[str, Any]:
        return {"model": "test/model", "content": "sampled", "usage": {"input_tokens": 2}}

    credentials: dict[str, str] = {}
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


@pytest.fixture
def server() -> MCPServer:
    server = MCPServer("test", instructions="test-owned-server-instructions")

    @server.tool()
    def echo(value: str) -> dict[str, str]:
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


@pytest_asyncio.fixture
async def context_service(
    host: ExtensionHost, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[Any, Any, Any, list[tuple[str, dict[str, Any]]]]]:
    """A service with one connected connection whose remote calls are recorded, not sent."""
    service, registry = await start_service(host)
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
    calls: list[tuple[str, dict[str, Any]]] = []

    async def invoke(
        operation: str, arguments: dict[str, Any], invocation_context: Any = None
    ) -> dict[str, Any]:
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
