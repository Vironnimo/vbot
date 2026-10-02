"""MCP test support: Tool contexts, a fake result payload store and service helpers."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any, cast

import anyio
import pytest

from core.extensions.extensions import ExtensionAPI, ExtensionDeclarations
from core.extensions.operations import ExtensionHost
from core.tools.availability import resolve_tool_access
from core.tools.tools import ToolContext, ToolRegistry
from core.utils.ids import new_id
from resources.extensions.mcp.client import ConnectionRunner
from resources.extensions.mcp.config import validate_connection
from resources.extensions.mcp.extension import MCPService
from resources.extensions.mcp.interactions import InputRequests

CONNECTION_TOOL = "mcp_example"


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


def payloads(host: ExtensionHost) -> SessionPayloads:
    """The fake payload store behind *host*."""
    return cast(SessionPayloads, host.load_result_payload.__self__)  # type: ignore[union-attr]


def context(
    host: ExtensionHost,
    agent: str = "alice",
    project: str | None = None,
    session: str | None = "session",
) -> ToolContext:
    """A call of the connection Tool in *session*; ``None`` is a call outside any Session."""
    call = ToolContext(
        agent_id=agent,
        project_id=project,
        session_id=session or "mcp-management",
        run_id="run",
        tool_call_id="call",
        tool_name=CONNECTION_TOOL,
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


def model_text(result: dict[str, Any]) -> str:
    """Return the plain text the Model reads for a Tool Result."""
    from core.providers.adapter import tool_result_text

    return str(tool_result_text(json.dumps(result)))


def targets(result: dict[str, Any]) -> list[str]:
    """Return the targets a search result lists, in order."""
    lines = str(result["data"].get("content", "")).splitlines()
    kinds = ("tool:", "resource:", "template:", "prompt:", "operation:", "connection:")
    return [line.split(": ", 1)[0] for line in lines if line.startswith(kinds)]


async def start_service(host: ExtensionHost) -> tuple[MCPService, ToolRegistry]:
    """Start the MCP service on *host* with its Tools bound to a fresh registry."""
    api = ExtensionAPI(
        "mcp", ExtensionDeclarations(), config={}, logger=logging.getLogger("test.mcp")
    )
    registry = ToolRegistry()
    api.operations.bind(registry)
    service = MCPService(api)
    await service.start(host)
    return service, registry


async def dispatch(
    registry: ToolRegistry, host: ExtensionHost, arguments: dict[str, Any], **call: Any
) -> dict[str, Any]:
    """Call the connection Tool as an Agent that may use it."""
    return await registry.dispatch(
        context(host, **call), arguments, allowed_tools=[CONNECTION_TOOL]
    )


async def tool_target(registry: ToolRegistry, host: ExtensionHost) -> str:
    """The target a Tool search currently lists for the connection's remote Tool."""
    return targets(await dispatch(registry, host, {"action": "search", "kind": "tool"}))[-1]


async def operation_target(registry: ToolRegistry, host: ExtensionHost, operation: str) -> str:
    """The target a search lists for the protocol *operation*."""
    listed = targets(
        await dispatch(
            registry, host, {"action": "search", "kind": "operation", "query": operation}
        )
    )
    return next(target for target in listed if target.startswith(f"operation:{operation}:"))


def allowed_tools(registry: ToolRegistry, host: ExtensionHost) -> tuple[str, ...]:
    """The Tools the test Agent may use under its current policy."""
    agent = host.resolve_agent(None, "alice")
    return resolve_tool_access(agent.tool_access, registry.list_tools(), "off").allowed_tools


def runner_for(
    host: ExtensionHost, server: Any, monkeypatch: pytest.MonkeyPatch
) -> ConnectionRunner:
    """A connection runner whose transport is the in-memory *server*."""
    runner = ConnectionRunner(
        validate_connection({"id": "example", "transport": "stdio", "command": sys.executable}),
        host,
        InputRequests(),
        lambda *args: None,
    )

    async def transport(stack: Any) -> Any:
        return server

    monkeypatch.setattr(runner, "_transport", transport)
    return runner


class StreamServer:
    """An in-memory *server* reached over message streams, like a stdio subprocess.

    Each connection runs a fresh server session; ``kill`` ends the current one the
    way a crashed server process does, so the client sees its stream end.
    """

    def __init__(self, server: Any) -> None:
        self._server = getattr(server, "_lowlevel_server", server)
        self._to_client: Any = None
        self._session: Any = None
        self.connections = 0

    @asynccontextmanager
    async def _open(self) -> AsyncIterator[tuple[Any, Any]]:
        to_client, client_read = anyio.create_memory_object_stream[Any](0)
        client_write, from_client = anyio.create_memory_object_stream[Any](0)
        self._to_client = to_client
        self.connections += 1

        async def serve() -> None:
            async with to_client, from_client:
                await self._server.run(
                    from_client, to_client, self._server.create_initialization_options()
                )

        async with anyio.create_task_group() as group:
            group.start_soon(serve)
            try:
                yield client_read, client_write
            finally:
                await client_write.aclose()
                group.cancel_scope.cancel()

    async def __aenter__(self) -> tuple[Any, Any]:
        self._session = self._open()
        return cast(tuple[Any, Any], await self._session.__aenter__())

    async def __aexit__(self, *exc: Any) -> bool | None:
        return cast(bool | None, await self._session.__aexit__(*exc))

    async def kill(self) -> None:
        """End the current server session: the client's read stream ends."""
        await self._to_client.aclose()
