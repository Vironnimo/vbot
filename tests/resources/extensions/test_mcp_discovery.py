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
from resources.extensions.mcp.extension import MCPService, register, remote_tool_name
from tests.resources.extensions.mcp_helpers import (
    context,
    model_text,
    payloads,
    runner_for,
    targets,
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
    before = registry.provider_definitions(profile_context=profile, allowed_tools=["mcp_example"])
    runner.catalog["tools"].extend(
        {
            "name": f"tool_{index}",
            "description": "long external description " * 100,
            "inputSchema": {"type": "object", "properties": {"field": {"type": "string"}}},
        }
        for index in range(500)
    )
    service._publish(runner, runner.catalog)
    after = registry.provider_definitions(profile_context=profile, allowed_tools=["mcp_example"])

    assert after == before
    assert [entry["name"] for entry in after] == ["mcp_example"]
    assert len(registry.list_tools()) == 502
    assert [tool.name for tool in registry.list_tools(include_catalog_hidden=False)] == [
        "mcp_example"
    ]
    assert service.api.operations.catalog_visible_tool_names == ("mcp_example",)
    assert registry.prompt_definitions(profile_context=profile, allowed_tools=["mcp_example"]) == [
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
    assert "agent_access" not in inspection
    assert selection["tools"][0]["requires_opt_in"] is True
    assert calls == []


@pytest.mark.asyncio
async def test_search_and_describe_load_only_the_requested_definition(context_service, host):
    service, registry, runner, calls = context_service
    result = await service._browse(
        runner, context(host), {"action": "search", "query": "inspection", "kind": "tool"}
    )
    target = targets(result)[0]
    detail = await service._browse(runner, context(host), {"action": "describe", "target": target})

    assert detail["data"]["arguments_schema"] == runner.catalog["tools"][0]["inputSchema"]
    assert detail["data"]["description"] == "test-owned-inspection"
    assert "definition" not in detail["data"]
    assert "resources/read" not in json.dumps(detail)
    assert calls == []


@pytest.mark.asyncio
async def test_discovery_leads_with_tools_and_delivers_guidance(context_service, host):
    service, registry, runner, calls = context_service
    runner.catalog["prompts"] = [{"name": "workflow", "description": "test-owned-workflow"}]
    result = await registry.dispatch(
        context(host), {"action": "search"}, allowed_tools=["mcp_example"]
    )
    listed = targets(result)
    assert listed[0].startswith("tool:inspect:")
    assert result["data"]["content"].endswith(
        "Server guidance (external, from the MCP server):\ntest-owned-guidance"
    )
    prompt = next(target for target in listed if target.startswith("prompt:"))
    detail = await registry.dispatch(
        context(host), {"action": "describe", "target": prompt}, allowed_tools=["mcp_example"]
    )
    assert detail["data"]["target"] == prompt
    assert detail["data"]["description"] == "test-owned-workflow"
    assert prompt in detail["data"]["call"]
    assert calls == []


@pytest.mark.asyncio
async def test_guidance_and_prompts_lead_only_the_unfiltered_first_page(context_service, host):
    service, registry, runner, calls = context_service
    runner.catalog["prompts"] = [
        {"name": f"workflow_{index}", "description": f"test-owned-workflow-{index}"}
        for index in range(5)
    ]
    runner.catalog["tools"] = [
        {"name": f"tool_{index:02d}", "description": "scene", "inputSchema": {"type": "object"}}
        for index in range(12)
    ]
    service._publish(runner, runner.catalog)

    browse = await registry.dispatch(
        context(host), {"action": "search"}, allowed_tools=["mcp_example"]
    )
    query = await registry.dispatch(
        context(host), {"action": "search", "query": "scene"}, allowed_tools=["mcp_example"]
    )

    text = model_text(browse)
    assert "available: 12 tools, 5 prompts" in text
    assert "Server guidance (external, from the MCP server):\ntest-owned-guidance" in text
    # Prompts outside the first page are summarized with the call that lists them all.
    assert "Prompts (server workflows):\nprompt:workflow_0:" in text
    assert '2 more: {"action":"search","kind":"prompt"}' in text
    assert "test-owned-guidance" not in model_text(query)
    assert query["data"]["server_guidance"] == 'shown by {"action":"search"}'
    assert calls == []


@pytest.mark.asyncio
async def test_no_match_provides_a_working_capability_browse(context_service, host):
    service, registry, runner, calls = context_service
    result = await registry.dispatch(
        context(host), {"action": "search", "query": "rendern"}, allowed_tools=["mcp_example"]
    )
    assert result["data"]["matches"] == "none"
    assert result["data"]["available"] == "1 tool"
    assert "does not establish that the task is unsupported" in result["data"]["note"]
    fallback = await registry.dispatch(
        context(host), result["data"]["next"], allowed_tools=["mcp_example"]
    )
    assert [target.split(":")[1] for target in targets(fallback)] == ["inspect"]


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
        context(host),
        {"action": "search", "query": "scene material", "kind": "tool"},
        allowed_tools=["mcp_example"],
    )
    assert result["data"]["matches"] == "1-10 of 14"
    assert targets(result)[0].startswith("tool:scene_12:")
    following = await registry.dispatch(
        context(host), result["data"]["next"], allowed_tools=["mcp_example"]
    )
    assert following["data"]["matches"] == "11-14 of 14"
    names = [target.split(":")[1] for target in targets(result) + targets(following)]
    assert len(set(names)) == 14
    assert "next" not in following["data"]


@pytest.mark.asyncio
async def test_long_server_guidance_is_explicitly_incomplete_and_readable(context_service, host):
    service, registry, runner, calls = context_service
    runner.catalog["instructions"] = "test-owned-guidance " * 500
    result = await registry.dispatch(
        context(host), {"action": "search"}, allowed_tools=["mcp_example"]
    )
    assert "remaining server guidance" in result["data"]["guidance"]
    text = result["data"]["content"].split("(external, from the MCP server):\n", 1)[1]
    next_read = result["data"]["guidance_read"]
    while next_read:
        part = await registry.dispatch(context(host), next_read, allowed_tools=["mcp_example"])
        text += part["data"]["content"]
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
    result = await registry.dispatch(
        context(host), {"action": "search", "kind": "tool"}, allowed_tools=["mcp_example"]
    )
    assert [target.split(":")[1] for target in targets(result)] == ["inspect"]


@pytest.mark.asyncio
async def test_tool_disabled_during_connection_does_not_disclose_catalog(
    context_service, host, monkeypatch
):
    service, registry, runner, calls = context_service
    runner.state = "connecting"

    async def connect(*args):
        host.resolve_agent(None, "alice").tool_access = ToolAccess(mode="none")
        runner.state = "connected"

    monkeypatch.setattr(runner, "invoke", connect)
    result = await registry.dispatch(
        context(host), {"action": "search"}, allowed_tools=["mcp_example"]
    )
    assert result["ok"] is False
    assert result["data"] is None


@pytest.mark.asyncio
async def test_no_match_fallback_does_not_reveal_denied_tools(context_service, host):
    service, registry, runner, calls = context_service
    host.resolve_agent(None, "alice").tool_access = ToolAccess(
        granted=("mcp_example",), denied=[remote_tool_name("example", "inspect")]
    )
    result = await registry.dispatch(
        context(host), {"action": "search", "query": "missing"}, allowed_tools=["mcp_example"]
    )
    assert result["data"]["available"] == "no tools, resources or prompts"
    fallback = await registry.dispatch(
        context(host), result["data"]["next"], allowed_tools=["mcp_example"]
    )
    assert fallback["data"]["matches"] == "none"
    assert targets(fallback) == []
    denied = await registry.dispatch(
        context(host),
        {"action": "call", "target": "inspect", "arguments": {"value": "sentinel"}},
        allowed_tools=["mcp_example"],
    )
    assert denied["error"]["code"] == "mcp_unknown_target"
    assert "tool:inspect" not in denied["error"]["message"]
    assert calls == []


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

    class McpAgent(StubAgent):
        @property
        def tool_access(self):
            return ToolAccess(mode="selected", allowed=("mcp_example",), granted=("mcp_example",))

    agent = McpAgent(id="alice", model="openai/gpt-5.2")
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
            profile_context=ToolDefinitionProfileContext(agent_id="alice"),
            allowed_tools=["mcp_example"],
        )
        expected = {
            "tool": '"value": "sentinel"',
            "resource": "test-owned-scene",
            "prompt": "[user]\ntest-owned-workflow:scene",
        }
        for kind, inputs in (
            ("tool", {"value": "sentinel"}),
            ("resource", {}),
            ("prompt", {"subject": "scene"}),
        ):
            search = await service._browse(
                runner, context(host), {"action": "search", "kind": kind}
            )
            target = targets(search)[0]
            detail = await service._browse(
                runner, context(host), {"action": "describe", "target": target}
            )
            result = await service._browse(
                runner, context(host), {"action": "call", "target": target, "arguments": inputs}
            )
            assert detail["ok"] and result["ok"]
            assert expected[kind] in result["data"]["content"]
            # The SDK's structured copy of the returned value repeats the text.
            assert "structuredContent" not in result["data"]
        after = registry.provider_definitions(
            profile_context=ToolDefinitionProfileContext(agent_id="alice"),
            allowed_tools=["mcp_example"],
        )
        assert before == after
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments,message",
    [
        ({"action": "call"}, "call was not run: target is missing"),
        (
            {"action": "describe", "target": "connection", "query": "wrong"},
            'query does not apply to describe, which takes target. Send {"action":"describe",'
            '"target":"connection"}.',
        ),
    ],
)
async def test_browse_rejects_invalid_arguments_without_calling_server(
    context_service, host, arguments, message
):
    service, registry, runner, calls = context_service

    with pytest.raises(ToolContractError) as refusal:
        await registry.dispatch(context(host), arguments, allowed_tools=["mcp_example"])

    assert message in str(refusal.value)
    assert calls == []


@pytest.mark.asyncio
async def test_read_of_an_unknown_result_names_what_to_use(context_service, host):
    service, registry, runner, calls = context_service

    result = await registry.dispatch(
        context(host), {"action": "read", "result_id": "../invalid"}, allowed_tools=["mcp_example"]
    )

    assert result["error"]["code"] == "invalid_arguments"
    assert "Use a result_id that this connection returned here" in result["error"]["message"]
    assert calls == []


@pytest.mark.asyncio
async def test_browse_rejects_unknown_arguments_before_calling_server(context_service, host):
    service, registry, runner, calls = context_service

    with pytest.raises(
        ToolContractError,
        match="unknown is not a field of mcp_example. search takes query, kind, offset, limit",
    ):
        await registry.dispatch(
            context(host), {"action": "search", "unknown": True}, allowed_tools=["mcp_example"]
        )

    assert calls == []


@pytest.mark.asyncio
async def test_negative_page_limit_fails_contract_before_server_call(context_service, host):
    service, registry, runner, calls = context_service

    with pytest.raises(ToolContractError, match='"limit" must be at least 1; received -1'):
        await registry.dispatch(
            context(host), {"action": "search", "limit": -1}, allowed_tools=["mcp_example"]
        )

    assert calls == []


@pytest.mark.asyncio
async def test_cli_explore_and_invoke_return_the_complete_payload_inline(context_service, host):
    service, registry, runner, calls = context_service
    runner.catalog["instructions"] = "test-owned-guidance " * 500

    search = await service._invoke_for_agent(
        runner, {"id": "example", "agent": "alice", "action": "search"}
    )
    invoke = await service._invoke_for_agent(
        runner,
        {
            "id": "example",
            "agent": "alice",
            "operation": "tools/call",
            "arguments": {"name": "inspect", "arguments": {"value": "x" * 7000}},
        },
    )

    # A management call has no Session that could read a saved result later.
    assert search["ok"] and invoke["ok"]
    assert set(search["data"]) == {"complete", "value"}
    guidance = search["data"]["value"]["server_guidance"]["instructions"]
    assert guidance == runner.catalog["instructions"]
    assert invoke["data"] == {
        "complete": True,
        "value": {
            "content": [{"type": "text", "text": "x" * 7000}],
            "structuredContent": {"sentinel": True},
            "_meta": {"retained": True},
        },
    }
    assert payloads(host).rows == {}


@pytest.mark.asyncio
async def test_cli_explore_offers_no_read(tmp_path):
    api = ExtensionAPI("mcp", ExtensionDeclarations(), config={}, logger=logging.getLogger("test"))
    register(api)
    explore = next(item for item in api.operations.describe() if item["name"] == "explore")

    assert explore["parameters"]["properties"]["action"]["enum"] == ["search", "describe", "call"]
    assert {"result_id", "pointer", "fields"}.isdisjoint(explore["parameters"]["properties"])
    with pytest.raises(ValueError, match="Invalid operation arguments"):
        await api.operations.invoke(
            "explore",
            {"id": "example", "agent": "alice", "action": "read", "result_id": "res_x"},
        )
