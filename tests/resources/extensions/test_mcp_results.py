"""Mcp: results behavior."""

from __future__ import annotations

import asyncio
import json

import pytest

from core.tools.availability import ToolAccess
from core.tools.tools import ToolDefinitionProfileContext
from resources.extensions.mcp.content import ContentStore
from resources.extensions.mcp.extension import remote_tool_name
from tests.resources.extensions.mcp_helpers import (
    context,
)
from tests.resources.extensions.mcp_helpers import (
    context_service as context_service,
)
from tests.resources.extensions.mcp_helpers import (
    host as host,
)


@pytest.mark.asyncio
async def test_large_result_is_durable_and_exactly_readable_in_chunks(host):
    from resources.extensions.mcp.content import RESULT_VIEW_CHARACTERS

    store = ContentStore(host, host.data_dir / "content")
    payload = {
        "content": [{"type": "text", "text": "test-owned-text-ä" * 2000}],
        "_meta": {"keep": True},
    }
    receipt, _ = await store.present(payload, context(host), "example")
    restored = ContentStore(host, host.data_dir / "content")
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

    assert not receipt["complete"]
    assert len(json.dumps(receipt)) < RESULT_VIEW_CHARACTERS
    assert "".join(pieces) == payload["content"][0]["text"]
    assert document["payload"]["_meta"] == payload["_meta"]


@pytest.mark.asyncio
async def test_result_reader_preserves_agent_and_project_ownership(host):
    store = ContentStore(host, host.data_dir / "content")
    receipt, _ = await store.present({"sentinel": True}, context(host), "example")

    with pytest.raises(ValueError):
        await store.load_result(receipt["result_id"], context(host, "bob"), "example")
    with pytest.raises(ValueError):
        await store.load_result(receipt["result_id"], context(host, project="other"), "example")
    with pytest.raises(ValueError):
        await store.load_result("../outside", context(host), "example")


@pytest.mark.asyncio
async def test_result_reader_filters_rows_and_paginates_without_losing_values(host):
    store = ContentStore(host, host.data_dir / "content")
    receipt, _ = await store.present(
        {"rows": [{"id": index, "private": "unused"} for index in range(31)]},
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
    store = ContentStore(host, host.data_dir / "content")
    receipt, _ = await store.present(
        {"rows": [{"selected": "sentinel " * 100, "unrequested": "do not include"}]},
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
        {"sentinel": True}, context(host), "example", source="inspect"
    )
    host.resolve_agent(None, "alice").tool_access = ToolAccess(
        denied=(remote_tool_name("example", "inspect"),)
    )

    result = await registry.dispatch(context(host), receipt["read"])

    assert not result["ok"]


@pytest.mark.asyncio
async def test_connection_disconnect_keeps_the_fixed_model_definition(context_service):
    service, registry, runner, calls = context_service
    profile = ToolDefinitionProfileContext(agent_id="alice")
    before = registry.provider_definitions(profile_context=profile)

    await service.manage("disconnect", {"id": "example"})

    assert registry.provider_definitions(profile_context=profile) == before


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
async def test_oversized_object_keys_return_file_without_unbounded_context(host):
    store = ContentStore(host, host.data_dir)
    payload = {"key" * 3000: "retained"}
    receipt, _ = await store.present(payload, context(host), "example")
    saved = await store.load_result(receipt["result_id"], context(host), "example")

    result = store.read_result(saved, receipt["read"])

    assert len(json.dumps(result)) < 6000
    assert result["result_file"] == receipt["result_file"]
    assert saved["payload"] == payload


@pytest.mark.asyncio
async def test_short_result_ids_claim_files_across_concurrent_stores(host, monkeypatch):
    from core.utils import ids

    store = ContentStore(host, host.data_dir / "content")
    other = ContentStore(host, host.data_dir / "content")
    values = iter((1, 1, 2))
    monkeypatch.setattr(ids.secrets, "randbits", lambda _bits: next(values))
    first, second = await asyncio.gather(
        store.present({"sentinel": "first"}, context(host), "example"),
        other.present({"sentinel": "second"}, context(host), "example"),
    )
    assert {first[0]["result_id"], second[0]["result_id"]} == {
        "res_000000000001",
        "res_000000000002",
    }
    restored = ContentStore(host, host.data_dir / "content")
    assert (await restored.load_result(first[0]["result_id"], context(host), "example"))[
        "payload"
    ] == {"sentinel": "first"}
    assert (await restored.load_result(second[0]["result_id"], context(host), "example"))[
        "payload"
    ] == {"sentinel": "second"}
