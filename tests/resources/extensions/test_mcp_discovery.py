"""Mcp: discovery behavior."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from core.extensions.extensions import ExtensionAPI, ExtensionDeclarations
from core.tools.availability import ToolAccess
from core.tools.contracts import ToolContractError
from core.tools.tools import ToolDefinitionProfileContext, ToolRegistry
from resources.extensions.mcp.extension import MCPService, remote_tool_name
from tests.resources.extensions.mcp_helpers import (
    context,
    runner_for,
)
from tests.resources.extensions.mcp_helpers import (
    context_service as context_service,
)
from tests.resources.extensions.mcp_helpers import (
    host as host,
)
from tests.resources.extensions.mcp_helpers import (
    server as server,
)


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
    assert [tool.name for tool in registry.list_tools(include_catalog_hidden=False)] == [
        "mcp_example"
    ]
    assert service.api.operations.catalog_visible_tool_names == ("mcp_example",)
    assert registry.prompt_definitions(profile_context=profile) == [
        {"name": before[0]["name"], "description": before[0]["description"]}
    ]


@pytest.mark.asyncio
async def test_tool_selection_shows_connection_and_inspector_keeps_remote_names(context_service):
    from server.rpc.catalog_methods import _list_tools

    service, registry, runner, calls = context_service
    remote_name = "get_blendfile_object_materials"
    runner.catalog["tools"][0]["name"] = remote_name
    service._publish(runner, runner.catalog)

    selection = _list_tools(SimpleNamespace(runtime=SimpleNamespace(tools=registry)), {})
    assert [tool["name"] for tool in selection["tools"]] == ["mcp_example"]
    assert registry.get(remote_tool_name("example", remote_name)) is not None

    inspection = await service.manage("inspect", {"id": "example"})
    assert [tool["name"] for tool in inspection["tools"]] == [remote_name]
    assert inspection["agent_access"] == [{"agent": "alice", "access": "allowed", "tool_count": 1}]
    assert calls == []


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
