"""MCP: remote results are preserved, bounded, saved with the Tool Result and read back."""

from __future__ import annotations

import base64
import json
from dataclasses import replace

import pytest

from core.attachments import AttachmentTooLargeError
from core.tools.availability import ToolAccess
from resources.extensions.mcp.content import (
    READ_TOO_LARGE,
    RESULT_DENIED,
    RESULT_MISSING,
    RESULT_READ_CHARACTERS,
    RESULT_READ_ENTRIES,
    RESULT_TEXT_CHARACTERS,
    RESULT_VIEW_CHARACTERS,
    ContentStore,
)
from resources.extensions.mcp.extension import MCP_MESSAGES, remote_tool_name
from tests.resources.extensions.mcp.mcp_test_support import (
    context,
    dispatch,
    model_text,
    payloads,
    tool_target,
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
    text = payload["content"][0]["text"]
    continuation = {
        "action": "read",
        "result_id": receipt["result_id"],
        "pointer": "/content/0/text",
        "offset": RESULT_TEXT_CHARACTERS,
    }
    arguments = {**continuation, "limit": 997}
    pieces = [receipt["content"]]
    while True:
        page = restored.read_result(document, arguments)
        pieces.append(page["content"])
        if "next" not in page:
            break
        arguments = page["next"]
    oversized = restored.read_result(document, {**continuation, "limit": 10_000})

    # The view shows the text start and names the read that continues it; it holds no
    # file path: read is the only way to the saved payload.
    assert set(receipt) == {"result_id", "_meta", "note", "content"}
    assert receipt["content"] == text[:RESULT_TEXT_CHARACTERS]
    assert json.dumps(continuation, separators=(",", ":")) in receipt["note"]
    assert f"first {RESULT_TEXT_CHARACTERS} of {len(text)} characters" in receipt["note"]
    assert len(json.dumps(receipt)) < RESULT_VIEW_CHARACTERS
    assert "".join(pieces) == text
    # A text page holds at most RESULT_READ_CHARACTERS, whatever limit asks for.
    assert oversized["content"] == text[RESULT_TEXT_CHARACTERS:][:RESULT_READ_CHARACTERS]
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
    with pytest.raises(ValueError, match=RESULT_DENIED.format(connection="example")):
        await store.load_result(identifier, context(host), "other")


@pytest.mark.asyncio
async def test_complete_results_and_calls_outside_a_session_keep_no_payload(host):
    store = ContentStore(host)
    small = {"sentinel": True}
    large = {"sentinel": "x" * 7000}

    inline, _ = await store.present(small, context(host), "example")
    outside, _ = await store.present(large, context(host, session=None), "example")

    # A complete result in a Session is shown as its own fields.
    assert inline == small
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

    assert len(first["entries"]) == RESULT_READ_ENTRIES
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


async def _call_returning(payload, context_service, host, monkeypatch):
    """Call the remote Tool through the connection Tool; the server answers *payload*."""
    service, registry, runner, calls = context_service

    async def answer(operation, arguments, invocation_context=None):
        return payload

    target = await tool_target(registry, host)
    monkeypatch.setattr(runner, "invoke", answer)
    return await dispatch(
        registry, host, {"action": "call", "target": target, "arguments": {"value": "x"}}
    )


@pytest.mark.asyncio
async def test_large_error_keeps_full_payload_and_bounded_receipt(
    context_service, host, monkeypatch
):
    service, registry, runner, calls = context_service
    report = "start " + "failure " * 3000 + "NameError: final line"
    payload = {"isError": True, "content": [{"type": "text", "text": report}]}

    result = await _call_returning(payload, context_service, host, monkeypatch)
    message = result["error"]["message"]
    identifier = payload_id(message)
    saved = await service.content.load_result(identifier, context(host), "example")
    page = await dispatch(
        registry, host, {"action": "read", "result_id": identifier, "pointer": "/content/0/text"}
    )

    assert not result["ok"]
    assert message.startswith("The MCP tool inspect reported an error:\nstart failure")
    assert "NameError: final line\n\nThe tool inspect could have changed the application" in message
    assert '"pointer":"/content/0/text","offset":1000' in message
    assert len(message) < 4000
    assert saved["payload"] == payload
    assert page["data"]["content"] == report[:4000]


def payload_id(message: str) -> str:
    return message.split('"result_id":"', 1)[1].split('"', 1)[0]


@pytest.mark.asyncio
async def test_tool_error_reads_as_the_servers_own_report(context_service, host, monkeypatch):
    payload = {
        "isError": True,
        "content": [{"type": "text", "text": "Traceback:\nNameError: name 'scene' is undefined"}],
    }

    result = await _call_returning(payload, context_service, host, monkeypatch)

    assert model_text(result) == (
        "Error (mcp_tool_error): The MCP tool inspect reported an error:\n"
        "Traceback:\nNameError: name 'scene' is undefined\n\n"
        "The tool inspect could have changed the application before it failed. If the error "
        "concerns this call, for example an argument or an item it names, fix the call and "
        "send it again. If the error concerns the setup, for example the application not "
        "running, a program not found or a missing key or setting, tell the user what the "
        "error says. The user configures this connection in Settings -> Integrations -> "
        "Extensions -> MCP connections. Repeat the unchanged call only when the error says "
        "the problem is temporary."
    )
    assert payloads(host).rows == {}


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
        pieces.append(page["content"])
        if "next" not in page:
            break
        arguments = page["next"]

    assert "".join(pieces) == text


@pytest.mark.asyncio
async def test_unknown_metadata_and_media_are_preserved(host):
    payload = {
        "content": [
            {
                "type": "image",
                "mimeType": "image/png",
                "data": base64.b64encode(b"image-bytes").decode(),
                "_meta": {"detail": "original"},
            }
        ],
        "structuredContent": {"answer": 42},
        "_meta": {"vendor": {"future": True}},
    }
    result, artifacts = await ContentStore(host).preserve(payload)
    assert result["_meta"] == payload["_meta"]
    assert result["structuredContent"] == payload["structuredContent"]
    assert result["content"][0]["_meta"] == {"detail": "original"}
    assert result["content"][0]["size_bytes"] == len(b"image-bytes")
    assert len(artifacts) == 1
    assert "data" in payload["content"][0]


@pytest.mark.asyncio
async def test_media_shaped_application_data_and_metadata_are_not_rewritten(host):
    media_shape = {"type": "image", "data": "not base64", "mimeType": "image/png"}
    resource_shape = {"uri": "app://item", "blob": "ordinary application data"}
    payload = {
        "structuredContent": {"rows": [media_shape, resource_shape]},
        "_meta": media_shape,
        "content": [{"type": "text", "text": "sentinel", "_meta": resource_shape}],
        "tools": [{"name": "example", "inputSchema": {"examples": [media_shape]}}],
    }
    result, artifacts = await ContentStore(host).preserve(payload)
    assert result == payload
    assert artifacts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("position", ["content", "contents", "messages", "message_list"])
async def test_protocol_resource_and_prompt_media_positions_are_preserved(host, position):
    raw = b"test-owned-media"
    resource = {
        "uri": "test://blob",
        "mimeType": "image/png",
        "blob": base64.b64encode(raw).decode(),
    }
    block = {"type": "resource", "resource": resource}
    if position == "contents":
        payload = {"contents": [resource]}
    elif position == "content":
        payload = {"content": [block]}
    else:
        payload = {
            "messages": [
                {"role": "user", "content": [block] if position == "message_list" else block}
            ]
        }
    result, artifacts = await ContentStore(host).preserve(payload)
    if position == "contents":
        preserved = result["contents"][0]
    elif position == "content":
        preserved = result["content"][0]["resource"]
    else:
        content = result["messages"][0]["content"]
        preserved = (content[0] if isinstance(content, list) else content)["resource"]
    assert preserved["size_bytes"] == len(raw)
    assert "path" in preserved
    assert "blob" not in preserved
    assert "blob" in resource
    assert artifacts == []


@pytest.mark.asyncio
async def test_media_attachment_delivery_refuses_is_omitted_with_a_marker(host):
    def reject(name, data):
        raise AttachmentTooLargeError("test-owned-size-limit")

    host = replace(host, store_attachment=reject)
    raw = b"media-sentinel"
    before = set(host.data_dir.rglob("*"))
    result, artifacts = await ContentStore(host).preserve(
        {
            "content": [
                {"type": "image", "mimeType": "image/png", "data": base64.b64encode(raw).decode()}
            ]
        }
    )

    # No unmanaged copy: the bytes are gone and the marker says why.
    assert result["content"][0] == {
        "type": "image",
        "mimeType": "image/png",
        "content_omitted": True,
        "media_delivery_error": "test-owned-size-limit",
        "size_bytes": len(raw),
    }
    assert set(host.data_dir.rglob("*")) == before
    assert artifacts == []
