import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
import pytest_asyncio

from core.agents.temporary import (
    TemporaryAgentConfig,
    TemporaryAgentRegistry,
    TemporaryExecutionGroups,
)
from core.chat import ChatMessage
from core.extensions import ExtensionAPI, ExtensionRecord, ExtensionRegistry
from core.extensions.extensions import ExtensionDeclarations
from core.extensions.operations import ExtensionHost
from core.runs import ChatRunManager, RunExecutionOwner
from core.sessions import ChatSessionManager
from core.sessions.format import write_bootstrap_marker
from core.tools import ToolContext, ToolRegistry
from core.tools.availability import ToolAccess
from resources.extensions.swarm.extension import register


@pytest_asyncio.fixture
async def board(tmp_path):
    write_bootstrap_marker(tmp_path)
    sessions = ChatSessionManager(tmp_path)
    manager = ChatRunManager()
    identity = SimpleNamespace(name="swarm", epoch="registration")
    groups = TemporaryExecutionGroups(
        TemporaryAgentRegistry(sessions),
        None,
        lambda value: value is identity,
        identity,
        run_manager=manager,
    )
    declarations = ExtensionDeclarations()
    api = ExtensionAPI("swarm", declarations, config={}, logger=None)
    register(api)
    registry = ExtensionRegistry()
    registry._records.append(
        ExtensionRecord(
            "swarm", tmp_path, tmp_path / "extension.py", "loaded", declarations=declarations
        )
    )
    tools = ToolRegistry()
    registry.apply_tools(tools)
    host = ExtensionHost(
        data_dir=tmp_path,
        sample=None,
        resolve_agent=None,
        store_attachment=None,
        resolve_credential=None,
        set_credential=None,
        state_dir=tmp_path,
        temporary_agents=groups,
    )
    await api.operations.startup[0](host)
    service = declarations.tools[0].handler.__self__
    store = service.store
    profile = await store.save_profile(
        {
            "schema_version": 1,
            "slug": "fixture",
            "name": "Fixture",
            "participants": [{"model": "fixture/model", "count": 3}],
            "working_directory": {"kind": "directory", "path": str(tmp_path)},
            "tool_access": {"mode": "selected", "allowed": []},
        },
        expected_revision=None,
    )
    swarm = await store.create_swarm(
        profile["id"],
        "fixture-goal",
        {"cwd": str(tmp_path)},
        request_id="start",
        expected_profile_revision=1,
    )
    swarm = await store.get_swarm(swarm["swarm_id"])
    bindings = []
    for peer in swarm["participants"]:
        binding = await groups.create(
            swarm["id"],
            peer["id"],
            TemporaryAgentConfig(
                model="fixture/model",
                cwd=tmp_path,
                tool_access=ToolAccess(mode="selected", allowed=()),
                allowed_skills=[],
                tools={},
                name=peer["display_name"],
            ),
        )
        await store.bind_participant_session(binding)
        bindings.append(binding)
    handle = await groups.open_group(swarm["id"])
    await store.bind_execution_epoch(swarm["id"], expected_epoch=0, execution_epoch=handle.epoch)
    contexts = [
        ToolContext(
            agent_id=binding.address.agent_id,
            session_id=binding.address.session_id,
            project_id=binding.address.project_id,
            run_id=f"run-{index}",
            tool_call_id=f"call-{index}",
            tool_name="swarm_board",
            tool_call_index=0,
            workspace=tmp_path,
            vbot_root=tmp_path,
            data_root=tmp_path,
            execution_owner=RunExecutionOwner(
                extension="swarm",
                group_id=swarm["id"],
                participant_id=binding.participant_id,
                epoch=handle.epoch,
                generation_id=binding.generation_id,
            ),
            delivery_receipt_hook=lambda *_: None,
        )
        for index, binding in enumerate(bindings)
    ]
    fixture = SimpleNamespace(
        service=service,
        store=store,
        swarm=swarm,
        contexts=contexts,
        bindings=bindings,
        sessions=sessions,
        tools=tools,
        registry=registry,
    )
    try:
        yield fixture
    finally:
        await service.close()
        await manager.aclose()
        sessions.close()


async def call(board, arguments, peer=0):
    context = replace(board.contexts[peer])
    result = await board.tools.get("swarm_board").handler(context, arguments)
    return result, context


@pytest.mark.asyncio
async def test_registered_board_public_posts_pages_and_durable_read_receipt(board):
    result, _ = await call(board, {"action": "list"})
    main = result["data"]["main_discussion_id"]
    assert result["data"]["entries"][0]["id"] == main
    peer = board.bindings[1].participant_id
    posted, _ = await call(
        board,
        {"action": "post", "text": "full text", "request_id": "one", "recipients": [peer, peer]},
    )
    assert posted["ok"]
    replay, _ = await call(
        board,
        {"action": "post", "text": "full text", "request_id": "one", "recipients": [peer, peer]},
    )
    assert replay["data"]["replayed"]
    read, context = await call(board, {"action": "read"}, peer=1)
    assert len(read["data"]["entries"]) == 1
    assert read["data"]["entries"][0]["text"] == "full text"
    assert len(context._delivery_receipts) == 1
    receipt_id, content_hash, effect = context._delivery_receipts[0]
    assert not await board.store.reconcile_delivery(receipt_id)
    binding = board.bindings[1]
    await board.sessions.append_messages_with_receipts_async(
        binding.address,
        generation_id=binding.generation_id,
        owner_name="swarm",
        messages=[
            ChatMessage.tool(
                tool_call_id=context.tool_call_id, name="swarm_board", content=json.dumps(read)
            )
        ],
        receipts=[(0, receipt_id, content_hash, effect, "tool")],
    )
    assert await board.store.reconcile_delivery(receipt_id)
    assert (await board.store.prepare_inbox_delivery(board.swarm["id"], peer))["entries"] == []
    assert board.tools.get("swarm_board").catalog_visible is False
    assert board.tools.get("swarm_board").session_scoped


@pytest.mark.asyncio
async def test_board_discussion_join_leave_reply_and_exact_pagination(board):
    created, _ = await call(
        board, {"action": "create", "title": "Topic", "text": "opening", "request_id": "create"}
    )
    discussion = created["data"]["discussion_id"]
    first, _ = await call(board, {"action": "list", "limit": 1})
    next_page, _ = await call(board, first["data"]["next_call"]["arguments"])
    assert next_page["data"]["entries"][0]["id"] == discussion
    joined, _ = await call(board, {"action": "join", "discussion_id": discussion}, peer=1)
    opening = joined["data"]["recent"]["entries"][0]
    response, _ = await call(
        board,
        {
            "action": "post",
            "discussion_id": discussion,
            "text": "answer",
            "reply_to": opening["id"],
            "request_id": "reply",
        },
        peer=1,
    )
    assert response["ok"]
    newest, _ = await call(board, {"action": "read", "discussion_id": discussion, "limit": 1})
    assert newest["data"]["entries"][0]["text"] == "answer"
    older, _ = await call(board, newest["data"]["next_call"]["arguments"])
    assert older["data"]["entries"] == [opening]
    one, _ = await call(board, {"action": "read", "message_id": opening["id"]})
    assert one["data"]["entries"] == [opening]
    left, _ = await call(board, {"action": "leave", "discussion_id": discussion}, peer=1)
    assert left["data"]["joined"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"action": "other"},
        {"action": "list", "limit": True},
        {"action": "list", "limit": "1"},
        {"action": "list", "limit": 0},
        {"action": "list", "limit": 101},
        {"action": "list", "swarm_id": "foreign"},
        {"action": "read", "message_id": "foreign", "limit": 20},
        {"action": "read", "text": "wrong"},
        {"action": "read", "cursor": None},
        {"action": "post", "text": "x"},
        {"action": "post", "text": "", "request_id": "id"},
        {"action": "post", "text": "x" * 16001, "request_id": "id"},
        {"action": "create", "text": "x", "request_id": "id"},
        {"action": "join"},
        {"action": "leave"},
    ],
)
async def test_invalid_board_calls_have_no_effect(board, arguments):
    result, context = await call(board, arguments)
    assert not result["ok"]
    assert context._delivery_receipts == []
    page = await board.store.read_posts(board.swarm["id"], board.bindings[0].participant_id)
    assert page.entries == ()


@pytest.mark.asyncio
async def test_board_rejects_forged_context_and_conflicting_idempotence(board):
    foreign = replace(board.contexts[0], session_id=board.contexts[1].session_id)
    result = await board.service.board(foreign, {"action": "list"})
    assert not result["ok"]
    assert (await call(board, {"action": "post", "text": "first", "request_id": "same"}))[0]["ok"]
    conflict, _ = await call(board, {"action": "post", "text": "changed", "request_id": "same"})
    assert conflict["error"]["code"] == "request_conflict"
    page = await board.store.read_posts(board.swarm["id"], board.bindings[0].participant_id)
    assert len(page.entries) == 1


@pytest.mark.asyncio
async def test_management_profiles_and_swarm_snapshots_are_owner_operations(board):
    operations = board.service.api.operations
    profiles = await operations.invoke("profiles.list", {})
    assert profiles["entries"][0]["slug"] == "fixture"

    profile = await operations.invoke("profiles.get", {"profile_id": profiles["entries"][0]["id"]})
    assert profile["profile"]["revision"] == 1
    swarms = await operations.invoke("swarms.list", {})
    assert swarms["entries"][0]["id"] == board.swarm["id"]
    events = await operations.invoke("swarms.events", {"swarm_id": board.swarm["id"]})
    assert events == {"entries": [], "has_more": False}
    snapshot = await operations.invoke("swarms.get", {"swarm_id": board.swarm["id"]})
    assert snapshot["swarm"]["main_discussion_id"] == board.swarm["main_discussion_id"]
    posted = await operations.invoke(
        "board.post",
        {"swarm_id": board.swarm["id"], "text": "operator note", "request_id": "operator"},
    )
    assert posted["discussion_id"] == board.swarm["main_discussion_id"]
    discussions = await operations.invoke("board.list", {"swarm_id": board.swarm["id"]})
    assert discussions["entries"][0]["id"] == board.swarm["main_discussion_id"]
    page = await operations.invoke("board.read", {"swarm_id": board.swarm["id"]})
    assert page["entries"][-1]["text"] == "operator note"
