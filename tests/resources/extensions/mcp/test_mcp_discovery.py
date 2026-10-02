"""MCP: the connection Tool lists its remote Tools, which discovery finds and describes."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import override

import pytest

from core.extensions import ExtensionRecord, ExtensionRegistry
from core.extensions.extensions import ExtensionAPI, ExtensionDeclarations
from core.tools.availability import ToolAccess
from core.tools.tools import ToolDefinitionProfileContext
from resources.extensions.mcp.client import ConnectionRunner
from resources.extensions.mcp.extension import MCP_GUIDANCE, register, remote_tool_name
from tests.core.prompts.prompts_test_support import _agent, _manager
from tests.resources.extensions.mcp.mcp_test_support import (
    dispatch,
    model_text,
    payloads,
    start_service,
    targets,
    tool_target,
)

_PROFILE = ToolDefinitionProfileContext(agent_id="alice")


_USAGE = "Describe a tool for its arguments schema, then call it."


def _definitions(registry):
    return registry.provider_definitions(profile_context=_PROFILE, allowed_tools=["mcp_example"])


def _tool(name):
    return {"name": name, "description": "test-owned", "inputSchema": {"type": "object"}}


def test_remote_names_are_stable_unique_and_provider_safe():
    assert remote_tool_name("a" * 32, "b" * 200) == remote_tool_name("a" * 32, "b" * 200)
    assert len(remote_tool_name("a" * 32, "b" * 200)) == 64
    assert remote_tool_name("example", "a/b") != remote_tool_name("example", "a_b")


@pytest.mark.asyncio
async def test_connection_description_lists_remote_tool_names_through_disconnects(
    context_service,
):
    service, registry, runner, calls = context_service
    before = _definitions(registry)
    assert before[0]["description"] == f"MCP connection example. {_USAGE} Tools: inspect"
    runner.catalog["server_info"] = {"name": "blender-mcp", "title": "Blender"}
    runner.catalog["tools"].extend(
        {
            "name": f"tool_{index:03d}",
            "description": "long external description " * 100,
            "inputSchema": {"type": "object", "properties": {"field": {"type": "string"}}},
        }
        for index in range(500)
    )
    service._publish(runner, runner.catalog)
    after = _definitions(registry)

    # The parameters stay fixed; the description names the server and lists the Tools
    # in server order, cut to about 3000 characters with the count of the rest.
    assert [entry["name"] for entry in after] == ["mcp_example"]
    assert after[0]["parameters"] == before[0]["parameters"]
    heading, listing = after[0]["description"].split(" Tools: ", 1)
    assert heading == f"MCP connection example: Blender. {_USAGE}"
    *shown, more = listing.split(", ")
    names = [tool["name"] for tool in runner.catalog["tools"]]
    assert shown == names[: len(shown)]
    assert len(", ".join(shown)) <= 3000 < len(", ".join(names[: len(shown) + 1]))
    assert more == f"... and {len(names) - len(shown)} more; search lists all."
    assert len(registry.list_tools()) == 502
    assert [tool.name for tool in registry.list_tools(include_catalog_hidden=False)] == [
        "mcp_example"
    ]
    assert service.api.operations.catalog_visible_tool_names == ("mcp_example",)
    assert registry.prompt_definitions(profile_context=_PROFILE, allowed_tools=["mcp_example"]) == [
        {"name": after[0]["name"], "description": after[0]["description"]}
    ]

    # A disconnect keeps the names; only a new catalog changes them.
    await service.manage("disconnect", {"id": "example"})

    assert _definitions(registry) == after


@pytest.mark.asyncio
async def test_connection_tool_announces_added_and_removed_tool_names(context_service):
    service, registry, runner, calls = context_service
    note = registry.get("mcp_example").definition_change_note

    def published(**catalog):
        runner.catalog.update(catalog)
        service._publish(runner, runner.catalog)
        return _definitions(registry)[0]

    first = _definitions(registry)[0]
    added = published(tools=[_tool("inspect"), _tool("render")])
    assert note(first, added) == "Tools added on this connection: render."
    # A new server title or user description alone is not announced.
    titled = published(server_info={"name": "Blender"})
    assert titled["description"] != added["description"]
    assert note(added, titled) is None
    reordered = published(tools=[_tool("render"), _tool("inspect")])
    assert note(titled, reordered) is None
    changed = published(tools=[_tool("render"), _tool("snapshot")])
    assert note(reordered, changed) == (
        "Tools added on this connection: snapshot. Tools removed: inspect."
    )
    assert note(changed, published(tools=[])) == "Tools removed: render, snapshot."

    # A description published before a restart is read from its own Tool list. Added
    # names need its complete list; removed names are those it shows.
    current = published(tools=[_tool("inspect"), _tool("render")])
    earlier = f"MCP connection example: Tools: fake. {_USAGE} Tools: inspect, old_tool"
    assert note({**first, "description": earlier}, current) == (
        "Tools added on this connection: render. Tools removed: old_tool."
    )
    cut_short = (
        f"MCP connection example. {_USAGE} Tools: old_tool, ... and 3 more; search lists all."
    )
    assert note({**first, "description": cut_short}, current) == "Tools removed: old_tool."
    legacy = "Discover and use this MCP connection's tools, resources, and prompts."
    assert note({**first, "description": legacy}, current) is None


def test_guidance_block_renders_while_a_connection_tool_is_listed(tmp_path):
    declarations = ExtensionDeclarations()
    register(ExtensionAPI("mcp", declarations, config={}, logger=logging.getLogger("test.mcp")))
    extensions = ExtensionRegistry()
    extensions._records.append(
        ExtensionRecord(
            "mcp", tmp_path, tmp_path / "extension.py", "loaded", declarations=declarations
        )
    )
    manager = _manager(tmp_path, block_definitions=extensions.prompt_block_declarations())
    agent = _agent(tmp_path)

    def prompt(*tools):
        return manager.build_system_prompt(
            agent, effective_tool_definitions=[{"name": name} for name in tools]
        )

    assert MCP_GUIDANCE in prompt("read_file", "mcp_blender", "mcp_godot")
    assert prompt("read_file", "mcp_blender").count("## MCP connections") == 1
    assert "## MCP connections" not in prompt("read_file")


@pytest.mark.asyncio
async def test_tool_selection_shows_connection_and_inspector_keeps_remote_names(
    context_service, host
):
    service, registry, runner, calls = context_service
    remote_name = "get_blendfile_object_materials"
    runner.catalog["tools"][0]["name"] = remote_name
    service._publish(runner, runner.catalog)

    selection = registry.list_tools(include_catalog_hidden=False)
    assert [tool.name for tool in selection] == ["mcp_example"]
    assert selection[0].requires_opt_in is True
    assert registry.get(remote_tool_name("example", remote_name)) is not None

    inspection = await service.manage("inspect", {"id": "example"})
    assert [tool["name"] for tool in inspection["tools"]] == [remote_name]
    # The inspector names each Tool by the target the connection Tool's search lists.
    assert inspection["tools"][0]["target"] == await tool_target(registry, host)
    assert "agent_access" not in inspection
    assert calls == []


@pytest.mark.asyncio
async def test_search_and_describe_load_only_the_requested_definition(context_service, host):
    service, registry, runner, calls = context_service
    target = targets(
        await dispatch(registry, host, {"action": "search", "query": "inspection", "kind": "tool"})
    )[0]
    detail = await dispatch(registry, host, {"action": "describe", "target": target})

    assert detail["data"]["arguments_schema"] == runner.catalog["tools"][0]["inputSchema"]
    assert detail["data"]["description"] == "test-owned-inspection"
    assert "definition" not in detail["data"]
    assert "resources/read" not in json.dumps(detail)
    assert calls == []


@pytest.mark.asyncio
async def test_discovery_leads_with_tools_and_delivers_guidance(context_service, host):
    service, registry, runner, calls = context_service
    runner.catalog["prompts"] = [{"name": "workflow", "description": "test-owned-workflow"}]
    result = await dispatch(registry, host, {"action": "search"})
    listed = targets(result)
    assert listed[0].startswith("tool:inspect:")
    assert result["data"]["content"].endswith(
        "Server guidance (external, from the MCP server):\ntest-owned-guidance"
    )
    prompt = next(target for target in listed if target.startswith("prompt:"))
    detail = await dispatch(registry, host, {"action": "describe", "target": prompt})
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

    browse = await dispatch(registry, host, {"action": "search"})
    query = await dispatch(registry, host, {"action": "search", "query": "scene"})

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
@pytest.mark.parametrize(
    "kind,searched", [(None, "tools, resources, templates or prompts"), ("prompt", "prompts")]
)
async def test_no_match_provides_a_working_capability_browse(context_service, host, kind, searched):
    service, registry, runner, calls = context_service
    arguments = {"action": "search", "query": "rendern"} | ({"kind": kind} if kind else {})
    result = await dispatch(registry, host, arguments)
    assert result["data"]["matches"] == "none"
    assert result["data"]["available"] == "1 tool"
    assert result["data"]["note"] == (
        f"No {searched} matched these words. This does not establish that the task is "
        "unsupported. Browse the available tools and inspect general-purpose capabilities "
        "before deciding."
    )
    fallback = await dispatch(registry, host, result["data"]["next"])
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
    result = await dispatch(
        registry, host, {"action": "search", "query": "scene material", "kind": "tool"}
    )
    assert result["data"]["matches"] == "1-10 of 14"
    assert targets(result)[0].startswith("tool:scene_12:")
    following = await dispatch(registry, host, result["data"]["next"])
    assert following["data"]["matches"] == "11-14 of 14"
    names = [target.split(":")[1] for target in targets(result) + targets(following)]
    assert len(set(names)) == 14
    assert "next" not in following["data"]


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", [80, "80", 80.0])
async def test_search_honors_limit_up_to_its_maximum_and_says_when_it_applied_less(
    context_service, host, requested
):
    service, registry, runner, calls = context_service
    # Long names and descriptions: a page of the largest limit still shows every entry.
    runner.catalog["tools"] = [
        {
            "name": f"tool_with_a_long_name_{index:02d}",
            "description": "scene " * 60,
            "inputSchema": {"type": "object"},
        }
        for index in range(60)
    ]
    service._publish(runner, runner.catalog)

    within = await dispatch(registry, host, {"action": "search", "limit": 30})
    capped = await dispatch(registry, host, {"action": "search", "limit": requested})
    rest = await dispatch(registry, host, capped["data"]["next"])

    assert within["data"]["matches"] == "1-30 of 60"
    assert "limit" not in within["data"]
    assert within["data"]["next"] == {"action": "search", "limit": 30, "offset": 30}
    assert len(targets(capped)) == 50
    assert model_text(capped).startswith(
        "connection: example\n"
        "available: 60 tools\n"
        "matches: 1-50 of 60\n"
        "limit: 80 was reduced to 50, the maximum for search\n"
        'next: {"action":"search","limit":50,"offset":50}\n'
    )
    assert rest["data"]["matches"] == "51-60 of 60"
    assert {"limit", "next"}.isdisjoint(rest["data"])


@pytest.mark.asyncio
async def test_search_without_kind_covers_application_items_and_points_to_operations(
    context_service, host
):
    service, registry, runner, calls = context_service
    runner.catalog["tools"] = [
        {"name": f"tool_{index:02d}", "description": "scene", "inputSchema": {"type": "object"}}
        for index in range(12)
    ]
    runner.catalog["resources"] = [{"uri": "test://scene", "name": "scene"}]
    runner.catalog["resource_templates"] = [{"uriTemplate": "test://items/{name}", "name": "item"}]
    runner.catalog["prompts"] = [{"name": "workflow", "description": "scene workflow"}]
    service._publish(runner, runner.catalog)

    browse = await dispatch(registry, host, {"action": "search"})
    following = await dispatch(registry, host, browse["data"]["next"])
    operations = await dispatch(registry, host, {"action": "search", "kind": "operation"})
    connection = await dispatch(registry, host, {"action": "search", "kind": "connection"})
    subscription = await dispatch(registry, host, {"action": "search", "query": "subscription"})

    # The first page also summarizes the prompt that the second page lists.
    listed = set(targets(browse) + targets(following))
    assert browse["data"]["matches"] == "1-10 of 15"
    assert following["data"]["matches"] == "11-15 of 15"
    assert "next" not in following["data"]
    assert len(listed) == 15
    assert not [target for target in listed if target.startswith(("operation:", "connection"))]
    assert browse["data"]["operations"] == (
        "resource subscriptions, events, logging, tasks and more: "
        '{"action":"search","kind":"operation"}'
    )
    assert "operations" not in following["data"]
    assert operations["data"]["matches"] == "1-10 of 13"
    assert all(target.startswith("operation:") for target in targets(operations))
    assert connection["data"]["matches"] == "1-1 of 1"
    assert subscription["data"]["matches"] == "none"
    assert subscription["data"]["operations"] == (
        '1 matches these words: {"action":"search","kind":"operation","query":"subscription"}'
    )
    assert calls == []


@pytest.mark.asyncio
async def test_long_server_guidance_is_explicitly_incomplete_and_readable(context_service, host):
    service, registry, runner, calls = context_service
    runner.catalog["instructions"] = "test-owned-guidance " * 500
    result = await dispatch(registry, host, {"action": "search"})
    assert "remaining server guidance" in result["data"]["guidance"]
    text = result["data"]["content"].split("(external, from the MCP server):\n", 1)[1]
    next_read = result["data"]["guidance_read"]
    while next_read:
        part = await dispatch(registry, host, next_read)
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
    result = await dispatch(registry, host, {"action": "search", "kind": "tool"})
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
    result = await dispatch(registry, host, {"action": "search"})
    assert result["ok"] is False
    assert result["data"] is None


@pytest.mark.asyncio
async def test_denied_tools_stay_out_of_search_and_are_refused_by_name(context_service, host):
    service, registry, runner, calls = context_service
    host.resolve_agent(None, "alice").tool_access = ToolAccess(
        granted=("mcp_example",), denied=[remote_tool_name("example", "inspect")]
    )
    result = await dispatch(registry, host, {"action": "search", "query": "missing"})
    assert result["data"]["available"] == "no tools, resources or prompts"
    fallback = await dispatch(registry, host, result["data"]["next"])
    assert fallback["data"]["matches"] == "none"
    assert targets(fallback) == []
    denied = await dispatch(
        registry,
        host,
        {"action": "call", "target": "inspect", "arguments": {"value": "sentinel"}},
    )
    # The connection description lists the name, so the refusal gives its real cause.
    assert denied["error"] == {
        "code": "mcp_access_denied",
        "message": (
            "This Agent's Tool settings do not allow this MCP tool, so nothing was run. "
            "Tell the user if it is needed."
        ),
    }
    described = await dispatch(registry, host, {"action": "describe", "target": "tool:inspect"})
    assert described["error"]["code"] == "mcp_access_denied"
    fingerprinted = await dispatch(
        registry,
        host,
        {"action": "call", "target": "tool:inspect:" + "0" * 24, "arguments": {"value": "x"}},
    )
    assert fingerprinted["error"]["code"] == "mcp_access_denied"
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
    target = await tool_target(registry, host)
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
        @override
        def tool_access(self):
            return ToolAccess(mode="selected", allowed=("mcp_example",), granted=("mcp_example",))

    agent = McpAgent(id="alice", model="openai/gpt-5.2")
    runtime = StubRuntime(data_dir=host.data_dir, agent=agent, adapter=adapter, tools=registry)
    try:
        runtime.chat_sessions.create("alice", session_id="session-one")
        run = await build_chat_loop(runtime).start_run("alice", "inspect", session_id="session-one")
        await run.wait()
    finally:
        runtime.chat_sessions.close()

    assert len(calls) == 1
    assert len(adapter.requests) == 4
    for previous, current in zip(adapter.requests, adapter.requests[1:], strict=False):
        assert current["kwargs"]["tools"] == previous["kwargs"]["tools"]
        assert current["messages"][: len(previous["messages"])] == previous["messages"]


@pytest.mark.asyncio
async def test_fixed_entry_point_uses_real_tools_resources_and_prompts(host, server, monkeypatch):
    async def in_memory(runner, stack):
        return server

    monkeypatch.setattr(ConnectionRunner, "_transport", in_memory)
    service, registry = await start_service(host)
    try:
        await service.manage(
            "save", {"connection": {"id": "example", "transport": "stdio", "command": "unused"}}
        )
        async with asyncio.timeout(10):
            first = await dispatch(registry, host, {"action": "search"})
        catalog = service.runners["example"].catalog
        assert catalog["instructions"] == "test-owned-server-instructions"
        assert [tool["name"] for tool in catalog["tools"]] == ["echo"]
        assert len(catalog["resource_templates"]) == 1
        assert "test-owned-server-instructions" in first["data"]["content"]
        before = _definitions(registry)
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
            target = targets(await dispatch(registry, host, {"action": "search", "kind": kind}))[0]
            detail = await dispatch(registry, host, {"action": "describe", "target": target})
            result = await dispatch(
                registry, host, {"action": "call", "target": target, "arguments": inputs}
            )
            assert detail["ok"] and result["ok"]
            assert expected[kind] in result["data"]["content"]
            # The SDK's structured copy of the returned value repeats the text.
            assert "structuredContent" not in result["data"]
        assert _definitions(registry) == before
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_read_of_an_unknown_result_names_what_to_use(context_service, host):
    service, registry, runner, calls = context_service

    result = await dispatch(registry, host, {"action": "read", "result_id": "../invalid"})

    assert result["error"]["code"] == "invalid_arguments"
    assert "Use a result_id that this connection returned here" in result["error"]["message"]
    assert calls == []


@pytest.mark.asyncio
async def test_cli_explore_and_invoke_return_the_complete_payload_inline(context_service, host):
    service, registry, runner, calls = context_service
    runner.catalog["instructions"] = "test-owned-guidance " * 500

    async def finished(job):
        await service.jobs.wait(job["job_id"])
        return (await service.manage("job", {"job_id": job["job_id"]}))["result"]

    search = await finished(
        await service.manage("explore", {"id": "example", "agent": "alice", "action": "search"})
    )
    invoke = await finished(
        await service.manage(
            "invoke",
            {
                "id": "example",
                "agent": "alice",
                "operation": "tools/call",
                "arguments": {"name": "inspect", "arguments": {"value": "x" * 7000}},
            },
        )
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
