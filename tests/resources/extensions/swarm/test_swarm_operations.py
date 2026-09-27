"""Swarm management operations: catalog, snapshots, resume and delete."""

import asyncio
from types import SimpleNamespace

import pytest

from core.agents.temporary import TemporaryAgentConfig
from core.chat import ChatMessage
from core.sessions import SessionAddress
from core.tools.availability import ToolAccess


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
async def test_swarm_get_projects_canonical_run_activity(board, monkeypatch):
    participant = board.swarm["participants"][0]
    lookups: list[tuple[str, list[str]]] = []

    def inspections(run: object) -> object:
        async def owned_runs(swarm_id, run_ids):
            lookups.append((swarm_id, list(run_ids)))
            return {run_id: SimpleNamespace(run=run) for run_id in run_ids}

        return owned_runs

    await board.store.record_run_started(
        board.swarm["id"], participant["id"], run_id="run-active", expected_epoch=0
    )
    running = SimpleNamespace(status=SimpleNamespace(value="running"))
    monkeypatch.setattr(board.groups, "owned_runs", inspections(running))
    projected = await board.service.operation("swarms.get", {"swarm_id": board.swarm["id"]})
    assert projected["swarm"]["participants"][0]["run_active"] is True
    # All participants resolve through one batched ownership read.
    assert lookups == [(board.swarm["id"], ["run-active"])]

    monkeypatch.setattr(board.groups, "owned_runs", inspections(None))
    projected = await board.service.operation("swarms.get", {"swarm_id": board.swarm["id"]})
    assert projected["swarm"]["participants"][0]["run_active"] is False

    async def unknown(_swarm_id, _run_ids):
        return {}

    monkeypatch.setattr(board.groups, "owned_runs", unknown)
    projected = await board.service.operation("swarms.get", {"swarm_id": board.swarm["id"]})
    assert projected["swarm"]["participants"][0]["run_active"] is False


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
    assert snapshot["swarm"]["newest_wiki_page_number"] is None
    posted = await operations.invoke(
        "board.post",
        {"swarm_id": board.swarm["id"], "text": "operator note", "request_id": "operator"},
    )
    assert posted["discussion_id"] == board.swarm["main_discussion_id"]
    await operations.invoke(
        "wiki",
        {
            "swarm_id": board.swarm["id"],
            "action": "create",
            "title": "Notes",
            "content": "x",
            "request_id": "page",
        },
    )
    # The page links "#N" and "wN" only up to the newest post and page.
    snapshot = await operations.invoke("swarms.get", {"swarm_id": board.swarm["id"]})
    newest = await board.store.read_human_posts(board.swarm["id"], limit=1)
    assert snapshot["swarm"]["newest_post_sequence"] == newest.entries[0]["sequence"]
    assert snapshot["swarm"]["newest_wiki_page_number"] == 1
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
@pytest.mark.parametrize("selected", [None, 1], ids=["every-inactive", "selected"])
async def test_resume_admits_inactive_participants_or_only_the_selected_one(
    board, monkeypatch, selected
):
    swarm_id = board.swarm["id"]
    peers = board.bindings
    await board.service.store.set_swarm_state(swarm_id, "running")
    for peer in peers:
        await board.service.store.set_participant_state(swarm_id, peer.participant_id, "failed")
    admitted = []

    async def start(handle, participant_id, input):
        admitted.append(participant_id)
        return SimpleNamespace(run_id=f"resumed-{participant_id}")

    monkeypatch.setattr(board.groups, "start", start)
    arguments = {"swarm_id": swarm_id, "request_id": "one"}
    expected = [peer.participant_id for peer in peers]
    if selected is not None:
        arguments["participant_id"] = expected[selected]
        expected = [expected[selected]]
    result = await board.operations.invoke("swarms.resume", arguments)
    assert admitted == expected
    assert len(result["runs"]) == len(expected)


@pytest.mark.asyncio
async def test_delete_rejects_open_swarm_without_touching_data(board):
    sid = board.swarm["id"]
    with pytest.raises(ValueError, match="swarm_not_stopped"):
        await board.service.operation("swarms.delete", {"swarm_id": sid})
    assert (await board.store.get_swarm(sid))["state"] == "preparing"
    with pytest.raises(ValueError, match="group_not_closed"):
        await board.groups.delete_group(sid)
    assert all(board.sessions.exists(binding.address) for binding in board.bindings)


@pytest.mark.asyncio
async def test_delete_removes_board_and_bound_sessions_but_keeps_profile_and_other_work(board):
    sid = board.swarm["id"]
    profile = board.swarm["profile_snapshot"]
    other = await board.store.create_swarm(
        profile["id"],
        "Keep this Swarm",
        {"cwd": "C:/work"},
        request_id="other",
        expected_profile_revision=profile["revision"],
    )
    ordinary = SessionAddress(None, "ordinary", "retained")
    board.sessions.get_or_create(ordinary)
    binding = board.bindings[0]
    board.sessions.get(binding.address).append(
        ChatMessage(
            id="history",
            role="user",
            content="Private history",
            timestamp="2026-09-08T10:00:00+00:00",
        )
    )
    foreign = board.groups._registry.create(
        owner_name="other-extension",
        group_id=sid,
        participant_id="foreign",
        config=TemporaryAgentConfig(
            model="fixture/model",
            cwd=board.contexts[0].workspace,
            tool_access=ToolAccess(mode="selected", allowed=()),
            allowed_skills=[],
            tools={},
            name="Foreign",
        ),
    )
    await board.store.post_human(sid, text="Delete this Board post", request_id="post")
    await board.store.prepare_inbox_delivery(sid, binding.participant_id)
    await board.service.operation("swarms.stop", {"swarm_id": sid, "request_id": "stop"})
    result = await board.service.operation("swarms.delete", {"swarm_id": sid})
    assert result == {"swarm_id": sid, "deleted": True}
    assert await board.service.operation("swarms.delete", {"swarm_id": sid}) == result
    assert not any(board.sessions.exists(item.address) for item in board.bindings)
    assert board.sessions.exists(ordinary) and board.sessions.exists(foreign.address)
    assert await board.store.get_profile(profile["id"]) == profile
    assert [row["id"] for row in (await board.store.list_swarms()).entries] == [other["swarm_id"]]
    (database,) = board.databases.open_databases()
    with database.read() as connection:
        for table in (
            "posts",
            "recipients",
            "delivery_batches",
            "delivery_batch_entries",
            "participant_sessions",
            "swarm_events",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == (
                1 if table == "posts" else 0
            )
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        assert connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1
    with pytest.raises(ValueError, match="swarm_not_found"):
        await board.service.operation("swarms.resume", {"swarm_id": sid, "request_id": "resume"})


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["sessions", "board"])
async def test_interrupted_delete_remains_retryable_and_cannot_resume(board, monkeypatch, stage):
    sid = board.swarm["id"]
    await board.service.operation("swarms.stop", {"swarm_id": sid, "request_id": "stop"})
    await board.store.begin_resume(sid, request_id="resume", actor="user")
    await board.service.operation(
        "swarms.stop", {"swarm_id": sid, "request_id": "stop-after-resume"}
    )
    target, method = (
        (board.groups, "delete_group") if stage == "sessions" else (board.store, "delete_swarm")
    )
    original = getattr(target, method)

    async def fail(*args, **kwargs):
        raise OSError("Deletion interrupted")

    monkeypatch.setattr(target, method, fail)
    with pytest.raises(OSError, match="Deletion interrupted"):
        await board.service.operation("swarms.delete", {"swarm_id": sid})
    await board.store.recover_interrupted()
    assert (await board.store.get_swarm(sid))["state"] == "deleting"
    for operation, request in (("resume", "resume"), ("stop", "stop")):
        with pytest.raises(ValueError, match="swarm_closed"):
            await board.service.operation(
                f"swarms.{operation}", {"swarm_id": sid, "request_id": request}
            )
    monkeypatch.setattr(target, method, original)
    assert (await board.service.operation("swarms.delete", {"swarm_id": sid}))["deleted"]
    assert not any(board.sessions.exists(item.address) for item in board.bindings)


@pytest.mark.asyncio
async def test_resume_waits_for_delete_and_cannot_recreate_sessions(board, monkeypatch):
    sid = board.swarm["id"]
    await board.service.operation("swarms.stop", {"swarm_id": sid, "request_id": "stop"})
    entered, release = asyncio.Event(), asyncio.Event()
    original = board.groups.delete_group

    async def delayed(group_id):
        entered.set()
        await release.wait()
        return await original(group_id)

    monkeypatch.setattr(board.groups, "delete_group", delayed)
    deletion = asyncio.create_task(board.service.operation("swarms.delete", {"swarm_id": sid}))
    await entered.wait()
    resume = asyncio.create_task(
        board.service.operation("swarms.resume", {"swarm_id": sid, "request_id": "resume"})
    )
    release.set()
    assert (await deletion)["deleted"]
    with pytest.raises(ValueError, match="swarm_not_found"):
        await resume
    assert not any(board.sessions.exists(item.address) for item in board.bindings)


@pytest.mark.asyncio
async def test_post_survives_resume_preparation_failure(board, monkeypatch):
    sid = board.swarm["id"]
    await board.service.operation("swarms.stop", {"swarm_id": sid, "request_id": "stop"})

    async def fail_open(*args, **kwargs):
        raise RuntimeError("test-owned preparation failure")

    monkeypatch.setattr(board.groups, "open_group", fail_open)
    result = await board.service.operation(
        "board.post", {"swarm_id": sid, "text": "retained followup", "request_id": "post"}
    )
    assert result["resume_failed"] is True
    entries = await board.store.read_human_posts(sid, message_id=result["post_id"])
    assert entries.entries[0]["text"] == "retained followup"
