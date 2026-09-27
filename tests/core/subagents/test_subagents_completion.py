"""Delivering finished Sub-Agent work to its Parent Session.

A foreground result reaches the Parent inline; every tracked child is also
submitted as an automatic completion notice, which is withdrawn once the Parent
has durably stored the result.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

import core.subagents.tracker as subagent_tracker
from core.runs import ChatRunManager, Run, RunAdmissionBlockedError
from core.subagents._constants import (
    SUBAGENT_REMOVED_FROM_QUEUE_MESSAGE,
    SUBAGENT_START_FAILED_MESSAGE_TEMPLATE,
)
from tests.core.sessions.history_fixtures import settle_run
from tests.core.subagents.subagents_test_support import (
    ExecutorLoop,
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


@pytest_asyncio.fixture
async def run_manager_harness(
    tmp_path: Path, current_format_data_directory: None
) -> AsyncIterator[SubAgentHarness]:
    """A harness whose child Runs execute on the real Run manager."""
    del current_format_data_directory
    subject = SubAgentHarness(tmp_path, manager=ChatRunManager())
    try:
        yield subject
    finally:
        await subject.close()


async def test_foreground_result_stays_owned_and_unread_until_the_parent_stores_it(
    harness: SubAgentHarness,
) -> None:
    harness.triggers.defer_persistence = True
    persisted: list[Any] = []
    call = harness.call_in_background(
        {"content": "spawn", "agent_id": "worker"},
        make_context(nesting_depth=1, result_persisted_hook=persisted.append),
    )
    [child] = await harness.started()
    child.run.mark_completed(done("child output", usage={"input_tokens": 1}))
    result = await call

    data = result["data"]
    work_id = data.pop("id")
    assert work_id == child.admission.work_id
    assert Path(activity_path_from_note(data.pop("activity_note"))).exists()
    assert data == {
        "agent_id": "worker",
        "session_id": child.run.session_id,
        "status": "completed",
        "result": "child output",
        "usage": {"input_tokens": 1},
        "delivery": "inline",
    }
    settle_run(
        harness.sessions,
        address("worker", child.run.session_id),
        child.run.id,
        completed_at="2026-07-22T10:00:00+00:00",
    )
    # A Parent cancelled before it stored the result still receives the notice.
    harness.manager.parent_run.request_cancel(reason="user")
    await harness.settle()
    notice_id = f"subagent:parent-run:{work_id}"
    assert harness.owned(work_id) is not None
    assert [notice.notice_id for notice in harness.triggers.notices] == [notice_id]
    assert harness.unread() is True
    assert len(persisted) == 1

    persisted[0]()

    assert harness.owned(work_id) is None
    assert harness.triggers.cancelled_notice_ids == [notice_id]
    assert harness.unread() is False


async def test_background_child_cancelled_by_the_user_notifies_its_parent(
    harness: SubAgentHarness,
) -> None:
    spawned = await harness.spawn({"content": "do work", "agent_id": "worker"})
    [child] = await harness.started()

    # The same Run signal as the user's Cancel control in the Child Session.
    child.run.request_cancel(reason="user")
    child.run.mark_cancelled()
    await harness.settle()

    assert spawned["delivery"] == "automatic"
    [notice] = harness.triggers.notices
    assert (notice.agent_id, notice.session_id, notice.project_id) == (
        "parent",
        "parent-session",
        None,
    )
    assert notice.body.splitlines()[0] == (
        f"### Sub-Agent worker (id {spawned['id']}, session {spawned['session_id']}) "
        "— cancelled by user"
    )
    assert notice.body.endswith("\nCancelled by the user")


async def test_failing_completion_watcher_is_logged(
    harness: SubAgentHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    logged: list[tuple[Any, ...]] = []

    def fail(*_args: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(harness.tracker, "on_sub_agent_complete", fail)
    monkeypatch.setattr(
        subagent_tracker._LOGGER, "error", lambda *args, **_kwargs: logged.append(args)
    )
    harness.manager.next_result = done()

    result = await harness.call({"content": "do work"})
    await harness.settle()

    assert result["ok"] is True
    assert "Sub-agent completion tracker failed" in logged[0][1]
    assert str(logged[0][2]) == "boom"


async def test_child_executor_abort_is_delivered_as_cancelled(
    run_manager_harness: SubAgentHarness,
) -> None:
    class ExecutorAbort(BaseException):
        pass

    runs: list[Run] = []

    async def abort(run: Run) -> None:
        runs.append(run)
        raise ExecutorAbort()

    run_manager_harness.runtime.streaming_chat_loop = ExecutorLoop(abort)

    spawned = await run_manager_harness.spawn({"content": "do work", "agent_id": "worker"})
    for _ in range(100):
        if run_manager_harness.triggers.notices:
            break
        await asyncio.sleep(0.01)

    [run] = runs
    assert run._task is not None
    with pytest.raises(ExecutorAbort):
        await run._task
    [notice] = run_manager_harness.triggers.notices
    assert notice.notice_id == f"subagent:parent-run:{spawned['id']}"
    assert notice.body.splitlines()[0].endswith("— cancelled")


@pytest.mark.parametrize(
    ("never_starts", "status", "activity", "note"),
    [
        ("removed", "cancelled", "cancelled before start", SUBAGENT_REMOVED_FROM_QUEUE_MESSAGE),
        (
            "admission_failed",
            "failed",
            "failed before start",
            SUBAGENT_START_FAILED_MESSAGE_TEMPLATE.format(error="owner closed"),
        ),
    ],
)
async def test_background_queue_item_that_never_starts_is_delivered_and_answerable(
    harness: SubAgentHarness, never_starts: str, status: str, activity: str, note: str
) -> None:
    harness.sessions.create("worker", session_id="busy-child")
    harness.manager.make_busy("worker", "busy-child")
    harness.manager.hold_enqueued_starts = True
    harness.triggers.defer_persistence = True
    spawned = await harness.spawn(
        {"content": "follow-up", "agent_id": "worker", "session_id": "busy-child"}
    )
    [child] = await harness.enqueued()

    if never_starts == "removed":
        # The user removes the item through the Session's Queue, not the Parent.
        assert harness.manager.remove_queued("worker", "busy-child", child.item.item_id)
    else:
        harness.manager.fail_next_enqueued_start(RunAdmissionBlockedError("owner closed"))
    await harness.settle()

    [notice] = harness.triggers.notices
    assert notice.notice_id == f"subagent:parent-run:{spawned['id']}"
    assert notice.body.splitlines()[0].endswith(f"— {status}")
    assert notice.body.endswith(f"\n(no output) {note}")
    activity_log = Path(activity_path_from_note(spawned["activity_note"])).read_text(
        encoding="utf-8"
    )
    assert activity_log.rstrip().endswith(activity)
    persisted: list[Any] = []
    later = make_context(run_id="later-run", result_persisted_hook=persisted.append)

    answer = await harness.call({"action": "status", "id": spawned["id"]}, later)

    assert answer["data"]["status"] == status
    assert answer["data"]["result"] is None
    assert "run_id" not in answer["data"]
    persisted[0]()
    assert harness.owned(spawned["id"]) is None
    assert harness.triggers.cancelled_notice_ids == [notice.notice_id]
