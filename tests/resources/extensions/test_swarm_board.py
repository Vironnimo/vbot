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
        groups=groups,
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
async def test_editor_catalog_delivers_default_without_replacing_saved_instructions(board):
    from resources.extensions.swarm.agent_text import DEFAULT_INSTRUCTIONS

    profile = await board.store.get_profile(board.swarm["profile_snapshot"]["id"])
    saved = await board.store.save_profile(
        {**profile, "instructions": "saved-instructions-sentinel"},
        expected_revision=profile["revision"],
    )
    catalog = (await board.service.operation("catalog", {}))["catalog"]
    assert catalog["prompt_defaults"]["instructions"] == DEFAULT_INSTRUCTIONS
    assert (await board.store.get_profile(saved["id"]))[
        "instructions"
    ] == "saved-instructions-sentinel"
    assert (await board.store.get_swarm(board.swarm["id"]))["profile_snapshot"][
        "instructions"
    ] == ""


@pytest.mark.asyncio
async def test_state_guidance_reaches_native_model_definition(board):
    from resources.extensions.swarm.agent_text import STATE_DESCRIPTION

    names = ("swarm_board", "swarm_inbox", "swarm_state")
    definitions = board.tools.provider_definitions(names, session_grants=names)
    state = next(tool for tool in definitions if tool["name"] == "swarm_state")
    assert state["description"] == STATE_DESCRIPTION
    assert set(state["parameters"]["properties"]["action"]["enum"]) == {
        "status",
        "name",
        "wait",
        "done",
    }


@pytest.mark.asyncio
async def test_swarm_get_projects_canonical_run_activity(board, monkeypatch):
    participant = board.swarm["participants"][0]

    async def active(_swarm_id, _run_id):
        return SimpleNamespace(run=SimpleNamespace(status=SimpleNamespace(value="running")))

    await board.store.record_run_started(
        board.swarm["id"], participant["id"], run_id="run-active", expected_epoch=0
    )
    monkeypatch.setattr(board.groups, "owned_run", active)
    projected = await board.service.operation("swarms.get", {"swarm_id": board.swarm["id"]})
    assert projected["swarm"]["participants"][0]["run_active"] is True

    async def retained(_swarm_id, _run_id):
        return SimpleNamespace(run=None)

    monkeypatch.setattr(board.groups, "owned_run", retained)
    projected = await board.service.operation("swarms.get", {"swarm_id": board.swarm["id"]})
    assert projected["swarm"]["participants"][0]["run_active"] is False


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


@pytest.mark.asyncio
async def test_resume_recovers_after_missing_binding_creation_failure(board, monkeypatch):
    swarm_id = board.swarm["id"]
    await board.store.begin_stop(swarm_id, request_id="stop", actor="test")
    await board.groups.close_group(swarm_id)
    await board.store.finish_stop(
        swarm_id, request_id="stop", actor="test", drain_report={"closed": True, "run_ids": []}
    )
    original_list = board.groups.list
    original_create = board.groups.create

    async def no_bindings(*_args, **_kwargs):
        return []

    async def fail_create(*_args, **_kwargs):
        raise RuntimeError("fixture binding failure")

    monkeypatch.setattr(board.groups, "list", no_bindings)
    monkeypatch.setattr(board.groups, "create", fail_create)
    with pytest.raises(RuntimeError, match="fixture binding failure"):
        await board.service.operation("swarms.resume", {"swarm_id": swarm_id, "request_id": "bad"})
    assert (await board.store.get_swarm(swarm_id))["state"] == "needs_attention"
    monkeypatch.setattr(board.groups, "list", original_list)
    monkeypatch.setattr(board.groups, "create", original_create)

    async def admit(_handle, participant_id, _input):
        return SimpleNamespace(run_id=f"resumed-{participant_id}")

    monkeypatch.setattr(board.groups, "start", admit)
    resumed = await board.service.operation(
        "swarms.resume", {"swarm_id": swarm_id, "request_id": "good"}
    )
    assert len(resumed["runs"]) == len(board.bindings)
    assert all("run_id" in admission and "error" not in admission for admission in resumed["runs"])
    assert await board.groups.list(swarm_id) == await original_list(swarm_id)


@pytest.mark.asyncio
async def test_create_pings_opening_atomically_without_joining_recipients(board):
    peer = board.bindings[1].participant_id
    arguments = {
        "action": "create",
        "title": "Review",
        "text": "Please review this draft",
        "recipients": [peer, peer],
        "request_id": "create-ping",
    }
    created, _ = await call(board, arguments)
    assert created["ok"]
    data = created["data"]
    inbox = await board.store.prepare_inbox_delivery(board.swarm["id"], peer)
    assert [entry["id"] for entry in inbox["entries"]] == [
        data["opening_post_id"],
        data["main_announcement_id"],
    ]
    discussions = await board.store.list_discussions(board.swarm["id"], peer)
    assert not next(row for row in discussions.entries if row["id"] == data["discussion_id"])[
        "joined"
    ]
    replay, _ = await call(board, arguments)
    assert replay["data"]["replayed"]
    conflict, _ = await call(board, {**arguments, "recipients": []})
    assert conflict["error"]["code"] == "request_conflict"
    invalid, _ = await call(
        board, {**arguments, "recipients": [peer, "foreign"], "request_id": "bad"}
    )
    assert invalid["error"]["code"] == "invalid_recipient"
    assert (
        await board.store.list_discussions(board.swarm["id"], peer)
    ).entries == discussions.entries
    assert (await board.store.prepare_inbox_delivery(board.swarm["id"], peer))["entries"] == inbox[
        "entries"
    ]


@pytest.mark.asyncio
async def test_reply_uses_owned_message_discussion_and_rejects_contradiction(board):
    created, _ = await call(
        board, {"action": "create", "title": "Topic", "text": "Opening", "request_id": "topic"}
    )
    topic = created["data"]
    arguments = {
        "action": "post",
        "text": "Answer",
        "reply_to": topic["opening_post_id"],
        "request_id": "answer",
    }
    reply, _ = await call(board, arguments, peer=1)
    assert reply["data"]["discussion_id"] == topic["discussion_id"]
    replay, _ = await call(board, {**arguments, "discussion_id": topic["discussion_id"]}, peer=1)
    assert replay["data"]["replayed"]
    mismatch, _ = await call(
        board,
        {**arguments, "discussion_id": board.swarm["main_discussion_id"], "request_id": "mismatch"},
        peer=1,
    )
    assert mismatch["error"]["code"] == "reply_discussion_mismatch"
    missing, _ = await call(
        board, {**arguments, "reply_to": "foreign", "request_id": "foreign"}, peer=1
    )
    assert missing["error"]["code"] == "message_not_found"
    assert (
        len(
            (
                await board.store.read_posts(
                    board.swarm["id"],
                    board.bindings[0].participant_id,
                    discussion_id=topic["discussion_id"],
                )
            ).entries
        )
        == 2
    )


@pytest.mark.asyncio
async def test_board_validation_identifies_the_field_before_any_effect(board):
    from resources.extensions.swarm.extension import _validate_board
    from resources.extensions.swarm.store import SwarmStoreError

    for arguments, field in [
        ({"action": "post", "text": "missing request"}, "request_id"),
        ({"action": "list", "text": "inapplicable"}, "text"),
        ({"action": "join"}, "discussion_id"),
        ({"action": "list", "limti": 1}, "limti"),
    ]:
        with pytest.raises(SwarmStoreError) as error:
            _validate_board(arguments)
        assert error.value.field == field
        context = replace(board.contexts[0], session_tool_grants=("swarm_board",))
        result = await board.tools.dispatch(context, arguments, allowed_tools=["swarm_board"])
        assert result["error"]["code"] == "invalid_arguments"
        assert context._delivery_receipts == []
    assert not (
        await board.store.read_posts(board.swarm["id"], board.bindings[0].participant_id)
    ).entries


@pytest.mark.asyncio
async def test_create_keeps_finished_peers_out_of_opening_and_announcement_delivery(board):
    peer = board.bindings[1].participant_id
    await board.store.set_participant_state(board.swarm["id"], peer, "cancelled")
    created, _ = await call(
        board,
        {
            "action": "create",
            "title": "Review",
            "text": "Opening",
            "recipients": [peer],
            "request_id": "inactive-create",
        },
    )
    assert created["data"]["inactive_recipients"] == [peer]
    assert not (await board.store.prepare_inbox_delivery(board.swarm["id"], peer))["entries"]
