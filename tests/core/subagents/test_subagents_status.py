"""The ``status`` action and Sub-Agent work inspection.

A snapshot reports live, queued and finished work; finished work falls back to
the child Session's stored history when the live Run is gone or has no output.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

import core.subagents._completion as subagent_completion
from core.chat import ChatMessage
from core.runs import Run
from core.sessions import ChatSession
from core.subagents._constants import (
    SESSION_RESULT_RETRY_DELAY_SECONDS,
    SUBAGENT_NOT_FOUND_CANCEL_MESSAGE_TEMPLATE,
    SUBAGENT_NOT_FOUND_STATUS_MESSAGE_TEMPLATE,
    SUBAGENT_STATUS_LIST_NOTE,
    SUBAGENT_STATUS_QUEUED_NOTE,
    SUBAGENT_STATUS_RUNNING_NOTE,
    SUBAGENT_USER_CANCEL_MESSAGE,
)
from tests.core.sessions.history_fixtures import complete_run
from tests.core.subagents.subagents_test_support import (
    JsonObject,
    SubAgentHarness,
    activity_path_from_note,
    address,
    done,
    make_context,
)
from tests.core.subagents.subagents_test_support import (
    harness as harness,
)

pytestmark = pytest.mark.asyncio

DELAY = SESSION_RESULT_RETRY_DELAY_SECONDS
TIMING = {
    "started_at": "2026-07-24T10:00:00+00:00",
    "completed_at": "2026-07-24T10:00:01+00:00",
    "duration_ms": 1000,
}
NO_SUMMARY_NOTE = "No terminal Run summary found in sub-agent session."


@pytest.fixture(autouse=True)
def session_polls(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the waits between history polls instead of sleeping."""
    sleeps: list[float] = []

    async def record(delay_seconds: float) -> None:
        sleeps.append(delay_seconds)

    monkeypatch.setattr(subagent_completion, "_sleep", record)
    return sleeps


def _history(
    harness: SubAgentHarness,
    session_id: str,
    run_id: str,
    *messages: ChatMessage,
    status: str | None = None,
    work_id: str | None = None,
    project_id: str | None = None,
    agent_id: str = "worker",
    timing: JsonObject = TIMING,
) -> None:
    """Store one Run's messages in a child Session; ``status`` also finishes it."""
    session = harness.sessions.get(address(agent_id, session_id, project_id)).start_run(run_id)
    for message in messages:
        session.append(message)
    if status is not None:
        summary_fields: JsonObject = {"work_id": work_id} if work_id else {}
        complete_run(
            session,
            ChatMessage.run_summary(
                run_id=run_id, status=status, timing=timing, iteration_count=1, **summary_fields
            ),
        )


def _activity_file(spawned: JsonObject) -> str:
    return activity_path_from_note(spawned["activity_note"])


async def _spawn_worker(harness: SubAgentHarness, **context: Any) -> tuple[JsonObject, Run]:
    spawned = await harness.spawn({"content": "do work", "agent_id": "worker"}, **context)
    return spawned, harness.manager.started[-1].run


async def test_running_work_reports_a_snapshot_to_a_later_parent_run(
    harness: SubAgentHarness,
) -> None:
    spawned, run = await _spawn_worker(harness)

    status = await harness.call({"action": "status", "id": spawned["id"]}, run_id="later-run")

    assert status["data"] == {
        "id": spawned["id"],
        "agent_id": "worker",
        "session_id": spawned["session_id"],
        "status": "running",
        "result": None,
        "usage": None,
        "activity_file": _activity_file(spawned),
        "note": SUBAGENT_STATUS_RUNNING_NOTE,
    }
    # A snapshot does not consume the result: it is still delivered.
    assert harness.owned(spawned["id"]).fetched is False
    run.mark_completed(done("finished work"))
    await harness.settle()
    [notice] = harness.triggers.notices
    assert notice.body.endswith("\nfinished work")


async def test_queued_project_work_reports_its_qualified_address(
    harness: SubAgentHarness,
) -> None:
    harness.sessions.create("worker", session_id="busy-child", project_id="vbot")
    harness.manager.make_busy("worker", "busy-child", project_id="vbot")
    harness.manager.hold_enqueued_starts = True
    spawned = await harness.spawn(
        {"content": "do work", "agent_id": "worker@vbot", "session_id": "busy-child"}
    )

    status = await harness.call({"action": "status", "id": spawned["id"]}, run_id="later-run")

    [child] = harness.manager.enqueued
    assert child.run.project_id == "vbot"
    assert status["data"] == {
        "id": spawned["id"],
        "agent_id": "worker@vbot",
        "project_id": "vbot",
        "session_id": "busy-child",
        "status": "queued",
        "activity_file": _activity_file(spawned),
        "note": SUBAGENT_STATUS_QUEUED_NOTE,
    }
    harness.manager.release_next_enqueued_start().mark_completed(done("finished work"))
    await harness.settle()
    assert [notice.notice_id for notice in harness.triggers.notices] == [
        f"subagent:parent-run:{spawned['id']}"
    ]


@pytest.mark.parametrize("caller_project", [None, "parent-project"])
async def test_status_lists_this_sessions_work_across_its_runs(
    harness: SubAgentHarness, caller_project: str | None
) -> None:
    harness.use_agents({"parent", "worker", "other"})
    harness.sessions.create("worker", session_id="busy-child", project_id=caller_project)
    harness.manager.make_busy("worker", "busy-child", project_id=caller_project)
    harness.manager.hold_enqueued_starts = True
    running, _ = await _spawn_worker(harness, run_id="earlier-run", project_id=caller_project)
    queued = await harness.spawn(
        {"content": "do work", "agent_id": "worker", "session_id": "busy-child"},
        project_id=caller_project,
    )
    # Work of another Agent, another Session or another scope is not listed.
    await _spawn_worker(harness, agent_id="other", project_id=caller_project)
    await _spawn_worker(harness, session_id="other-session", project_id=caller_project)
    await _spawn_worker(harness, run_id="other-project-run", project_id="other-project")

    listing = await harness.call({"action": "status"}, project_id=caller_project)

    snapshots = listing["data"]["subagents"]
    assert [(item["id"], item["status"]) for item in snapshots] == [
        (running["id"], "running"),
        (queued["id"], "queued"),
    ]
    assert listing["data"]["note"] == SUBAGENT_STATUS_LIST_NOTE
    for item in snapshots:
        single = await harness.call(
            {"action": "status", "id": item["id"]}, project_id=caller_project
        )
        # A single snapshot carries its own waiting note; the list states it once.
        assert single["data"].pop("note") in {
            SUBAGENT_STATUS_RUNNING_NOTE,
            SUBAGENT_STATUS_QUEUED_NOTE,
        }
        assert item == single["data"]
        assert "run_id" not in item and "queue_item_id" not in item


async def test_status_list_keeps_other_snapshots_when_one_lookup_fails(
    harness: SubAgentHarness,
) -> None:
    broken, broken_run = await _spawn_worker(harness)
    harness.sessions.create("worker", session_id="busy-child")
    harness.manager.make_busy("worker", "busy-child")
    harness.manager.hold_enqueued_starts = True
    queued = await harness.spawn(
        {"content": "do work", "agent_id": "worker", "session_id": "busy-child"}
    )
    # Defensive: the manager returns a Run of another Session for tracked work.
    harness.manager.runs[broken_run.id] = Run(
        run_id=broken_run.id, agent_id="other", session_id="other-session"
    )

    listing = await harness.call({"action": "status"})

    harness.manager.runs[broken_run.id] = broken_run
    failed, waiting = listing["data"]["subagents"]
    assert failed == {
        "id": broken["id"],
        "error": {
            "code": "run_not_found",
            "message": f"Sub-Agent work resolved to the wrong Session: {broken['id']}",
        },
    }
    assert (waiting["id"], waiting["status"]) == (queued["id"], "queued")
    assert "other-session" not in str(listing)


def _user_cancel(run: Run) -> None:
    run.request_cancel(reason="user")
    run.mark_cancelled()


def _partial(run: Run) -> None:
    run.mark_completed(
        done("I am about to write the plan.", interrupted=True, interruption_cause="timeout")
    )


@pytest.mark.parametrize(
    ("caller_project", "target", "finish", "expected", "continue_with"),
    [
        (
            None,
            "worker",
            _user_cancel,
            {
                "status": "cancelled",
                "cancelled_by_user": True,
                "result": SUBAGENT_USER_CANCEL_MESSAGE,
            },
            None,
        ),
        (
            None,
            "worker",
            _partial,
            {"status": "completed", "interrupted": True, "interruption_cause": "timeout"},
            "worker",
        ),
        # The note names the address the Tool accepts, whatever the caller's scope.
        (
            None,
            "builder@vbot",
            _partial,
            {"agent_id": "builder@vbot", "project_id": "vbot", "interrupted": True},
            "builder@vbot",
        ),
        (
            "vbot",
            "builder",
            _partial,
            {"agent_id": "builder@vbot", "project_id": "vbot", "interrupted": True},
            "builder@vbot",
        ),
    ],
)
async def test_status_reports_how_finished_live_work_ended(
    harness: SubAgentHarness,
    caller_project: str | None,
    target: str,
    finish: Any,
    expected: JsonObject,
    continue_with: str | None,
) -> None:
    harness.use_agents({"parent", "worker", "builder"})
    harness.triggers.defer_persistence = True
    spawned = await harness.spawn(
        {"content": "do work", "agent_id": target}, project_id=caller_project
    )
    finish(harness.manager.started[0].run)
    await harness.settle()

    status = await harness.call(
        {"action": "status", "id": spawned["id"]}, project_id=caller_project
    )

    data = status["data"]
    assert {key: data.get(key) for key in expected} == expected
    if continue_with is None:
        assert "note" not in data
    else:
        assert f"`{continue_with}`" in data["note"]
        assert f"`{spawned['session_id']}`" in data["note"]


async def test_released_run_answers_from_session_history(harness: SubAgentHarness) -> None:
    spawned, run = await _spawn_worker(harness)
    _history(
        harness,
        spawned["session_id"],
        run.id,
        ChatMessage.user("question"),
        ChatMessage.assistant(model="fixture", content="first"),
        ChatMessage.assistant(
            model="fixture", content="final answer", usage={"input_tokens": 3, "output_tokens": 5}
        ),
        status="completed",
    )
    harness.manager.forget(run.id)

    status = await harness.call({"action": "status", "id": spawned["id"]})

    assert status["data"] == {
        "id": spawned["id"],
        "agent_id": "worker",
        "session_id": spawned["session_id"],
        "status": "completed",
        "result": "final answer",
        "usage": {"input_tokens": 3, "output_tokens": 5},
        "activity_file": _activity_file(spawned),
    }


async def test_session_history_keeps_interruption_details(harness: SubAgentHarness) -> None:
    spawned, run = await _spawn_worker(harness)
    _history(
        harness,
        spawned["session_id"],
        run.id,
        ChatMessage.user("write the plan"),
        done("I am about to write the plan.", interrupted=True, interruption_cause="timeout"),
        status="completed",
    )
    harness.manager.forget(run.id)

    status = await harness.call({"action": "status", "id": spawned["id"]})

    assert status["data"]["interrupted"] is True
    assert status["data"]["interruption_cause"] == "timeout"


@pytest.mark.parametrize(
    ("finish", "stored_status", "expected"),
    [
        (
            lambda run: run.mark_completed(None),
            "completed",
            {
                "status": "completed",
                "result": "stored answer",
                "usage": {"input_tokens": 7, "output_tokens": 11},
            },
        ),
        (
            lambda run: run.mark_failed(RuntimeError("provider failed after persistence")),
            "failed",
            {"status": "failed", "result": "stored answer", "usage": None},
        ),
    ],
)
async def test_live_run_without_output_answers_from_session_history(
    harness: SubAgentHarness, finish: Any, stored_status: str, expected: JsonObject
) -> None:
    harness.triggers.defer_persistence = True
    spawned, run = await _spawn_worker(harness)
    usage = expected["usage"]
    _history(
        harness,
        spawned["session_id"],
        run.id,
        ChatMessage.user("question"),
        ChatMessage.assistant(model="fixture", content="stored answer", usage=usage),
        status=stored_status,
    )
    finish(run)
    await harness.settle()

    status = await harness.call({"action": "status", "id": spawned["id"]})

    assert status["data"] == {
        "id": spawned["id"],
        "agent_id": "worker",
        "session_id": spawned["session_id"],
        "activity_file": _activity_file(spawned),
        **expected,
    }


async def test_project_history_is_read_off_the_event_loop(
    harness: SubAgentHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned = await harness.spawn({"content": "do work", "agent_id": "worker@vbot"})
    run = harness.manager.started[0].run
    _history(
        harness,
        spawned["session_id"],
        run.id,
        ChatMessage.assistant(model="fixture", content="project result"),
        status="completed",
        project_id="vbot",
    )
    harness.manager.forget(run.id)
    read_threads: list[int] = []
    load_run_result = ChatSession.load_run_result

    def recording_load_run_result(self: ChatSession, **kwargs: Any) -> Any:
        read_threads.append(threading.get_ident())
        return load_run_result(self, **kwargs)

    monkeypatch.setattr(ChatSession, "load_run_result", recording_load_run_result)

    status = await harness.call({"action": "status", "id": spawned["id"]})

    assert status["data"]["project_id"] == "vbot"
    assert status["data"]["result"] == "project result"
    assert read_threads and threading.get_ident() not in read_threads


@pytest.mark.parametrize("history", ["none", "intermediate output", "earlier finished run"])
async def test_history_without_this_runs_summary_reports_a_failure_after_bounded_polls(
    harness: SubAgentHarness, session_polls: list[float], history: str
) -> None:
    session_id = None
    if history == "earlier finished run":
        session_id = harness.sessions.create("worker").id
        _history(
            harness,
            session_id,
            "first-run",
            ChatMessage.user("first question"),
            ChatMessage.assistant(model="fixture", content="First answer."),
            status="completed",
        )
    arguments: JsonObject = {"content": "do work", "agent_id": "worker"}
    if session_id is not None:
        arguments["session_id"] = session_id
    spawned = await harness.spawn(arguments)
    run = harness.manager.started[0].run
    if history != "none":
        _history(
            harness,
            spawned["session_id"],
            run.id,
            ChatMessage.user("continue"),
            ChatMessage.assistant(model="fixture", content="Still working."),
        )
    harness.manager.forget(run.id)

    status = await harness.call({"action": "status", "id": spawned["id"]})

    assert status["data"] == {
        "id": spawned["id"],
        "agent_id": "worker",
        "session_id": spawned["session_id"],
        "status": "failed",
        "result": None,
        "usage": None,
        "activity_file": _activity_file(spawned),
        "note": NO_SUMMARY_NOTE,
    }
    assert session_polls == [DELAY, DELAY]


async def test_deleted_child_session_reports_why_no_result_was_read(
    harness: SubAgentHarness, session_polls: list[float]
) -> None:
    spawned, run = await _spawn_worker(harness)
    harness.manager.forget(run.id)
    harness.sessions.delete(address("worker", spawned["session_id"]))

    status = await harness.call({"action": "status", "id": spawned["id"]})

    data = status["data"]
    assert (data["status"], data["result"]) == ("failed", None)
    assert data["note"] == f"session does not exist: {spawned['session_id']}"
    assert session_polls == [DELAY, DELAY]


async def test_status_polls_until_the_stored_result_appears(
    harness: SubAgentHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.triggers.defer_persistence = True
    spawned, run = await _spawn_worker(harness)
    run.mark_failed(RuntimeError("provider failed after persistence"))
    await harness.settle()
    sleeps: list[float] = []

    async def store_after_first_poll(delay_seconds: float) -> None:
        sleeps.append(delay_seconds)
        _history(
            harness,
            spawned["session_id"],
            run.id,
            ChatMessage.assistant(model="fixture", content="late answer"),
            status="failed",
        )

    monkeypatch.setattr(subagent_completion, "_sleep", store_after_first_poll)

    status = await harness.call({"action": "status", "id": spawned["id"]})

    assert (status["data"]["status"], status["data"]["result"]) == ("failed", "late answer")
    assert sleeps == [DELAY]


async def test_fetching_one_run_of_a_reused_session_consumes_only_that_result(
    harness: SubAgentHarness,
) -> None:
    harness.triggers.defer_persistence = True
    harness.sessions.create("worker", session_id="shared-session")
    arguments = {"agent_id": "worker", "session_id": "shared-session"}
    old = await harness.spawn({"content": "old task", **arguments})
    harness.manager.started[0].run.mark_completed(done("old answer"))
    await harness.settle()
    new = await harness.spawn({"content": "new task", **arguments})
    persisted: list[Any] = []

    fetched = await harness.call(
        {"action": "status", "id": old["id"]},
        make_context(result_persisted_hook=persisted.append),
    )

    assert fetched["data"]["result"] == "old answer"
    assert (harness.owned(old["id"]).fetched, harness.owned(new["id"]).fetched) == (False, False)
    persisted[0]()
    assert (harness.owned(old["id"]).fetched, harness.owned(new["id"]).fetched) == (True, False)
    old_notice = f"subagent:parent-run:{old['id']}"
    assert harness.triggers.cancelled_notice_ids == [old_notice]

    harness.manager.started[1].run.mark_completed(done("new answer"))
    await harness.settle()
    harness.triggers.persist()

    new_notice = harness.triggers.notices[-1]
    assert [notice.notice_id for notice in harness.triggers.notices] == [
        old_notice,
        f"subagent:parent-run:{new['id']}",
    ]
    assert new_notice.body.splitlines()[0] == (
        f"### Sub-Agent worker (id {new['id']}, session shared-session) — completed"
    )
    assert "new answer" in new_notice.body and "old answer" not in new_notice.body
    assert harness.owned_work() == []


@pytest.mark.parametrize("listing", [False, True])
async def test_status_result_stays_owned_and_unread_until_the_parent_stores_it(
    harness: SubAgentHarness, listing: bool
) -> None:
    harness.triggers.defer_persistence = True
    spawned, run = await _spawn_worker(harness)
    _history(
        harness,
        spawned["session_id"],
        run.id,
        ChatMessage.assistant(model="fixture", content="child output"),
        status="completed",
    )
    run.mark_completed(done("child output"))
    await harness.settle()
    persisted: list[Any] = []
    context = make_context(result_persisted_hook=persisted.append)

    result = await harness.call(
        {"action": "status"} if listing else {"action": "status", "id": spawned["id"]}, context
    )

    snapshot = result["data"]["subagents"][0] if listing else result["data"]
    assert (snapshot["id"], snapshot["result"]) == (spawned["id"], "child output")
    assert "run_id" not in snapshot
    assert len(persisted) == 1
    harness.manager.parent_run.request_cancel(reason="user")
    notice_id = f"subagent:parent-run:{spawned['id']}"
    assert harness.owned(spawned["id"]) is not None
    assert notice_id in harness.triggers.deliveries
    assert harness.unread() is True

    persisted[0]()

    assert harness.owned(spawned["id"]) is None
    assert harness.triggers.cancelled_notice_ids == [notice_id]
    assert harness.unread() is False
    if listing:
        empty = await harness.call({"action": "status"}, context)
        assert empty["data"] == {"subagents": []}


async def test_inspect_reads_exact_stored_work_after_session_reuse(
    harness: SubAgentHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.sessions.create("worker", session_id="reused-child")
    _history(
        harness,
        "reused-child",
        "old-run",
        ChatMessage.user("old request"),
        ChatMessage.assistant(model="fixture", content="old result"),
        status="completed",
        work_id="sub_old",
    )
    _history(
        harness,
        "reused-child",
        "new-run",
        ChatMessage.user("new request"),
        ChatMessage.assistant(model="fixture", content="new result"),
        status="completed",
        work_id="sub_new",
        timing={
            **TIMING,
            "started_at": "2026-07-24T11:00:00+00:00",
            "completed_at": "2026-07-24T11:00:01+00:00",
        },
    )

    def fail_full_load(self: ChatSession) -> list[ChatMessage]:
        raise AssertionError("Sub-Agent inspection must use the terminal Run projection")

    monkeypatch.setattr(ChatSession, "load", fail_full_load)
    read_threads: list[int] = []
    load_run_result = ChatSession.load_run_result

    def recording_load_run_result(self: ChatSession, **kwargs: Any) -> Any:
        read_threads.append(threading.get_ident())
        return load_run_result(self, **kwargs)

    monkeypatch.setattr(ChatSession, "load_run_result", recording_load_run_result)

    result = await harness.coordinator.inspect("worker", "reused-child", "sub_old")

    # The durable result read runs on a Session worker, never on the Event Loop.
    assert read_threads and threading.get_ident() not in read_threads
    assert result is not None
    assert (result["id"], result["run_id"], result["status"], result["result"]) == (
        "sub_old",
        "old-run",
        "completed",
        "old result",
    )
    # Stored timing values come back in canonical UTC form.
    assert result["timing"] == {
        "started_at": "2026-07-24T10:00:00.000000Z",
        "completed_at": "2026-07-24T10:00:01.000000Z",
        "duration_ms": 1000,
    }


async def test_inspect_prefers_matching_live_work(harness: SubAgentHarness) -> None:
    harness.sessions.create("worker", session_id="live-child")
    active = harness.manager.make_busy("worker", "live-child", work_id="sub_live")

    result = await harness.coordinator.inspect("worker", "live-child", "sub_live")

    assert result is not None
    assert (result["id"], result["run_id"], result["status"], result["result"]) == (
        "sub_live",
        active.id,
        "running",
        None,
    )
    assert result["started_at"] == active.created_at


@pytest.mark.parametrize("action", ["status", "cancel"])
async def test_unknown_id_lists_the_tracked_work(harness: SubAgentHarness, action: str) -> None:
    spawned, _ = await _spawn_worker(harness)

    result = await harness.call({"action": action, "id": "sub_unknown"})

    assert result["error"] == {
        "code": "subagent_not_found",
        "message": (
            "No Sub-Agent work with id sub_unknown is tracked for this Session; work stops "
            "being tracked once its result was delivered to you, or when vBot restarts. "
            f"Tracked work: {spawned['id']} (agent_id worker, session_id "
            f"{spawned['session_id']}, running)."
        ),
    }


@pytest.mark.parametrize(
    ("action", "template"),
    [
        ("status", SUBAGENT_NOT_FOUND_STATUS_MESSAGE_TEMPLATE),
        ("cancel", SUBAGENT_NOT_FOUND_CANCEL_MESSAGE_TEMPLATE),
    ],
)
async def test_another_sessions_work_is_not_found(
    harness: SubAgentHarness, action: str, template: str
) -> None:
    spawned, run = await _spawn_worker(harness)

    result = await harness.call(
        {"action": action, "id": spawned["id"]},
        session_id="different-parent-session",
        run_id="different-parent-run",
    )

    assert result["error"] == {
        "code": "subagent_not_found",
        "message": template.format(work_id=spawned["id"]),
    }
    assert not run.cancel_requested


async def test_delivered_work_is_no_longer_tracked(harness: SubAgentHarness) -> None:
    persisted: list[Any] = []
    call = harness.call_in_background(
        {"content": "spawn", "agent_id": "worker"},
        make_context(nesting_depth=1, result_persisted_hook=persisted.append),
    )
    (await harness.started())[0].run.mark_completed(done("child output"))
    delivered = await call
    work_id = delivered["data"]["id"]
    persisted[0]()

    status = await harness.call({"action": "status", "id": work_id}, run_id="parent-run-two")

    assert status["error"] == {
        "code": "subagent_not_found",
        "message": SUBAGENT_NOT_FOUND_STATUS_MESSAGE_TEMPLATE.format(work_id=work_id),
    }
