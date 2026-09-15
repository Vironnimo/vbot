"""Resolve MCP callers through their canonical temporary Session binding."""

import logging
from dataclasses import replace

import pytest

from core.agents.temporary import TemporaryAgentConfig
from core.extensions.extensions import ExtensionAPI, ExtensionDeclarations
from core.runs import RunExecutionOwner
from core.runtime.runtime import Runtime
from core.tools.availability import ToolAccess
from core.tools.tools import ToolContext
from core.utils.config import Config
from resources.extensions.mcp.config import validate_connection
from resources.extensions.mcp.extension import MCPService


@pytest.mark.asyncio
async def test_temporary_mcp_caller_uses_bound_policy_and_keeps_generation_scope(tmp_path):
    runtime = Runtime(Config(data_dir=tmp_path / "data"))
    runtime.start()
    service = None
    try:
        binding = runtime._temporary_agents.create(
            owner_name="swarm",
            group_id="swarm-test",
            participant_id="peer",
            config=TemporaryAgentConfig(
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
        host = runtime._extension_host()
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
        runtime.stop()
