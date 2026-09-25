"""Mcp: connection Tool call shapes, target resolution, and request failures."""

from __future__ import annotations

import json

import pytest

from core.tools.contracts import ToolContractError
from resources.extensions.mcp.client import InvocationNotSentError
from tests.resources.extensions.mcp_helpers import context, model_text, targets
from tests.resources.extensions.mcp_helpers import context_service as context_service
from tests.resources.extensions.mcp_helpers import host as host

TARGET = "<target>"


def _with_target(value, target):
    if value == TARGET:
        return target
    if isinstance(value, dict):
        return {key: _with_target(item, target) for key, item in value.items()}
    return value


def _tool_target(service, runner, host) -> str:
    return str(service._entries(runner, service._allowed(context(host)))[-1]["target"])


async def _dispatch(registry, host, arguments):
    return await registry.dispatch(context(host), arguments, allowed_tools=["mcp_example"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        # Proxy-tool spellings with the call inferred from target and arguments.
        {"tool": "inspect", "args": {"value": "sentinel"}},
        {"action": "run", "name": "inspect", "input": {"value": "sentinel"}},
        {"action": "CALL", "target": "tool:inspect", "arguments": '{"value": "sentinel"}'},
        {"action": "call", "target": "Inspect", "parameters": {"value": "sentinel"}},
        {"call": {"target": TARGET, "arguments": {"value": "sentinel"}}},
        {"Action": "call", "Target": TARGET, "Arguments": {"value": "sentinel"}},
        # Fields that request nothing for a call are dropped.
        {"action": "call", "target": TARGET, "arguments": {"value": "sentinel"}, "query": ""},
        {"action": "call", "target": TARGET, "arguments": {"value": "sentinel"}, "offset": 0},
    ],
)
async def test_call_dialects_reach_the_exact_remote_tool(context_service, host, arguments):
    service, registry, runner, calls = context_service
    arguments = _with_target(arguments, _tool_target(service, runner, host))

    result = await _dispatch(registry, host, arguments)

    assert result["ok"], result
    assert result["data"]["content"] == "sentinel"
    assert calls == [("tools/call", {"name": "inspect", "arguments": {"value": "sentinel"}})]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"action": "browse"},
        {"q": "inspection"},
        {"action": "find", "search": "inspection"},
        {"action": "search", "kind": "TOOLS"},
        {"action": "search", "type": "tools", "query": "inspection"},
    ],
)
async def test_search_dialects_list_the_tool(context_service, host, arguments):
    service, registry, runner, calls = context_service

    result = await _dispatch(registry, host, arguments)

    assert result["ok"], result
    assert targets(result)[0] == _tool_target(service, runner, host)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {"pointer": "content/0/text"},
        {"path": "#/content/0/text"},
        {"action": "page", "json_pointer": "/content/0/text"},
    ],
)
async def test_read_dialects_select_the_saved_text(context_service, host, extra):
    service, registry, runner, calls = context_service
    receipt, _ = await service.content.present(
        {"content": [{"type": "text", "text": "sentinel " * 1000}]}, context(host), "example"
    )

    result = await _dispatch(registry, host, {"result_id": receipt["result_id"], **extra})

    assert result["ok"], result
    assert result["data"]["pointer"] == "/content/0/text"
    assert result["data"]["content"].startswith("sentinel sentinel")


@pytest.mark.asyncio
async def test_read_field_list_may_be_comma_separated(context_service, host):
    service, registry, runner, calls = context_service
    receipt, _ = await service.content.present(
        {"rows": [{"id": index, "name": "x", "private": "y" * 400} for index in range(20)]},
        context(host),
        "example",
    )

    result = await _dispatch(
        registry,
        host,
        {
            "action": "read",
            "result_id": receipt["result_id"],
            "pointer": "/rows",
            "fields": "id, name",
        },
    )

    assert result["data"]["entries"][0]["value"] == {"id": 0, "name": "x"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments,expected",
    [
        (
            {"target": TARGET},
            'Describe it with {"action":"describe","target":"<target>"}, or call it with '
            '{"action":"call","target":"<target>","arguments":{}} and its arguments.',
        ),
        (
            {"action": "describe", "target": TARGET, "arguments": {"value": "x"}},
            'Describe the target with {"action":"describe","target":"<target>"}, or call it '
            'with these arguments: {"action":"call","target":"<target>","arguments":'
            '{"value":"x"}}.',
        ),
        (
            {"action": "call", "target": TARGET, "arguments": {"value": "x"}, "limit": 3},
            "limit does not apply to call, which takes target, arguments. Send "
            '{"action":"call","target":"<target>","arguments":{"value":"x"}}.',
        ),
        (
            {"action": "call", "target": TARGET, "value": "x"},
            "value is not a field of mcp_example. The target's own arguments go inside "
            'arguments: {"action":"call","target":"<target>","arguments":{"value":"x"}}.',
        ),
        (
            {"action": "search", "target": TARGET},
            'Describe that target with {"action":"describe","target":"<target>"}',
        ),
        (
            {"action": "explode"},
            'action must be one of search, describe, call, read. Start with {"action":"search"}',
        ),
        ({"action": "read"}, "read was not run: result_id is missing"),
        ({"action": "describe"}, "target is missing. Use a target from search"),
    ],
)
async def test_unclear_calls_refuse_with_the_corrected_call(
    context_service, host, arguments, expected
):
    service, registry, runner, calls = context_service
    target = _tool_target(service, runner, host)

    with pytest.raises(ToolContractError) as refusal:
        await _dispatch(registry, host, _with_target(arguments, target))

    assert expected.replace(TARGET, target) in str(refusal.value)
    assert calls == []


@pytest.mark.asyncio
async def test_a_long_corrected_call_is_described_instead_of_repeated(context_service, host):
    service, registry, runner, calls = context_service
    target = _tool_target(service, runner, host)
    code = "x = 1\n" * 1000

    with pytest.raises(ToolContractError) as refusal:
        await _dispatch(
            registry,
            host,
            {"action": "call", "target": target, "arguments": {"value": code}, "limit": 3},
        )

    assert str(refusal.value).endswith("Send the same call without limit.")
    assert code not in str(refusal.value)
    assert calls == []


@pytest.mark.asyncio
async def test_unparsable_argument_text_is_refused_without_echo(context_service, host):
    service, registry, runner, calls = context_service
    target = _tool_target(service, runner, host)

    with pytest.raises(ToolContractError) as refusal:
        await _dispatch(
            registry,
            host,
            {"action": "call", "target": target, "arguments": '{"value": "test-owned-secret"'},
        )

    assert "arguments is text that is not a JSON object" in str(refusal.value)
    assert "test-owned-secret" not in str(refusal.value)
    assert calls == []


@pytest.mark.asyncio
async def test_changed_definition_is_not_substituted_for_a_call(context_service, host):
    service, registry, runner, calls = context_service
    old = _tool_target(service, runner, host)
    runner.catalog["tools"][0]["description"] = "test-owned-new-description"
    service._publish(runner, runner.catalog)
    current = _tool_target(service, runner, host)

    call = await _dispatch(
        registry, host, {"action": "call", "target": old, "arguments": {"value": "x"}}
    )
    describe = await _dispatch(registry, host, {"action": "describe", "target": old})

    assert call["error"]["code"] == "mcp_target_changed"
    assert current in call["error"]["message"]
    assert (
        json.dumps({"action": "describe", "target": current}, separators=(",", ":"))
        in (call["error"]["message"])
    )
    assert describe["data"]["target"] == current
    assert (
        describe["data"]["note"] == f"{old} named an earlier definition; this is the current one."
    )
    assert calls == []


@pytest.mark.asyncio
async def test_a_name_of_several_items_is_never_chosen(context_service, host):
    service, registry, runner, calls = context_service
    runner.catalog["prompts"] = [{"name": "inspect", "description": "test-owned-prompt"}]
    service._publish(runner, runner.catalog)

    ambiguous = await _dispatch(
        registry, host, {"action": "call", "target": "inspect", "arguments": {"value": "x"}}
    )
    kinded = await _dispatch(
        registry, host, {"action": "call", "target": "tool:inspect", "arguments": {"value": "x"}}
    )

    assert ambiguous["error"]["code"] == "mcp_unknown_target"
    assert "tool:inspect:" in ambiguous["error"]["message"]
    assert "prompt:inspect:" in ambiguous["error"]["message"]
    assert kinded["ok"]
    assert calls == [("tools/call", {"name": "inspect", "arguments": {"value": "x"}})]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target,expected",
    [
        (
            "inspekt",
            'The closest is <target>; if you mean it, repeat the call with "target":"<target>".',
        ),
        ("render_scene", 'Find it with {"action":"search","query":"render scene"}.'),
    ],
)
async def test_unknown_target_names_candidates_without_calling(
    context_service, host, target, expected
):
    service, registry, runner, calls = context_service
    current = _tool_target(service, runner, host)

    result = await _dispatch(
        registry, host, {"action": "call", "target": target, "arguments": {"value": "x"}}
    )

    assert result["error"]["code"] == "mcp_unknown_target"
    assert expected.replace(TARGET, current) in result["error"]["message"]
    assert calls == []


@pytest.mark.asyncio
async def test_resource_can_be_named_by_its_uri(context_service, host):
    service, registry, runner, calls = context_service
    runner.catalog["resources"] = [{"uri": "test://scene", "name": "scene"}]

    result = await _dispatch(registry, host, {"action": "call", "target": "resource:test://scene"})

    assert result["ok"], result
    assert calls == [("resources/read", {"uri": "test://scene"})]


@pytest.mark.asyncio
async def test_invalid_target_arguments_name_the_fields_and_the_describe_call(
    context_service, host
):
    service, registry, runner, calls = context_service
    target = _tool_target(service, runner, host)

    result = await _dispatch(
        registry,
        host,
        {"action": "call", "target": target, "arguments": {"value": "x", "vlaue": "y"}},
    )

    assert model_text(result) == (
        "Error (mcp_invalid_arguments): tool inspect was not called: /arguments has fields "
        "the target does not take: vlaue. It takes value (string, required). Correct the "
        f'arguments and call again; {{"action":"describe","target":"{target}"}} shows the '
        "full schema."
    )
    assert calls == []


@pytest.mark.asyncio
async def test_a_call_that_was_never_sent_says_so_and_invites_one_retry(
    context_service, host, monkeypatch
):
    service, registry, runner, calls = context_service
    target = _tool_target(service, runner, host)

    async def unavailable(*args):
        calls.append(args)
        raise InvocationNotSentError("MCP connection did not become ready")

    monkeypatch.setattr(runner, "invoke", unavailable)
    result = await _dispatch(
        registry, host, {"action": "call", "target": target, "arguments": {"value": "x"}}
    )

    assert result["error"] == {
        "code": "mcp_request_failed",
        "message": (
            "The MCP connection example is not available (MCP connection did not become "
            "ready), so nothing was run. The next call reconnects: try once more, and if it "
            "fails again, tell the user that the MCP server example cannot be reached."
        ),
        "retryable": True,
    }


@pytest.mark.asyncio
async def test_a_refused_queued_call_reports_the_access_rule(context_service, host, monkeypatch):
    service, registry, runner, calls = context_service
    target = _tool_target(service, runner, host)

    async def refused(*args):
        raise InvocationNotSentError("denied", denied=True)

    monkeypatch.setattr(runner, "invoke", refused)
    result = await _dispatch(
        registry, host, {"action": "call", "target": target, "arguments": {"value": "x"}}
    )

    assert result["error"]["code"] == "mcp_access_denied"
    assert "nothing was run" in result["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,arguments,retryable,expected",
    [
        ("ping", {}, True, "This read returned no result and changed nothing"),
        (
            "logging/setLevel",
            {"level": "debug"},
            None,
            "may already have changed the application",
        ),
    ],
)
async def test_unconfirmed_calls_say_whether_repeating_is_safe(
    context_service, host, monkeypatch, operation, arguments, retryable, expected
):
    service, registry, runner, calls = context_service

    async def timeout(*args):
        raise ValueError("test-owned-timeout")

    monkeypatch.setattr(runner, "invoke", timeout)
    result = await _dispatch(
        registry,
        host,
        {"action": "call", "target": service._operation_target(operation), "arguments": arguments},
    )

    assert result["error"]["code"] == "mcp_call_unconfirmed"
    assert result["error"]["message"].startswith("test-owned-timeout. ")
    assert expected in result["error"]["message"]
    assert result["error"].get("retryable") is retryable


@pytest.mark.asyncio
async def test_unreachable_connection_on_discovery_invites_one_retry(
    context_service, host, monkeypatch
):
    service, registry, runner, calls = context_service
    runner.state = "failed"

    async def unreachable(*args):
        raise InvocationNotSentError("test-owned-connection-error")

    monkeypatch.setattr(runner, "invoke", unreachable)
    result = await _dispatch(registry, host, {"action": "search"})

    assert result["error"]["code"] == "mcp_request_failed"
    assert result["error"]["retryable"] is True
    assert "(test-owned-connection-error), so nothing was run" in result["error"]["message"]
