"""Queued Sub-Agent work, Parent cancellation, explicit ``cancel`` and foreground bounds."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from core.runs import ActiveRunError, RunStatus
from core.subagents._constants import (
    PARENT_AGENT_CANCEL_REASON,
    SUBAGENT_QUEUED_TIMEOUT_MESSAGE_TEMPLATE,
    SUBAGENT_REMOVED_FROM_QUEUE_MESSAGE,
    SUBAGENT_STATUS_CHANGED_EVENT,
    TOP_LEVEL_BACKGROUND_NOTE,
    TOP_LEVEL_QUEUED_BACKGROUND_NOTE,
)
from tests.core.subagents.subagents_test_support import (
    JsonObject,
    SubAgentHarness,
    activity_path_from_note,
    done,
    make_context,
)
from tests.core.subagents.subagents_test_support import (
    harness as harness,
)

pytestmark = pytest.mark.asyncio

FOLLOW_UP: JsonObject = {"content": "follow-up", "agent_id": "worker", "session_id": "busy-child"}
TOOL_CALL = {"id": "tool-call-one", "index": 0, "name": "subagent"}


def _busy_child(harness: SubAgentHarness, *, hold: bool = True) -> None:
    """Give Agent worker a Session whose active Run keeps new work in its Queue."""
    harness.sessions.create("worker", session_id="busy-child")
    harness.manager.make_busy("worker", "busy-child")
    harness.manager.hold_enqueued_starts = hold


def _recording(events: list[tuple[str, JsonObject]]) -> Any:
    return lambda event_type, payload: events.append((event_type, payload))


@pytest.mark.parametrize("admission", ["held", "admitted", "raced"])
async def test_busy_child_session_queues_the_work(
    harness: SubAgentHarness, caplog: pytest.LogCaptureFixture, admission: str
) -> None:
    caplog.set_level(logging.INFO, logger="vbot.subagents")
    if admission == "raced":
        # The Session became busy between the idle check and the start.
        harness.sessions.create("worker", session_id="busy-child")
        harness.manager.start_error = ActiveRunError("session already has an active run")
    else:
        _busy_child(harness, hold=admission == "held")

    result = await harness.spawn(FOLLOW_UP)

    [child] = await harness.enqueued()
    assert harness.manager.started == []
    assert child.display_content == "follow-up"
    [spawned] = [record for record in caplog.records if record.name == "vbot.subagents"]
    assert "child_session=busy-child" in spawned.getMessage()
    assert f"queue_item={child.item.item_id}" in spawned.getMessage()
    assert await child.task() == "follow-up"
    activity_note = result.pop("activity_note")
    assert Path(activity_path_from_note(activity_note)).exists()
    queued = admission == "held"
    assert result == {
        "id": child.admission.work_id,
        "agent_id": "worker",
        "session_id": "busy-child",
        "status": "queued" if queued else "running",
        "delivery": "automatic",
        "note": TOP_LEVEL_QUEUED_BACKGROUND_NOTE if queued else TOP_LEVEL_BACKGROUND_NOTE,
    }


async def test_queued_work_counts_against_the_per_turn_limit(harness: SubAgentHarness) -> None:
    harness.storage.settings = {"max_subagents_per_turn": 1}
    _busy_child(harness)

    first = await harness.spawn(FOLLOW_UP)
    second = await harness.call({**FOLLOW_UP, "content": "follow-up again"})

    assert first["status"] == "queued"
    assert second["error"]["code"] == "subagent_limit_exceeded"
    assert len(harness.manager.enqueued) == 1


async def test_queued_runs_keep_their_own_overrides(harness: SubAgentHarness) -> None:
    _busy_child(harness)

    await harness.spawn({**FOLLOW_UP, "content": "first", "model": "openai/gpt-mini"})
    await harness.spawn({**FOLLOW_UP, "content": "second", "thinking_effort": "high"})

    first, second = harness.loop.tasks["first"].overrides, harness.loop.tasks["second"].overrides
    assert (first.model, first.thinking_effort) == ("openai/gpt-mini", None)
    assert (second.model, second.thinking_effort) == (None, "high")


async def test_foreground_call_waits_for_its_queued_run_to_finish(
    harness: SubAgentHarness,
) -> None:
    harness.manager.next_result = done("queued finished", usage={"input_tokens": 2})
    _busy_child(harness, hold=False)

    result = await harness.call(FOLLOW_UP, nesting_depth=1)

    assert result["ok"] is True, result
    assert result["data"]["status"] == "completed"
    assert result["data"]["result"] == "queued finished"
    assert result["data"]["usage"] == {"input_tokens": 2}
    assert harness.manager.started == []


async def test_parent_run_unknown_to_the_run_manager_still_gets_its_result(
    harness: SubAgentHarness,
) -> None:
    # Nothing can cascade from a Parent Run the manager does not track.
    call = harness.call_in_background(
        {"content": "do work"}, nesting_depth=1, run_id="untracked-parent-run"
    )
    [child] = await harness.started()
    child.run.mark_completed(done("child output"))

    result = await call

    assert (result["data"]["status"], result["data"]["result"]) == ("completed", "child output")


async def test_parent_cancellation_removes_its_queued_foreground_child(
    harness: SubAgentHarness,
) -> None:
    _busy_child(harness)
    call = harness.call_in_background(FOLLOW_UP, nesting_depth=1)
    await harness.enqueued()
    await harness.settle()

    harness.manager.parent_run.request_cancel()

    with pytest.raises(asyncio.CancelledError):
        await call
    assert harness.manager.list_queued("worker", "busy-child") == []
    assert harness.owned_work() == []


async def test_removing_a_queued_foreground_child_is_a_tool_outcome(
    harness: SubAgentHarness,
) -> None:
    _busy_child(harness)
    call = harness.call_in_background(FOLLOW_UP, nesting_depth=1)
    [child] = await harness.enqueued()
    await harness.settle()

    # The user removes the item through the Session's Queue, not the Parent.
    assert harness.manager.remove_queued("worker", "busy-child", child.item.item_id)

    result = await call
    assert result["error"] == {
        "code": "subagent_removed",
        "message": SUBAGENT_REMOVED_FROM_QUEUE_MESSAGE,
    }
    assert harness.manager.parent_run.cancel_requested is False
    assert harness.owned_work() == []


@pytest.mark.parametrize(
    ("nesting_depth", "abort", "starts_during_event"),
    [
        (0, asyncio.CancelledError, False),
        (0, RuntimeError, True),
        (1, asyncio.CancelledError, True),
        (1, RuntimeError, False),
    ],
)
async def test_abort_while_announcing_queued_work_keeps_its_lifecycle(
    harness: SubAgentHarness,
    nesting_depth: int,
    abort: type[BaseException],
    starts_during_event: bool,
) -> None:
    _busy_child(harness)
    announced: dict[str, Any] = {}

    async def abort_on_queued_event(_event_type: str, payload: JsonObject) -> None:
        data = payload["data"]
        if data.get("queue_item_id") is None:
            return
        announced["id"] = data["id"]
        if starts_during_event:
            announced["run"] = harness.manager.release_next_enqueued_start()
        if abort is asyncio.CancelledError:
            harness.manager.parent_run.request_cancel(reason="user")
        raise abort()

    context = make_context(nesting_depth=nesting_depth, emit_hook=abort_on_queued_event)

    with pytest.raises(abort):
        await harness.call(FOLLOW_UP, context)

    if nesting_depth and not starts_during_event:
        # A foreground call that ends before its child starts takes the child with it.
        assert harness.manager.list_queued("worker", "busy-child") == []
        assert harness.owned_work() == []
        return
    # Admitted work stays owned and is delivered like any other child.
    assert harness.owned_work() == [announced["id"]]
    child = announced.get("run") or harness.manager.release_next_enqueued_start()
    if nesting_depth:
        assert child.cancel_requested
        child.mark_cancelled()
    else:
        assert not child.cancel_requested
        child.mark_completed(done("Finished"))
    await harness.settle()
    assert [notice.notice_id for notice in harness.triggers.notices] == [
        f"subagent:parent-run:{announced['id']}"
    ]


async def test_parent_cancel_during_the_spawn_window_still_cascades_and_tracks(
    harness: SubAgentHarness,
) -> None:
    # The child is live while the call announces it; a Parent cancel landing in
    # that window must still reach the child and keep it tracked.
    announced: dict[str, Any] = {}

    def cancel_on_running_event(_event_type: str, payload: JsonObject) -> None:
        if payload["data"].get("run_id") is None:
            return
        announced["id"] = payload["data"]["id"]
        harness.manager.parent_run.request_cancel(reason="user")
        harness.manager.started[0].run.mark_completed(done("child output"))

    result = await harness.call(
        {"content": "spawn", "agent_id": "worker"},
        make_context(nesting_depth=1, emit_hook=cancel_on_running_event),
    )

    assert result["ok"] is True
    assert announced["id"] == result["data"]["id"]
    assert harness.manager.started[0].run.cancel_requested is True
    # Tracked work is delivered to the Parent Session as well.
    assert [notice.notice_id for notice in harness.triggers.notices] == [
        f"subagent:parent-run:{announced['id']}"
    ]


async def test_background_child_survives_parent_cancellation_until_cancelled_by_id(
    harness: SubAgentHarness,
) -> None:
    spawned = await harness.spawn({"content": "keep working"})
    [child] = await harness.started()

    harness.manager.parent_run.request_cancel()
    await harness.settle()

    assert child.run.status is RunStatus.RUNNING
    assert child.run.cancel_requested is False
    events: list[tuple[str, JsonObject]] = []
    later = make_context(run_id="parent-run-two", emit_hook=_recording(events))

    cancelled = await harness.call({"action": "cancel", "id": spawned["id"]}, later)

    assert cancelled["ok"] is True
    data = dict(cancelled["data"])
    note = data.pop("note")
    assert data == {
        "id": spawned["id"],
        "agent_id": "parent",
        "session_id": spawned["session_id"],
        "status": "cancelled",
    }
    assert "`parent`" in note and f"`{spawned['session_id']}`" in note
    assert child.run.status is RunStatus.CANCELLED
    assert child.run.cancel_reason == PARENT_AGENT_CANCEL_REASON
    assert events == [
        (
            SUBAGENT_STATUS_CHANGED_EVENT,
            {"tool_call": TOOL_CALL, "data": {**data, "run_id": child.run.id}},
        )
    ]


async def test_cancelled_project_child_names_its_address_and_resume_note(
    harness: SubAgentHarness,
) -> None:
    spawned = await harness.spawn(
        {"content": "keep working", "agent_id": "worker"}, project_id="vbot"
    )
    events: list[tuple[str, JsonObject]] = []

    cancelled = await harness.call(
        {"action": "cancel", "id": spawned["id"]},
        make_context(run_id="parent-run-two", project_id="vbot", emit_hook=_recording(events)),
    )
    await harness.settle()

    # The Agent receives the address it passes back; the event keeps bare ids.
    assert (cancelled["data"]["agent_id"], cancelled["data"]["project_id"]) == (
        "worker@vbot",
        "vbot",
    )
    event_data = events[-1][1]["data"]
    assert (event_data["agent_id"], event_data["project_id"]) == ("worker", "vbot")
    note = cancelled["data"]["note"]
    assert "`worker@vbot`" in note and f"`{spawned['session_id']}`" in note
    [notice] = harness.triggers.notices
    assert note in notice.body


async def test_cancel_removes_the_exact_queued_child(harness: SubAgentHarness) -> None:
    _busy_child(harness)
    spawned = await harness.spawn(FOLLOW_UP)
    events: list[tuple[str, JsonObject]] = []

    result = await harness.call(
        {"action": "cancel", "id": spawned["id"]},
        make_context(run_id="parent-run-two", emit_hook=_recording(events)),
    )

    # Removed Queue work never ran, so there is no Session history to resume.
    assert result["data"] == {
        "id": spawned["id"],
        "agent_id": "worker",
        "session_id": "busy-child",
        "status": "cancelled",
    }
    assert harness.manager.list_queued("worker", "busy-child") == []
    assert [event_type for event_type, _ in events] == [SUBAGENT_STATUS_CHANGED_EVENT]
    await harness.settle()


@pytest.mark.parametrize("watcher_ran", [True, False])
async def test_cancel_stops_queued_work_that_has_started(
    harness: SubAgentHarness, watcher_ran: bool
) -> None:
    _busy_child(harness)
    spawned = await harness.spawn(FOLLOW_UP)
    started = harness.manager.release_next_enqueued_start()
    if watcher_ran:
        await harness.settle()
    # Otherwise the cancel resolves the admitted child before its completion
    # watcher has recorded the start.

    result = await harness.call({"action": "cancel", "id": spawned["id"]}, run_id="parent-run-two")

    assert result["ok"] is True
    assert result["data"]["id"] == spawned["id"]
    assert "queue_item_id" not in result["data"] and "run_id" not in result["data"]
    assert started.status is RunStatus.CANCELLED


async def test_cancel_refuses_when_another_run_holds_the_childs_session(
    harness: SubAgentHarness,
) -> None:
    _busy_child(harness)
    spawned = await harness.spawn(FOLLOW_UP)
    released = harness.manager.release_next_enqueued_start()
    other = harness.manager.make_busy("worker", "busy-child", work_id="other-work")

    result = await harness.call({"action": "cancel", "id": spawned["id"]})

    assert result["error"] == {
        "code": "subagent_not_running",
        "message": f"Sub-Agent work is no longer queued and has no active Run: {spawned['id']}",
    }
    assert not released.cancel_requested and not other.cancel_requested


async def test_cancel_without_id_names_the_tracked_work(harness: SubAgentHarness) -> None:
    spawned = await harness.spawn({"content": "keep working", "agent_id": "worker"})

    result = await harness.call({"action": "cancel", "id": ""})

    assert result["error"] == {
        "code": "invalid_arguments",
        "message": (
            'cancel needs "id", the work to stop; nothing was cancelled. Tracked work: '
            f"{spawned['id']} (agent_id worker, session_id {spawned['session_id']}, running). "
            f'Call {{"action": "cancel", "id": "{spawned["id"]}"}}.'
        ),
    }
    assert harness.manager.started[0].run.status is RunStatus.RUNNING


async def test_foreground_timeout_cancels_the_child_and_delivers_after_persistence(
    harness: SubAgentHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("core.subagents.subagents.SECONDS_PER_MINUTE", 0)
    harness.storage.settings = {"subagent_timeout_minutes": 1}
    harness.triggers.defer_persistence = True
    persisted: list[Any] = []

    result = await harness.call(
        {"content": "do work"},
        make_context(nesting_depth=1, result_persisted_hook=persisted.append),
    )

    assert result["error"]["code"] == "subagent_timeout"
    [child] = harness.manager.started
    assert result["error"]["message"].startswith(
        "The Sub-Agent did not finish within 1 minutes, so vBot cancelled it."
    )
    assert f"session_id `{child.run.session_id}`" in result["error"]["message"]
    assert child.run.cancel_requested is True
    await harness.settle()
    [notice] = harness.triggers.notices
    assert "Sub-agent run timed out after 1 minutes" in notice.body
    assert harness.owned_work() == [child.admission.work_id]

    persisted[0]()

    assert harness.triggers.cancelled_notice_ids == [notice.notice_id]
    assert harness.owned_work() == []


async def test_queued_foreground_wait_shares_the_timeout(
    harness: SubAgentHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("core.subagents.subagents.SECONDS_PER_MINUTE", 0)
    harness.storage.settings = {"subagent_timeout_minutes": 1}
    _busy_child(harness)

    result = await harness.call(FOLLOW_UP, nesting_depth=1)

    assert result["error"] == {
        "code": "subagent_timeout",
        "message": SUBAGENT_QUEUED_TIMEOUT_MESSAGE_TEMPLATE.format(minutes=1),
    }
    assert harness.manager.list_queued("worker", "busy-child") == []
    assert harness.owned_work() == []
    assert harness.manager.parent_run.cancel_requested is False
