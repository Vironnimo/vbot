"""Resolve MCP callers: temporary Session bindings and management calls outside any Session."""

import asyncio
import logging
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import mcp.types as types
import pytest
from mcp.server import Server

from core.agents.temporary import TemporaryAgentConfig
from core.extensions.extensions import ExtensionAPI, ExtensionDeclarations
from core.runs import RunExecutionOwner
from core.runtime.runtime import Runtime
from core.tools.availability import ToolAccess
from core.tools.tools import ToolContext
from core.utils.config import Config
from resources.extensions.mcp.client import ConnectionRunner
from resources.extensions.mcp.config import validate_connection
from resources.extensions.mcp.extension import MCPService


@pytest.mark.asyncio
async def test_temporary_mcp_caller_uses_bound_policy_and_keeps_generation_scope(
    config: Config, tmp_path: Path
) -> None:
    runtime = Runtime(config)
    runtime.start()
    service = None
    try:
        assert runtime.extensions is not None
        root = runtime._extension_host()  # noqa: SLF001 - the Runtime hands hosts only to Extensions.
        assert root.for_owner is not None
        groups = root.for_owner(runtime.extensions.registration_identity("swarm")).temporary_agents
        assert groups is not None
        binding = await groups.create(
            "swarm-test",
            "peer",
            TemporaryAgentConfig(
                model="fixture/model",
                cwd=tmp_path,
                tool_access=ToolAccess(
                    mode="selected", allowed=("mcp_example",), granted=("mcp_example",)
                ),
                allowed_skills=[],
                tools={},
                name="Peer",
            ),
        )
        owner = RunExecutionOwner("swarm", "swarm-test", "peer", binding.generation_id, "epoch")
        ctx = ToolContext(
            agent_id=binding.address.agent_id,
            session_id=binding.address.session_id,
            project_id=binding.address.project_id,
            run_id="test-run",
            tool_call_id="test-call",
            tool_name="mcp_example",
            tool_call_index=0,
            workspace=tmp_path,
            vbot_root=tmp_path,
            data_root=tmp_path,
            execution_owner=owner,
        )
        host = root.for_owner(runtime.extensions.registration_identity("mcp"))
        agent = host.resolve_tool_agent(ctx)
        assert agent.tool_access.granted == ("mcp_example",)
        assert not runtime.agents.exists(ctx.agent_id)
        api = ExtensionAPI(
            "mcp-test", ExtensionDeclarations(), config={}, logger=logging.getLogger("test")
        )
        api.operations.bind(runtime.tools)
        service = MCPService(api)
        await service.start(host)
        service.connections["example"] = validate_connection(
            {"id": "example", "transport": "stdio", "command": "unused"}
        )
        runner = service._runner(service.connections["example"])
        runner.state = "connected"
        runner.catalog = {"tools": [], "resources": [], "resource_templates": [], "prompts": []}
        result = await runtime.tools.dispatch(ctx, {"action": "search"}, service._allowed(ctx))
        assert result["ok"]
        for invalid in (
            None,
            replace(owner, generation_id="replaced"),
            replace(owner, participant_id="other"),
        ):
            with pytest.raises(ValueError):
                host.resolve_tool_agent(replace(ctx, execution_owner=invalid))
    finally:
        if service is not None:
            await service.close()
        await runtime.aclose()


def _asking_server() -> Server:
    """A server whose Tool asks the user one question before it answers."""

    async def call(server_context: Any, params: Any) -> Any:
        if params.input_responses:
            name = params.input_responses["name"].content["name"]
            return types.CallToolResult(content=[types.TextContent(type="text", text=name)])
        return types.InputRequiredResult.model_validate(
            {
                "resultType": "input_required",
                "inputRequests": {
                    "name": {
                        "method": "elicitation/create",
                        "params": {
                            "message": "test-owned-question",
                            "requestedSchema": {
                                "type": "object",
                                "properties": {"name": {"type": "string"}},
                                "required": ["name"],
                            },
                        },
                    }
                },
            }
        )

    async def list_tools(server_context: Any, params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[types.Tool(name="ask", input_schema={"type": "object"})]
        )

    return Server("asking", on_call_tool=call, on_list_tools=list_tools)


@pytest.mark.asyncio
async def test_management_explore_and_invoke_run_as_the_agent_outside_any_session(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _asking_server()

    async def transport(runner: Any, stack: Any) -> Any:
        return server

    monkeypatch.setattr(ConnectionRunner, "_transport", transport)
    runtime = Runtime(config)
    runtime.start()
    service = None
    try:
        assert runtime.extensions is not None
        runtime.agents.create("mcptest", tool_access=ToolAccess(granted=("mcp_example",)))
        root = runtime._extension_host()  # noqa: SLF001 - the Runtime hands hosts only to Extensions.
        assert root.for_owner is not None
        host = root.for_owner(runtime.extensions.registration_identity("mcp"))
        api = ExtensionAPI(
            "mcp-test", ExtensionDeclarations(), config={}, logger=logging.getLogger("test")
        )
        api.operations.bind(runtime.tools)
        service = MCPService(api)
        await service.start(host)
        service.connections["example"] = validate_connection(
            {"id": "example", "transport": "stdio", "command": sys.executable}
        )
        service._runner(service.connections["example"])

        async def finished(job: dict[str, Any]) -> dict[str, Any]:
            return await service.jobs.wait(job["job_id"])

        invoke = await service.manage(
            "invoke",
            {
                "id": "example",
                "agent": "mcptest",
                "operation": "tools/call",
                "arguments": {"name": "ask", "arguments": {}},
            },
        )
        async with asyncio.timeout(10):
            while not service.inputs.list():
                status = service.jobs.status(invoke["job_id"])
                assert status["state"] == "running", status
                await asyncio.sleep(0)
        pending = service.inputs.list()[0]
        # The question is listed for the user; no Session asked it.
        assert (pending["kind"], pending["session_id"]) == ("elicitation", None)
        service.inputs.respond(pending["id"], {"action": "accept", "content": {"name": "answer"}})
        called = await asyncio.wait_for(finished(invoke), 10)
        explored = await asyncio.wait_for(
            finished(
                await service.manage(
                    "explore", {"id": "example", "agent": "mcptest", "action": "search"}
                )
            ),
            10,
        )
    finally:
        if service is not None:
            await service.close()
        await runtime.aclose()

    assert called["state"] == "completed", called
    assert called["result"]["data"]["value"]["content"][0]["text"] == "answer"
    assert explored["state"] == "completed", explored
    assert [match["name"] for match in explored["result"]["data"]["value"]["matches"]] == ["ask"]
