"""The Sub-Agent batch tracker: completion notices, acknowledgement and pruning."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import core.subagents.tracker as subagent_tracker
from core.chat import ChatSessionManager
from core.runs import Run, RunAdmissionBlockedError, RunExecutionOwner
from core.subagents import SubAgentBatchTracker
from tests.core.sessions.history_fixtures import settle_run
from tests.core.subagents.subagents_test_support import (
    JsonObject,
    RecordingTriggerService,
    address,
)

pytestmark = pytest.mark.asyncio

PARENT_KEY = ("parent", "parent-session", "parent-run")
ACTIVITY_FILE = "C:/data/artifacts/temp/subagents/run-one.md"
PARTIAL_NOTE = "Result is partial: the Sub-Agent Run was interrupted by timeout."


def _track(
    tracker: SubAgentBatchTracker,
    run_id: str,
    *,
    parent_key: tuple[str, str, str] = PARENT_KEY,
    parent_project: str | None = None,
    child_project: str | None = None,
    session_id: str | None = None,
    activity_file: str | None = None,
    execution_owner: RunExecutionOwner | None = None,
) -> str:
    """Track one started child Run the way a spawn does and return its work id."""
    assert tracker.reserve_slot(parent_key, 8, parent_project, execution_owner=execution_owner)
    work_id = f"sub_{run_id}"
    tracker.register_reserved(
        parent_key,
        "worker",
        session_id or f"session-{run_id}",
        run_id,
        child_project,
        activity_file,
        work_id=work_id,
    )
    return work_id


def _batch_open(tracker: SubAgentBatchTracker) -> bool:
    return tracker.references_identity_agent("parent")


@pytest.mark.parametrize(
    ("result", "activity_file", "status_line", "text"),
    [
        (
            {"status": "completed", "result": "x" * 2000},
            ACTIVITY_FILE,
            "completed",
            # The whole result is delivered, without truncation.
            f"Activity file: {ACTIVITY_FILE}\n" + "x" * 2000,
        ),
        ({"status": "failed", "result": None, "note": "boom"}, None, "failed", "(no output) boom"),
        (
            {"status": "cancelled", "result": "Cancelled by the user", "cancelled_by_user": True},
            None,
            "cancelled by user",
            "Cancelled by the user",
        ),
        ({"status": "cancelled", "result": None}, None, "cancelled", "(no output)"),
        (
            {
                "status": "completed",
                "result": "I am about to write the plan.",
                "interrupted": True,
                "interruption_cause": "timeout",
                "note": PARTIAL_NOTE,
            },
            None,
            "interrupted (timeout)",
            f"I am about to write the plan.\n\n{PARTIAL_NOTE}",
        ),
    ],
)
async def test_completion_notice_describes_how_the_child_ended(
    result: JsonObject, activity_file: str | None, status_line: str, text: str
) -> None:
    triggers = RecordingTriggerService()
    tracker = SubAgentBatchTracker(triggers)
    _track(tracker, "run-one", activity_file=activity_file)

    tracker.on_sub_agent_complete(PARENT_KEY, "run-one", result)

    assert triggers.bodies == [
        f"### Sub-Agent worker (id sub_run-one, session session-run-one) — {status_line}\n{text}"
    ]


async def test_each_result_is_submitted_once() -> None:
    triggers = RecordingTriggerService()
    tracker = SubAgentBatchTracker(triggers)
    first = _track(tracker, "run-one")
    second = _track(tracker, "run-two")

    tracker.on_sub_agent_complete(PARENT_KEY, "run-one", {"result": "First result"})
    tracker.on_sub_agent_complete(PARENT_KEY, "run-two", {"result": "Second result"})
    tracker.on_sub_agent_complete(PARENT_KEY, "run-two", {"result": "Second result again"})

    assert [notice.notice_id for notice in triggers.notices] == [
        f"subagent:parent-run:{first}",
        f"subagent:parent-run:{second}",
    ]
    assert triggers.bodies[1].endswith("\nSecond result")


@pytest.mark.parametrize(
    ("parent_key", "parent_project", "owner"),
    [
        (PARENT_KEY, None, None),
        # A Project Agent's Parent continues under its project and execution owner.
        (
            ("orchestrator", "parent-session", "parent-run"),
            "vbot",
            RunExecutionOwner("swarm", "group", "peer", "generation", "epoch"),
        ),
    ],
)
async def test_notice_continues_the_parent_where_it_ran(
    parent_key: tuple[str, str, str], parent_project: str | None, owner: RunExecutionOwner | None
) -> None:
    triggers = RecordingTriggerService()
    tracker = SubAgentBatchTracker(triggers)
    _track(
        tracker,
        "run-one",
        parent_key=parent_key,
        parent_project=parent_project,
        child_project="vbot",
        execution_owner=owner,
    )

    tracker.on_sub_agent_complete(parent_key, "run-one", {"result": "output"})

    [notice] = triggers.notices
    assert (notice.agent_id, notice.session_id, notice.project_id, notice.execution_owner) == (
        parent_key[0],
        "parent-session",
        parent_project,
        owner,
    )
    assert notice.body.startswith("### Sub-Agent worker@vbot (id sub_run-one,")


async def test_batch_closes_once_every_notice_is_stored() -> None:
    triggers = RecordingTriggerService()
    triggers.defer_persistence = True
    tracker = SubAgentBatchTracker(triggers)
    _track(tracker, "run-one")
    _track(tracker, "run-two")
    tracker.on_sub_agent_complete(PARENT_KEY, "run-one", {"result": "first output"})
    tracker.on_sub_agent_complete(PARENT_KEY, "run-two", {"result": "second output"})
    assert _batch_open(tracker)

    triggers.persist()

    assert len(triggers.notices) == 2
    assert not _batch_open(tracker)


async def test_result_fetched_through_status_withdraws_its_notice() -> None:
    triggers = RecordingTriggerService()
    triggers.defer_persistence = True
    tracker = SubAgentBatchTracker(triggers)
    first = _track(tracker, "run-one")
    _track(tracker, "run-two")
    tracker.on_sub_agent_complete(PARENT_KEY, "run-one", {"result": "first output"})

    tracker.mark_fetched(PARENT_KEY, "session-run-one", "run-one", sub_agent_id="worker")
    triggers.defer_persistence = False
    tracker.on_sub_agent_complete(PARENT_KEY, "run-two", {"result": "second output"})

    assert triggers.cancelled_notice_ids == [f"subagent:parent-run:{first}"]
    assert len(triggers.notices) == 2
    assert not _batch_open(tracker)


async def test_result_fetched_before_its_watcher_reports_sends_no_notice() -> None:
    triggers = RecordingTriggerService()
    tracker = SubAgentBatchTracker(triggers)
    _track(tracker, "run-one")

    # A status call read the terminal Run before the completion watcher ran.
    tracker.mark_fetched(PARENT_KEY, "session-run-one", "run-one")
    tracker.on_sub_agent_complete(PARENT_KEY, "run-one", {"result": "Already fetched"})

    assert triggers.notices == []
    assert not _batch_open(tracker)


@pytest.mark.parametrize("sibling", [None, "delivered", "running"])
async def test_removed_queue_entry_leaves_only_its_siblings_notice(sibling: str | None) -> None:
    triggers = RecordingTriggerService()
    tracker = SubAgentBatchTracker(triggers)
    if sibling is not None:
        _track(tracker, "run-b")
    assert tracker.reserve_slot(PARENT_KEY, 8)
    tracker.register_queued(PARENT_KEY, "worker", "session-a", "queue-item-a", work_id="sub_a")
    if sibling == "delivered":
        tracker.on_sub_agent_complete(PARENT_KEY, "run-b", {"result": "b output"})

    tracker.remove_queued(PARENT_KEY, "queue-item-a")
    if sibling == "running":
        assert _batch_open(tracker)
        tracker.on_sub_agent_complete(PARENT_KEY, "run-b", {"result": "b output"})

    assert triggers.bodies == ([] if sibling is None else [triggers.bodies[0]])
    if sibling is not None:
        assert triggers.bodies[0].endswith("\nb output")
    assert not _batch_open(tracker)


@pytest.mark.parametrize("outcome", ["delivered", "failed", "owner-closed"])
async def test_child_is_marked_read_only_after_its_notice_is_stored(
    tmp_path: Path,
    current_format_data_directory: None,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    del current_format_data_directory
    sessions = ChatSessionManager(tmp_path)
    child = address("worker", "session-run-one")
    sessions.create("worker", session_id=child.session_id)
    settle_run(sessions, child, "run-one", completed_at="2026-07-22T10:00:00+00:00")
    triggers = RecordingTriggerService()
    triggers.defer_persistence = True
    delivered = outcome == "delivered"
    if outcome == "failed":
        triggers.error = RuntimeError("parent unavailable")
    elif outcome == "owner-closed":
        triggers.error = RunAdmissionBlockedError("completion owner can no longer receive work")
    logged: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        subagent_tracker._LOGGER, "error", lambda *args, **_kwargs: logged.append(args)
    )
    tracker = SubAgentBatchTracker(triggers, sessions=sessions)
    _track(tracker, "run-one")

    try:
        tracker.on_sub_agent_complete(PARENT_KEY, "run-one", {"result": "done"})
        assert sessions.list_summaries("worker")[0]["has_unread_completion"] is True
        triggers.persist()
        await asyncio.sleep(0)  # Delivery outcomes are reported by Future callbacks.

        unread = sessions.list_summaries("worker")[0]["has_unread_completion"]
    finally:
        sessions.close()
    # A notice that cannot be delivered releases the work but leaves the child unread.
    assert unread is not delivered
    assert not _batch_open(tracker)
    if outcome == "failed":
        assert "Sub-Agent completion delivery failed" in logged[0][1]
        assert str(logged[0][2]) == "parent unavailable"
    else:
        # A closed execution owner is an expected lifecycle end, not an error.
        assert logged == []


async def test_work_ids_are_unique_across_parent_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.utils import ids

    tracker = SubAgentBatchTracker(RecordingTriggerService())
    first = ("agent", "session", "run-one")
    second = ("agent", "session", "run-two")
    assert tracker.reserve_slot(first, 2)
    assert tracker.reserve_slot(second, 2)
    values = iter((1, 1, 2))
    monkeypatch.setattr(ids.secrets, "randbits", lambda _bits: next(values))

    assert tracker.allocate_work_id(first) == "sub_000000000001"
    assert tracker.allocate_work_id(second) == "sub_000000000002"


async def test_parent_run_budget_survives_its_pruned_batch() -> None:
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    parent = ("parent", "session", "run-parent")
    run = Run(run_id=parent[2], agent_id=parent[0], session_id=parent[1])
    assert tracker.reserve_slot(parent, max_count=1, parent_run=run)
    tracker.register_reserved(parent, "child", "child-session", "child-run")
    tracker.mark_fetched(parent, "child-session", "child-run")
    tracker.on_sub_agent_complete(parent, "child-run", {"status": "completed", "result": "done"})
    assert tracker.owned_entries(parent[0], parent[1], None) == []

    assert not tracker.reserve_slot(parent, max_count=1, parent_run=run)
    next_run = Run(run_id="next", agent_id=parent[0], session_id=parent[1])
    assert tracker.reserve_slot((parent[0], parent[1], "next"), max_count=1, parent_run=next_run)


async def test_open_batches_report_the_identity_agents_they_address() -> None:
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    tracker.reserve_slot(("parent", "session", "run"), 2, project_id=None)
    tracker.reserve_slot(("project-parent", "session", "run"), 2, project_id="vbot")
    tracker.register_reserved(("project-parent", "session", "run"), "child", "s", "r")
    tracker.reserve_slot(("qualified-parent", "session", "run"), 2, project_id="vbot")
    tracker.register_reserved(
        ("qualified-parent", "session", "run"), "qualified-child", "s", "r", project_id="vbot"
    )

    assert tracker.references_identity_agent("parent") is True
    assert tracker.references_identity_agent("child") is True
    assert tracker.references_identity_agent("project-parent") is False
    assert tracker.references_identity_agent("qualified-parent") is False
    assert tracker.references_identity_agent("qualified-child") is False
    assert tracker.references_identity_agent("missing") is False
