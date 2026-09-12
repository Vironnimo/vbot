"""Swarm store: execution behavior."""

from __future__ import annotations

# mypy: disable-error-code=arg-type
import pytest

from core.sessions import SessionAddress, TemporarySessionBinding
from resources.extensions.swarm.store import SwarmStore, SwarmStoreError
from tests.resources.extensions.swarm_store_helpers import (
    _profile,
    _swarm,
)
from tests.resources.extensions.swarm_store_helpers import (
    store as store,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["all", "idle", "pull"])
@pytest.mark.parametrize("wake_idle", [True, False])
@pytest.mark.parametrize("state", ["running", "idle"])
async def test_automatic_policy_modes_and_wake_watermark(
    store: SwarmStore, mode: str, wake_idle: bool, state: str
) -> None:
    started = await _swarm(store)
    swarm = await store.get_swarm(started["swarm_id"])
    sender, recipient = [item["id"] for item in swarm["participants"]]
    settings = {**swarm["delivery"], "main": {"mode": mode, "wake_idle": wake_idle}}
    await store.apply_delivery_settings(
        started["swarm_id"],
        settings,
        expected_revision=1,
        request_id=f"policy-{mode}-{wake_idle}",
        actor="test",
    )
    await store.set_participant_state(started["swarm_id"], recipient, state)
    await store.post(
        started["swarm_id"], sender, text="policy", request_id=f"post-{mode}-{wake_idle}-{state}"
    )
    result = await store.prepare_automatic_delivery(
        started["swarm_id"],
        recipient,
        expected_epoch=0,
        admission_boundary=1 if state == "idle" else None,
    )
    assert result["wake"] is (wake_idle and state == "idle")
    if (mode == "pull" and not (wake_idle and state == "idle")) or (
        mode == "idle" and state == "running"
    ):
        assert result["entries"] == []
    else:
        assert len(result["entries"]) == 1
    repeat = await store.prepare_automatic_delivery(
        started["swarm_id"],
        recipient,
        expected_epoch=0,
        admission_boundary=1 if state == "idle" else None,
    )
    assert repeat["wake"] is False
    await store.post(
        started["swarm_id"], sender, text="new", request_id=f"new-{mode}-{wake_idle}-{state}"
    )
    newer = await store.prepare_automatic_delivery(
        started["swarm_id"],
        recipient,
        expected_epoch=0,
        admission_boundary=2 if state == "idle" else None,
    )
    assert newer["wake"] is False  # one unadmitted wake coalesces newer pending posts


@pytest.mark.asyncio
async def test_stop_resume_retires_old_epoch_and_settings_toggle_retracts_wake(
    store: SwarmStore,
) -> None:
    started = await _swarm(store)
    swarm = await store.get_swarm(started["swarm_id"])
    sender, recipient = [item["id"] for item in swarm["participants"]]
    await store.set_participant_state(started["swarm_id"], recipient, "idle")
    await store.post(started["swarm_id"], sender, text="wake", request_id="wake-post")
    assert (
        await store.prepare_automatic_delivery(
            started["swarm_id"], recipient, expected_epoch=0, admission_boundary=1
        )
    )["wake"]
    disabled = {
        **swarm["delivery"],
        "main": {"mode": "all", "wake_idle": False},
        "discussion": {"mode": "all", "wake_idle": False},
        "ping": {"mode": "all", "wake_idle": False},
    }
    await store.apply_delivery_settings(
        started["swarm_id"], disabled, expected_revision=1, request_id="disable", actor="test"
    )
    await store.begin_stop(started["swarm_id"], request_id="stop", actor="test")
    await store.finish_stop(
        started["swarm_id"], request_id="stop-finish", actor="test", drain_report={"drained": True}
    )
    await store.begin_resume(started["swarm_id"], request_id="resume", actor="test")
    with pytest.raises(SwarmStoreError, match="stale_epoch"):
        await store.prepare_automatic_delivery(
            started["swarm_id"], recipient, expected_epoch=0, admission_boundary=2
        )


@pytest.mark.asyncio
async def test_recovery_interrupts_open_swarms_without_admitting_work(tmp_path) -> None:
    path = tmp_path / "recovery.db"
    first = SwarmStore(path)
    await first.open()
    started = await _swarm(first)
    swarm = await first.get_swarm(started["swarm_id"])
    participant = swarm["participants"][0]["id"]
    await first.set_swarm_state(started["swarm_id"], "running")
    await first.set_participant_state(started["swarm_id"], participant, "running")
    await first.close()

    recovered = SwarmStore(path)
    await recovered.open()
    try:
        assert await recovered.recover_interrupted() == [
            {"swarm_id": started["swarm_id"], "state": "interrupted"}
        ]
        snapshot = await recovered.get_swarm(started["swarm_id"])
        assert snapshot["state"] == "interrupted"
        assert [item["state"] for item in snapshot["participants"]] == ["interrupted", "idle"]
        assert await recovered.recover_interrupted() == []
        with pytest.raises(SwarmStoreError, match="swarm_closed"):
            await recovered.prepare_automatic_delivery(
                started["swarm_id"], participant, expected_epoch=0
            )
    finally:
        await recovered.close()


@pytest.mark.asyncio
async def test_settings_enable_returns_wake_intent_and_events_are_bounded(
    store: SwarmStore,
) -> None:
    started = await _swarm(store)
    swarm = await store.get_swarm(started["swarm_id"])
    sender, recipient = [item["id"] for item in swarm["participants"]]
    await store.set_participant_state(started["swarm_id"], recipient, "idle")
    disabled = {**swarm["delivery"], "main": {"mode": "all", "wake_idle": False}}
    await store.apply_delivery_settings(
        started["swarm_id"], disabled, expected_revision=1, request_id="disable", actor="user"
    )
    await store.post(started["swarm_id"], sender, text="pending", request_id="pending")
    enabled = {**disabled, "main": {"mode": "all", "wake_idle": True}}
    applied = await store.apply_delivery_settings(
        started["swarm_id"], enabled, expected_revision=2, request_id="enable", actor="user"
    )

    assert applied["wake_intents"] == [
        {"participant_id": recipient, "pending_count": 1, "settings_revision": 3}
    ]
    assert (
        applied["old"] == disabled and applied["new"] == enabled and applied["effective"] == enabled
    )
    events = await store.list_events(started["swarm_id"], limit=1)
    assert events.entries[0]["kind"] == "settings"
    assert events.entries[0]["old"] == disabled
    assert events.entries[0]["new"] == enabled
    assert events.entries[0]["settings_revision"] == 3
    assert events.has_more is True

    second_profile = await store.save_profile(_profile(slug="other"), expected_revision=None)
    await store.create_swarm(
        second_profile["id"],
        "Other",
        {"cwd": "C:/work"},
        request_id="other-start",
        expected_profile_revision=second_profile["revision"],
    )
    page = await store.list_swarms(limit=1)
    assert len(page.entries) == 1 and page.has_more is True and page.cursor is not None
    assert page.entries[0]["title"] == "Other"


@pytest.mark.asyncio
async def test_wake_claim_preserves_idle_boundary_for_running_idle_delivery(
    store: SwarmStore,
) -> None:
    started = await _swarm(store)
    sender, recipient = [
        item["id"] for item in (await store.get_swarm(started["swarm_id"]))["participants"]
    ]
    await store.set_participant_state(started["swarm_id"], recipient, "idle", idle_boundary=7)
    await store.post(started["swarm_id"], sender, text="idle", request_id="idle-boundary")
    assert (
        await store.prepare_automatic_delivery(
            started["swarm_id"], recipient, expected_epoch=0, admission_boundary=7
        )
    )["wake"]
    assert (await store.claim_wake(started["swarm_id"], recipient, expected_epoch=0))[
        "boundary"
    ] == 7
    assert (
        await store.mark_wake_admitted(
            started["swarm_id"], recipient, expected_epoch=0, run_id="wake-run", boundary=7
        )
    )["admitted"]
    assert (
        await store.mark_wake_admitted(
            started["swarm_id"], recipient, expected_epoch=0, run_id="wake-run", boundary=7
        )
    )["replayed"]
    assert (
        await store.prepare_automatic_delivery(
            started["swarm_id"], recipient, expected_epoch=0, admission_boundary=7
        )
    )["replayed"]


@pytest.mark.asyncio
async def test_status_and_exact_run_finish(store: SwarmStore) -> None:
    started = await _swarm(store)
    participant = (await store.get_swarm(started["swarm_id"]))["participants"][0]["id"]
    status = await store.participant_status(started["swarm_id"], participant, limit=1)
    assert (
        status["roster"][0]["name"]
        == (await store.get_swarm(started["swarm_id"]))["participants"][0]["display_name"]
    )
    assert "epoch" not in status and "usage" not in status
    await store.record_run_started(started["swarm_id"], participant, run_id="new", expected_epoch=0)
    with pytest.raises(SwarmStoreError, match="stale_run"):
        await store.reconcile_run_finished(
            started["swarm_id"], participant, run_id="old", expected_epoch=0, outcome="completed"
        )


@pytest.mark.asyncio
async def test_unadmitted_wake_coalesces_without_advancing_watermark(store: SwarmStore) -> None:
    started = await _swarm(store)
    sender, recipient = [
        item["id"] for item in (await store.get_swarm(started["swarm_id"]))["participants"]
    ]
    await store.set_participant_state(started["swarm_id"], recipient, "idle", idle_boundary=4)
    await store.post(started["swarm_id"], sender, text="A", request_id="wake-a")
    assert (
        await store.prepare_automatic_delivery(
            started["swarm_id"], recipient, expected_epoch=0, admission_boundary=4
        )
    )["wake"]
    connection = store._database._connection  # noqa: SLF001 - durable watermark assertion
    assert connection is not None
    assert (
        connection.execute(
            "SELECT wake_announced_seq FROM participants WHERE id=?", (recipient,)
        ).fetchone()[0]
        == 0
    )
    await store.post(started["swarm_id"], sender, text="B", request_id="wake-b")
    assert not (
        await store.prepare_automatic_delivery(
            started["swarm_id"], recipient, expected_epoch=0, admission_boundary=4
        )
    )["wake"]
    assert (await store.claim_wake(started["swarm_id"], recipient, expected_epoch=0))["pending"]
    await store.mark_wake_admitted(
        started["swarm_id"], recipient, expected_epoch=0, run_id="wake", boundary=4
    )
    assert (
        connection.execute(
            "SELECT wake_announced_seq FROM participants WHERE id=?", (recipient,)
        ).fetchone()[0]
        == 1
    )


@pytest.mark.asyncio
async def test_reopen_reannounces_unadmitted_prepared_delivery(store: SwarmStore) -> None:
    started = await _swarm(store)
    sender, recipient = [
        item["id"] for item in (await store.get_swarm(started["swarm_id"]))["participants"]
    ]
    await store.set_participant_state(started["swarm_id"], recipient, "idle", idle_boundary=3)
    await store.post(started["swarm_id"], sender, text="A", request_id="prepared-a")
    prepared = await store.prepare_automatic_delivery(
        started["swarm_id"], recipient, expected_epoch=0, admission_boundary=3
    )
    assert prepared["wake"] and [entry["text"] for entry in prepared["entries"]] == ["A"]

    await store.begin_stop(started["swarm_id"], request_id="cancel", actor="test")
    await store.finish_stop(
        started["swarm_id"], request_id="cancel-finish", actor="test", drain_report={}
    )
    await store.begin_resume(started["swarm_id"], request_id="reopen", actor="test")
    await store.set_participant_state(started["swarm_id"], recipient, "idle", idle_boundary=9)

    replay = await store.prepare_automatic_delivery(
        started["swarm_id"], recipient, expected_epoch=1, admission_boundary=9
    )
    assert replay["replayed"] and replay["wake"]
    assert [entry["text"] for entry in replay["entries"]] == ["A"]
    claimed = await store.claim_wake(started["swarm_id"], recipient, expected_epoch=1)
    assert claimed["pending"] and claimed["boundary"] == 9
    admitted = await store.mark_wake_admitted(
        started["swarm_id"], recipient, expected_epoch=1, run_id="reopened", boundary=9
    )
    assert admitted["admitted"]


@pytest.mark.asyncio
async def test_foreign_ping_after_resume_has_exact_code(store: SwarmStore) -> None:
    started = await _swarm(store)
    sender = (await store.get_swarm(started["swarm_id"]))["participants"][0]["id"]
    await store.begin_stop(started["swarm_id"], request_id="stop-r", actor="test")
    await store.finish_stop(
        started["swarm_id"], request_id="finish-r", actor="test", drain_report={}
    )
    await store.begin_resume(started["swarm_id"], request_id="resume-r", actor="test")
    with pytest.raises(SwarmStoreError, match="invalid_recipient"):
        await store.post(
            started["swarm_id"],
            sender,
            text="ping",
            request_id="foreign-ping",
            recipients=("foreign",),
            expected_epoch=1,
        )


@pytest.mark.asyncio
async def test_status_cursor_and_board_epoch_inactive_guards(store: SwarmStore) -> None:
    started = await _swarm(store, count=2)
    swarm = await store.get_swarm(started["swarm_id"])
    first, second = [item["id"] for item in swarm["participants"]]
    page = await store.participant_status(started["swarm_id"], first, limit=1)
    assert page["self"]["id"] == first and page["has_more"] and page["cursor"]
    with pytest.raises(SwarmStoreError, match="invalid_cursor"):
        await store.participant_status(started["swarm_id"], second, cursor=page["cursor"], limit=1)
    await store.set_participant_state(started["swarm_id"], first, "failed")
    await store.create_discussion(
        started["swarm_id"], first, title="Review", text="Question", request_id="inactive"
    )
    await store.post(
        started["swarm_id"], first, text="human", request_id="human", author_kind="user"
    )


@pytest.mark.asyncio
async def test_newer_run_blocks_old_finished_callback_and_status_limit_100(
    store: SwarmStore,
) -> None:
    started = await _swarm(store, count=101)
    participant = (await store.get_swarm(started["swarm_id"]))["participants"][0]["id"]
    page = await store.participant_status(started["swarm_id"], participant, limit=100)
    assert len(page["roster"]) == 100 and page["has_more"]
    await store.record_run_started(started["swarm_id"], participant, run_id="old", expected_epoch=0)
    await store.record_run_started(started["swarm_id"], participant, run_id="new", expected_epoch=0)
    with pytest.raises(SwarmStoreError, match="stale_run"):
        await store.reconcile_run_finished(
            started["swarm_id"], participant, run_id="old", expected_epoch=0, outcome="completed"
        )
    assert (
        await store.reconcile_run_finished(
            started["swarm_id"], participant, run_id="new", expected_epoch=0, outcome="completed"
        )
    )["state"] == "idle"


@pytest.mark.asyncio
async def test_participant_lifecycle_aggregates_swarm_state(store: SwarmStore) -> None:
    started = await _swarm(store, count=2)
    swarm_id = started["swarm_id"]
    first, second = [item["id"] for item in (await store.get_swarm(swarm_id))["participants"]]
    await store.set_swarm_state(swarm_id, "running")
    await store.set_participant_state(swarm_id, first, "idle")
    await store.set_participant_state(swarm_id, second, "idle")
    assert (await store.get_swarm(swarm_id))["state"] == "idle"
    await store.set_participant_state(swarm_id, second, "failed")
    assert (await store.get_swarm(swarm_id))["state"] == "needs_attention"


@pytest.mark.asyncio
async def test_open_epoch_resume_excludes_busy_participant(store: SwarmStore) -> None:
    started = await _swarm(store, count=2)
    swarm_id = started["swarm_id"]
    waiting, busy = [item["id"] for item in (await store.get_swarm(swarm_id))["participants"]]
    await store.set_swarm_state(swarm_id, "running")
    await store.set_participant_state(swarm_id, waiting, "idle")
    await store.record_run_started(swarm_id, busy, run_id="busy", expected_epoch=0)
    resumed = await store.begin_resume(swarm_id, request_id="open", actor="test")
    assert resumed["reused_epoch"] and resumed["participant_ids"] == [waiting]
    assert (await store.get_swarm(swarm_id))["participants"][1]["lifecycle_run_id"] == "busy"


@pytest.mark.asyncio
async def test_resume_waits_for_stop_to_finish_before_opening_epoch(store: SwarmStore) -> None:
    started = await _swarm(store)
    swarm_id = started["swarm_id"]
    await store.begin_stop(swarm_id, request_id="stop", actor="test")
    with pytest.raises(SwarmStoreError, match="swarm_closed"):
        await store.begin_resume(swarm_id, request_id="too-early", actor="test")
    assert (await store.get_swarm(swarm_id))["epoch"] == 0
    await store.finish_stop(
        swarm_id, request_id="stop", actor="test", drain_report={"closed": True, "run_ids": []}
    )
    resumed = await store.begin_resume(swarm_id, request_id="resume", actor="test")
    assert resumed["epoch"] == 1


@pytest.mark.asyncio
async def test_board_read_preserves_saved_timestamp(store):
    started = await _swarm(store)
    snapshot = await store.get_swarm(started["swarm_id"])
    await store.set_swarm_state(snapshot["id"], "running")
    result = await store.post(
        snapshot["id"],
        snapshot["participants"][0]["id"],
        text="timestamp-sentinel",
        request_id="post-time",
    )
    page = await store.read_human_posts(
        snapshot["id"], discussion_id=snapshot["main_discussion_id"]
    )
    post = next(item for item in page.entries if item["id"] == result["post_id"])
    from datetime import datetime

    assert datetime.fromisoformat(post["created_at"]).utcoffset().total_seconds() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome,expected",
    [
        ("completed", "idle"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
        ("interrupted", "interrupted"),
    ],
)
async def test_participant_state_follows_run_outcome(store, outcome, expected):
    started = await _swarm(store, count=1)
    swarm_id = started["swarm_id"]
    participant = (await store.get_swarm(swarm_id))["participants"][0]["id"]
    assert (await store.participant_status(swarm_id, participant))["self"]["state"] == "idle"
    await store.set_swarm_state(swarm_id, "running")
    await store.record_run_started(swarm_id, participant, run_id="run", expected_epoch=0)
    assert (await store.participant_status(swarm_id, participant))["self"]["state"] == "running"
    await store.reconcile_run_finished(
        swarm_id, participant, run_id="run", expected_epoch=0, outcome=outcome
    )
    assert (await store.participant_status(swarm_id, participant))["self"]["state"] == expected
    assert (await store.get_swarm(swarm_id))["state"] == (
        "idle" if outcome == "completed" else "needs_attention"
    )
    await store.post_human(swarm_id, text="Follow-up", request_id="follow-up")
    assert (await store.participant_status(swarm_id, participant))["pending_count"] == 1


@pytest.mark.asyncio
async def test_stop_resume_keeps_every_participant_and_rejects_stale_callbacks(store):
    started = await _swarm(store, count=3)
    swarm_id = started["swarm_id"]
    participants = [p["id"] for p in (await store.get_swarm(swarm_id))["participants"]]
    names = {p["id"]: p["display_name"] for p in (await store.get_swarm(swarm_id))["participants"]}
    await store.set_swarm_state(swarm_id, "running")
    for peer, outcome in zip(participants, ["completed", "failed", "cancelled"], strict=True):
        await store.record_run_started(swarm_id, peer, run_id=peer, expected_epoch=0)
        await store.reconcile_run_finished(
            swarm_id, peer, run_id=peer, expected_epoch=0, outcome=outcome
        )
    stop = await store.begin_stop(swarm_id, request_id="stop", actor="user")
    assert (await store.begin_stop(swarm_id, request_id="stop", actor="user"))["replayed"]
    await store.finish_stop(
        swarm_id, request_id="stop", actor="user", drain_report={"closed": True, "run_ids": []}
    )
    resumed = await store.begin_resume(swarm_id, request_id="resume", actor="user")
    assert resumed["participant_ids"] == participants and resumed["epoch"] == stop["epoch"] + 1
    assert {
        p["id"]: p["display_name"] for p in (await store.get_swarm(swarm_id))["participants"]
    } == names
    assert (await store.begin_resume(swarm_id, request_id="resume", actor="user"))["replayed"]
    with pytest.raises(SwarmStoreError, match="stale_epoch"):
        await store.reconcile_run_finished(
            swarm_id, participants[0], run_id=participants[0], expected_epoch=0, outcome="completed"
        )
    assert {p["state"] for p in (await store.get_swarm(swarm_id))["participants"]} == {"idle"}


@pytest.mark.asyncio
async def test_no_participant_lifecycle_storage_or_summary_contract(store):
    started = await _swarm(store)
    participants = (await store.get_swarm(started["swarm_id"]))["participants"]
    assert all(
        not ({"wait_reason", "summary", "artifacts", "completion_call_id"} & p.keys())
        for p in participants
    )
    assert "done_count" not in (await store.list_swarms()).entries[0]
    assert (
        store._database._connection.execute(
            "SELECT name FROM sqlite_master WHERE name='lifecycle_intents'"
        ).fetchone()
        is None
    )
    columns = {
        r["name"] for r in store._database._connection.execute("PRAGMA table_info(participants)")
    }
    assert not {"wait_reason", "summary_json", "artifacts_json", "completion_call_id"} & columns


@pytest.mark.asyncio
async def test_completed_admission_outcomes_survive_reopen_and_later_epochs(store):
    started = await _swarm(store, count=1)
    sid = started["swarm_id"]
    snapshot = await store.get_swarm(sid)
    pid = snapshot["participants"][0]["id"]
    await store.set_swarm_state(sid, "running")
    admitted = await store.finish_admission(
        sid, request_id="start-1", kind="start", runs=[{"participant_id": pid, "run_id": "first"}]
    )
    await store.begin_stop(sid, request_id="stop", actor="user")
    stopped = await store.finish_stop(sid, request_id="stop", actor="user", drain_report={})
    await store.begin_resume(sid, request_id="resume", actor="user")
    await store.set_swarm_state(sid, "running")
    resumed = await store.finish_admission(
        sid, request_id="resume", kind="resume", runs=[{"participant_id": pid, "run_id": "second"}]
    )
    await store.begin_stop(sid, request_id="stop-2", actor="user")
    await store.finish_stop(sid, request_id="stop-2", actor="user", drain_report={})
    await store.begin_resume(sid, request_id="resume-2", actor="user")
    await store.set_swarm_state(sid, "running")
    current = await store.get_swarm(sid)
    await store.close()
    await store.open()
    profile = snapshot["profile_snapshot"]
    assert await store.create_swarm(
        profile["id"],
        "Investigate",
        {"cwd": "C:/work"},
        request_id="start-1",
        expected_profile_revision=profile["revision"],
    ) == {**admitted, "replayed": True}
    assert await store.begin_resume(sid, request_id="resume", actor="user") == {
        **resumed,
        "replayed": True,
    }
    assert await store.begin_stop(sid, request_id="stop", actor="user") == {
        **stopped,
        "replayed": True,
    }
    assert await store.get_swarm(sid) == current


@pytest.mark.asyncio
async def test_late_start_and_wake_ack_cannot_resurrect_finished_run(store):
    started = await _swarm(store, count=1)
    sid = started["swarm_id"]
    pid = (await store.get_swarm(sid))["participants"][0]["id"]
    await store.set_swarm_state(sid, "running")
    await store.bind_participant_session(
        TemporarySessionBinding(SessionAddress(None, "tmp", "ses"), "gen", "swarm", sid, pid, {})
    )
    await store.post_human(sid, text="Wake", request_id="wake")
    await store.prepare_wake(sid, pid, expected_epoch=0)
    claim = await store.claim_wake(sid, pid, expected_epoch=0)
    assert claim["pending"]
    await store.record_run_started(sid, pid, run_id="quick", expected_epoch=0)
    await store.reconcile_run_finished(
        sid, pid, run_id="quick", expected_epoch=0, outcome="completed"
    )
    assert (await store.record_run_started(sid, pid, run_id="quick", expected_epoch=0))[
        "state"
    ] == "idle"
    await store.mark_wake_admitted(
        sid, pid, expected_epoch=0, run_id="quick", boundary=claim["boundary"]
    )
    assert (await store.participant_status(sid, pid))["self"]["state"] == "idle"
    assert (
        await store.mark_wake_admitted(
            sid, pid, expected_epoch=0, run_id="quick", boundary=claim["boundary"]
        )
    )["replayed"]


@pytest.mark.asyncio
async def test_stop_preserves_idle_and_failed_outcomes(store):
    started = await _swarm(store, count=3)
    sid = started["swarm_id"]
    peers = (await store.get_swarm(sid))["participants"]
    await store.record_run_started(sid, peers[0]["id"], run_id="active", expected_epoch=0)
    await store.set_participant_state(sid, peers[2]["id"], "failed")
    await store.begin_stop(sid, request_id="stop", actor="user")
    await store.finish_stop(
        sid, request_id="stop", actor="user", drain_report={"closed": True, "run_ids": []}
    )
    assert [p["state"] for p in (await store.get_swarm(sid))["participants"]] == [
        "cancelled",
        "idle",
        "failed",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("closed", [False, True])
async def test_targeted_resume_preserves_other_participants(store, closed):
    started = await _swarm(store, count=3)
    swarm_id = started["swarm_id"]
    await store.set_swarm_state(swarm_id, "running")
    peers = (await store.get_swarm(swarm_id))["participants"]
    for peer, state in zip(peers, ["failed", "idle", "running"], strict=True):
        await store.set_participant_state(swarm_id, peer["id"], state)
    if closed:
        await store.begin_stop(swarm_id, request_id="stop", actor="user")
        await store.finish_stop(
            swarm_id, request_id="finish-stop", actor="user", drain_report={"drained": True}
        )
    before = (await store.get_swarm(swarm_id))["participants"]
    result = await store.begin_resume(
        swarm_id, request_id="target", actor="user", participant_id=peers[0]["id"]
    )
    assert result["participant_ids"] == [peers[0]["id"]]
    after = (await store.get_swarm(swarm_id))["participants"]
    assert after[1:] == before[1:]
    assert await store.begin_resume(
        swarm_id, request_id="target", actor="user", participant_id=peers[0]["id"]
    ) == {**result, "replayed": True}
    with pytest.raises(SwarmStoreError) as conflict:
        await store.begin_resume(
            swarm_id, request_id="target", actor="user", participant_id=peers[1]["id"]
        )
    assert conflict.value.code == "request_conflict"


@pytest.mark.asyncio
async def test_targeted_resume_rejects_foreign_or_active_participant(store):
    started = await _swarm(store)
    swarm_id = started["swarm_id"]
    await store.set_swarm_state(swarm_id, "running")
    peers = (await store.get_swarm(swarm_id))["participants"]
    await store.set_participant_state(swarm_id, peers[0]["id"], "running")
    for peer_id in ["foreign", peers[0]["id"]]:
        with pytest.raises(SwarmStoreError):
            await store.begin_resume(
                swarm_id, request_id=peer_id, actor="user", participant_id=peer_id
            )
