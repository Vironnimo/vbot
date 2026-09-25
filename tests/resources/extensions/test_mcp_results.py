"""Mcp: results behavior."""

from __future__ import annotations

import json

import pytest

from core.tools.availability import ToolAccess
from core.tools.tools import ToolDefinitionProfileContext
from resources.extensions.mcp.content import (
    READ_TOO_LARGE,
    RESULT_DENIED,
    RESULT_MISSING,
    RESULT_VIEW_CHARACTERS,
    ContentStore,
)
from resources.extensions.mcp.extension import MCP_MESSAGES, remote_tool_name
from tests.resources.extensions.mcp_helpers import (
    context,
    payloads,
)
from tests.resources.extensions.mcp_helpers import (
    context_service as context_service,
)
from tests.resources.extensions.mcp_helpers import (
    host as host,
)


@pytest.mark.asyncio
async def test_large_result_is_kept_with_the_tool_result_and_readable_in_chunks(host):
    store = ContentStore(host)
    payload = {
        "content": [{"type": "text", "text": "test-owned-text-ä" * 2000}],
        "_meta": {"keep": True},
    }
    receipt, _ = await store.present(payload, context(host), "example")
    restored = ContentStore(host)
    document = await restored.load_result(receipt["result_id"], context(host), "example")
    arguments = {
        "action": "read",
        "result_id": receipt["result_id"],
        "pointer": "/content/0/text",
        "limit": 997,
    }
    pieces = []
    while True:
        page = restored.read_result(document, arguments)
        pieces.append(page["value"])
        if "next" not in page:
            break
        arguments = page["next"]

    # The receipt names no file: read is the only way to the saved payload.
    assert set(receipt) == {"result_id", "complete", "preview", "read"}
    assert receipt["read"] == {"action": "read", "result_id": receipt["result_id"]}
    assert not receipt["complete"]
    assert len(json.dumps(receipt)) < RESULT_VIEW_CHARACTERS
    assert "".join(pieces) == payload["content"][0]["text"]
    assert document["payload"]["_meta"] == payload["_meta"]
    assert list(payloads(host).rows) == [receipt["result_id"]]
    assert not (host.data_dir / "mcp").exists()


@pytest.mark.asyncio
async def test_saved_result_is_readable_only_where_its_tool_result_is_visible(host):
    store = ContentStore(host)
    receipt, _ = await store.present({"sentinel": "x" * 7000}, context(host), "example")
    identifier = receipt["result_id"]

    for elsewhere in (
        context(host, session="other"),
        context(host, "bob"),
        context(host, project="other"),
        context(host, session=None),
    ):
        with pytest.raises(ValueError, match=RESULT_MISSING):
            await store.load_result(identifier, elsewhere, "example")
    with pytest.raises(ValueError, match=RESULT_MISSING):
        await store.load_result("../outside", context(host), "example")
    with pytest.raises(ValueError, match=RESULT_DENIED):
        await store.load_result(identifier, context(host), "other")


@pytest.mark.asyncio
async def test_complete_results_and_calls_outside_a_session_keep_no_payload(host):
    store = ContentStore(host)
    small = {"sentinel": True}
    large = {"sentinel": "x" * 7000}

    inline, _ = await store.present(small, context(host), "example")
    outside, _ = await store.present(
        large, context(host, session=None), "example", preview={"page": 1}
    )

    assert inline == {"complete": True, "value": small}
    # Outside a Session nothing could read a saved result, so all of it is inline.
    assert outside == {"complete": True, "value": large}
    assert payloads(host).rows == {}


@pytest.mark.asyncio
async def test_result_reader_filters_rows_and_paginates_without_losing_values(host):
    store = ContentStore(host)
    receipt, _ = await store.present(
        {"rows": [{"id": index, "private": "unused" * 40} for index in range(31)]},
        context(host),
        "example",
    )
    document = await store.load_result(receipt["result_id"], context(host), "example")
    first = store.read_result(
        document,
        {"action": "read", "result_id": receipt["result_id"], "pointer": "/rows", "fields": ["id"]},
    )
    second = store.read_result(document, first["next"])

    assert [entry["value"] for entry in first["entries"] + second["entries"]] == [
        {"id": index} for index in range(31)
    ]
    assert "next" not in second


@pytest.mark.asyncio
async def test_large_projected_row_expansion_keeps_requested_fields(host):
    store = ContentStore(host)
    receipt, _ = await store.present(
        {"rows": [{"selected": "sentinel " * 1000, "unrequested": "do not include"}]},
        context(host),
        "example",
    )
    document = await store.load_result(receipt["result_id"], context(host), "example")
    page = store.read_result(
        document, {**receipt["read"], "pointer": "/rows", "fields": ["selected"]}
    )
    expansion = page["entries"][0]["read"]
    assert expansion["fields"] == ["selected"]
    expanded = store.read_result(document, expansion)
    assert [entry["pointer"] for entry in expanded["entries"]] == ["/rows/0/selected"]
    assert "unrequested" not in json.dumps(expanded)


@pytest.mark.asyncio
async def test_revoke_prevents_reading_a_saved_remote_result(context_service, host):
    service, registry, runner, calls = context_service
    receipt, _ = await service.content.present(
        {"sentinel": "x" * 7000}, context(host), "example", source="inspect"
    )
    host.resolve_agent(None, "alice").tool_access = ToolAccess(
        granted=("mcp_example",), denied=(remote_tool_name("example", "inspect"),)
    )

    result = await registry.dispatch(context(host), receipt["read"], allowed_tools=["mcp_example"])

    assert not result["ok"]
    assert result["error"]["message"] == MCP_MESSAGES["access_denied"]


@pytest.mark.asyncio
async def test_connection_disconnect_keeps_the_fixed_model_definition(context_service):
    service, registry, runner, calls = context_service
    profile = ToolDefinitionProfileContext(agent_id="alice")
    before = registry.provider_definitions(profile_context=profile, allowed_tools=["mcp_example"])

    await service.manage("disconnect", {"id": "example"})

    assert (
        registry.provider_definitions(profile_context=profile, allowed_tools=["mcp_example"])
        == before
    )


@pytest.mark.asyncio
async def test_large_error_keeps_full_payload_and_bounded_receipt(context_service, host):
    service, registry, runner, calls = context_service
    payload = {"isError": True, "content": [{"type": "text", "text": "failure" * 3000}]}

    result = await service._present(runner, context(host), payload)
    receipt = json.loads(result["error"]["message"])
    saved = await service.content.load_result(receipt["result_id"], context(host), "example")

    assert not result["ok"]
    assert len(result["error"]["message"]) < 6000
    assert saved["payload"] == payload


@pytest.mark.asyncio
async def test_oversized_selection_asks_for_a_narrower_read(host):
    store = ContentStore(host)
    payload = {"key" * 3000: "retained"}
    receipt, _ = await store.present(payload, context(host), "example")
    saved = await store.load_result(receipt["result_id"], context(host), "example")

    result = store.read_result(saved, receipt["read"])

    assert result == {
        "result_id": receipt["result_id"],
        "type": "object",
        "complete": False,
        "guidance": READ_TOO_LARGE,
    }
    assert saved["payload"] == payload


@pytest.mark.asyncio
async def test_string_pages_shrink_until_escaped_text_fits(host):
    store = ContentStore(host)
    text = "" * 3000
    receipt, _ = await store.present({"text": text}, context(host), "example")
    saved = await store.load_result(receipt["result_id"], context(host), "example")
    arguments = {**receipt["read"], "pointer": "/text"}
    pieces = []
    while True:
        page = store.read_result(saved, arguments)
        assert len(json.dumps(page, ensure_ascii=False)) <= RESULT_VIEW_CHARACTERS
        pieces.append(page["value"])
        if "next" not in page:
            break
        arguments = page["next"]

    assert "".join(pieces) == text
