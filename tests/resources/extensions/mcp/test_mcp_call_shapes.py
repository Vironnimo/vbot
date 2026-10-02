"""MCP: connection Tool call shapes, target resolution, and request failures."""

from __future__ import annotations

import json

import pytest

from core.tools.contracts import ToolContractError
from resources.extensions.mcp._tasks import TaskEndedError
from resources.extensions.mcp.client import InvocationNotSentError
from tests.resources.extensions.mcp.mcp_test_support import (
    context,
    dispatch,
    model_text,
    operation_target,
    targets,
    tool_target,
)

TARGET = "<target>"
# The target from search without its kind, such as inspect:<fingerprint>.
UNKINDED = "<target without kind>"


def _with_target(value, target):
    if value == TARGET:
        return target
    if value == UNKINDED:
        return target.split(":", 1)[1]
    if isinstance(value, dict):
        return {key: _with_target(item, target) for key, item in value.items()}
    return value


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
        {"action": "call", "target": UNKINDED, "arguments": {"value": "sentinel"}},
        # Fields that request nothing for a call are dropped.
        {"action": "call", "target": TARGET, "arguments": {"value": "sentinel"}, "query": ""},
        {"action": "call", "target": TARGET, "arguments": {"value": "sentinel"}, "offset": 0},
    ],
)
async def test_call_dialects_reach_the_exact_remote_tool(context_service, host, arguments):
    service, registry, runner, calls = context_service
    arguments = _with_target(arguments, await tool_target(registry, host))

    result = await dispatch(registry, host, arguments)

    assert result["ok"], result
    assert result["data"]["content"] == "sentinel"
    assert "note" not in result["data"]
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

    result = await dispatch(registry, host, arguments)

    assert result["ok"], result
    assert targets(result)[0] == await tool_target(registry, host)
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

    result = await dispatch(registry, host, {"result_id": receipt["result_id"], **extra})

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

    result = await dispatch(
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
        ({"action": "call"}, "call was not run: target is missing"),
        (
            {"action": "search", "unknown": True},
            "unknown is not a field of mcp_example. search takes query, kind, offset, limit",
        ),
        ({"action": "search", "limit": -1}, '"limit" must be at least 1; received -1'),
    ],
)
async def test_unclear_calls_refuse_with_the_corrected_call(
    context_service, host, arguments, expected
):
    service, registry, runner, calls = context_service
    target = await tool_target(registry, host)

    with pytest.raises(ToolContractError) as refusal:
        await dispatch(registry, host, _with_target(arguments, target))

    assert expected.replace(TARGET, target) in str(refusal.value)
    assert calls == []


@pytest.mark.asyncio
async def test_a_long_corrected_call_is_described_instead_of_repeated(context_service, host):
    service, registry, runner, calls = context_service
    target = await tool_target(registry, host)
    code = "x = 1\n" * 1000

    with pytest.raises(ToolContractError) as refusal:
        await dispatch(
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
    target = await tool_target(registry, host)

    with pytest.raises(ToolContractError) as refusal:
        await dispatch(
            registry,
            host,
            {"action": "call", "target": target, "arguments": '{"value": "test-owned-secret"'},
        )

    assert "arguments is text that is not a JSON object" in str(refusal.value)
    assert "test-owned-secret" not in str(refusal.value)
    assert calls == []


# A fingerprint no definition of the item has, such as an invented one.
UNMATCHED = "0" * 24


@pytest.mark.asyncio
@pytest.mark.parametrize("earlier", [True, False], ids=["earlier_definition", "never_shown"])
async def test_a_target_whose_fingerprint_does_not_match_is_never_called(
    context_service, host, earlier
):
    service, registry, runner, calls = context_service
    if earlier:
        sent = await tool_target(registry, host)
        # Metadata a server varies between listings keeps the target.
        runner.catalog["tools"][0]["_meta"] = {"test-owned": "changed"}
        runner.catalog["tools"][0]["icons"] = [{"src": "https://example.test/icon.png"}]
        service._publish(runner, runner.catalog)
        assert await tool_target(registry, host) == sent
        runner.catalog["tools"][0]["description"] = "test-owned-new-description"
        service._publish(runner, runner.catalog)
    else:
        sent = f"tool:inspect:{UNMATCHED}"
    current = await tool_target(registry, host)
    assert current != sent

    call = await dispatch(
        registry, host, {"action": "call", "target": sent, "arguments": {"value": "x"}}
    )
    describe = await dispatch(registry, host, {"action": "describe", "target": sent})

    describe_current = json.dumps({"action": "describe", "target": current}, separators=(",", ":"))
    assert model_text(call) == (
        "Error (mcp_target_mismatch): tool inspect was not called: the part after its name in "
        f"{sent} does not match its current definition. Its current target is {current}; check "
        f"its arguments with {describe_current}, then call the current target."
    )
    assert calls == []
    assert describe["data"]["target"] == current
    assert describe["data"]["note"] == (
        f"Used the current target {current}: the part after the name in {sent} does not match "
        "the current definition."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,name,arguments,sent_call",
    [
        ("resource", "scene", {}, ("resources/read", {"uri": "test://scene"})),
        (
            "template",
            "item",
            {"uri": "test://items/a"},
            ("resources/read", {"uri": "test://items/a"}),
        ),
        (
            "prompt",
            "workflow",
            {"subject": "a"},
            ("prompts/get", {"name": "workflow", "arguments": {"subject": "a"}}),
        ),
        ("operation", "ping", {}, ("ping", {})),
        ("operation", "logging/setLevel", {"level": "debug"}, None),
    ],
)
async def test_only_reads_run_with_a_fingerprint_that_does_not_match(
    context_service, host, kind, name, arguments, sent_call
):
    service, registry, runner, calls = context_service
    runner.catalog["resources"] = [{"uri": "test://scene", "name": "scene"}]
    runner.catalog["resource_templates"] = [{"uriTemplate": "test://items/{name}", "name": "item"}]
    runner.catalog["prompts"] = [
        {"name": "workflow", "arguments": [{"name": "subject", "required": True}]}
    ]
    listed = targets(
        await dispatch(registry, host, {"action": "search", "kind": kind, "query": name})
    )
    current = next(target for target in listed if target.startswith(f"{kind}:{name}:"))
    sent = f"{kind}:{name}:{UNMATCHED}"

    result = await dispatch(
        registry, host, {"action": "call", "target": sent, "arguments": arguments}
    )

    if sent_call is None:
        assert result["error"]["code"] == "mcp_target_mismatch"
        assert f"Its current target is {current};" in result["error"]["message"]
        assert calls == []
    else:
        assert result["ok"], result
        assert result["data"]["note"] == (
            f"Used the current target {current}: the part after the name in {sent} does not "
            "match the current definition."
        )
        assert calls == [sent_call]


@pytest.mark.asyncio
async def test_the_target_note_leaves_a_remote_note_field_intact(
    context_service, host, monkeypatch
):
    service, registry, runner, calls = context_service
    runner.catalog["resources"] = [{"uri": "test://scene", "name": "scene"}]

    async def answer(operation, arguments, invocation_context=None):
        return {"content": [{"type": "text", "text": "done"}], "note": {"remote": True}}

    monkeypatch.setattr(runner, "invoke", answer)
    result = await dispatch(
        registry, host, {"action": "call", "target": f"resource:scene:{UNMATCHED}"}
    )

    assert result["data"]["note"] == {"remote": True}
    assert result["data"]["target_note"].startswith("Used the current target resource:scene:")


@pytest.mark.asyncio
async def test_a_name_of_several_items_is_never_chosen(context_service, host):
    service, registry, runner, calls = context_service
    runner.catalog["prompts"] = [{"name": "inspect", "description": "test-owned-prompt"}]
    service._publish(runner, runner.catalog)

    ambiguous = await dispatch(
        registry, host, {"action": "call", "target": "inspect", "arguments": {"value": "x"}}
    )
    kinded = await dispatch(
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
    current = await tool_target(registry, host)

    result = await dispatch(
        registry, host, {"action": "call", "target": target, "arguments": {"value": "x"}}
    )

    assert result["error"]["code"] == "mcp_unknown_target"
    assert expected.replace(TARGET, current) in result["error"]["message"]
    assert calls == []


@pytest.mark.asyncio
async def test_resource_can_be_named_by_its_uri(context_service, host):
    service, registry, runner, calls = context_service
    runner.catalog["resources"] = [{"uri": "test://scene", "name": "scene"}]

    result = await dispatch(registry, host, {"action": "call", "target": "resource:test://scene"})

    assert result["ok"], result
    assert calls == [("resources/read", {"uri": "test://scene"})]


@pytest.mark.asyncio
async def test_invalid_target_arguments_name_the_fields_and_the_describe_call(
    context_service, host
):
    service, registry, runner, calls = context_service
    target = await tool_target(registry, host)

    result = await dispatch(
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
    target = await tool_target(registry, host)

    async def unavailable(*args):
        calls.append(args)
        raise InvocationNotSentError("MCP connection did not become ready")

    monkeypatch.setattr(runner, "invoke", unavailable)
    result = await dispatch(
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
    target = await tool_target(registry, host)

    async def refused(*args):
        raise InvocationNotSentError("denied", denied=True)

    monkeypatch.setattr(runner, "invoke", refused)
    result = await dispatch(
        registry, host, {"action": "call", "target": target, "arguments": {"value": "x"}}
    )

    assert result["error"]["code"] == "mcp_access_denied"
    assert "nothing was run" in result["error"]["message"]


UNKNOWN_OUTCOME = (
    "No result came back, so whether the call changed the application is unknown. Before you "
    "repeat a call that changes something, check the application's current state. A call "
    "that only reads is safe to repeat."
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,arguments,error,expected",
    [
        (
            "ping",
            {},
            ValueError,
            "mcp_call_unconfirmed): test-owned-timeout. This read returned no result and "
            "changed nothing; try it once more, and tell the user if it keeps failing."
            "\nretryable: true",
        ),
        (
            "logging/setLevel",
            {"level": "debug"},
            ValueError,
            f"mcp_call_unconfirmed): test-owned-timeout. {UNKNOWN_OUTCOME}",
        ),
        (
            "tools/call",
            {"value": "sentinel"},
            ValueError,
            f"mcp_call_unconfirmed): test-owned-timeout. {UNKNOWN_OUTCOME}",
        ),
        (
            "tools/call",
            {"value": "sentinel"},
            TaskEndedError,
            "mcp_task_ended): test-owned-timeout. The MCP server ran this call in the "
            "background, and it ended without a result. Work it did before it ended may "
            "remain: before you repeat a call that changes something, check the application's "
            "current state. A call that only reads is safe to repeat.",
        ),
    ],
    ids=["read", "operation", "tool", "task-ended"],
)
async def test_calls_without_a_result_are_not_repeated_and_say_whether_repeating_is_safe(
    context_service, host, monkeypatch, operation, arguments, error, expected
):
    service, registry, runner, calls = context_service
    target = (
        await tool_target(registry, host)
        if operation == "tools/call"
        else await operation_target(registry, host, operation)
    )

    async def timeout(*args):
        calls.append(args)
        raise error("test-owned-timeout")

    monkeypatch.setattr(runner, "invoke", timeout)
    result = await dispatch(
        registry, host, {"action": "call", "target": target, "arguments": arguments}
    )

    assert model_text(result) == f"Error ({expected}"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_unreachable_connection_on_discovery_invites_one_retry(
    context_service, host, monkeypatch
):
    service, registry, runner, calls = context_service
    runner.state = "failed"

    async def unreachable(*args):
        raise InvocationNotSentError("test-owned-connection-error")

    monkeypatch.setattr(runner, "invoke", unreachable)
    result = await dispatch(registry, host, {"action": "search"})

    assert result["error"]["code"] == "mcp_request_failed"
    assert result["error"]["retryable"] is True
    assert "(test-owned-connection-error), so nothing was run" in result["error"]["message"]
