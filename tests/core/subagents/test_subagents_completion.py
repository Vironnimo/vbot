"""Tests for subagents completion."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage
from core.runs import (
    ChatRunManager,
    Run,
    RunExecutionOwner,
)
from core.sessions import ChatSession, SessionAddress
from core.subagents._completion import _track_subagent_completion
from core.subagents._status import _handle_subagent_status
from core.subagents.subagents import SubAgentCoordinator
from core.subagents.subagents import _handle_subagent as _handle_subagent_impl
from core.subagents.tracker import SubAgentBatchTracker
from core.tools.tools import ToolContext
from tests.core.subagents.subagents_test_support import (
    FakeRunManager,
    JsonObject,
    RecordingTriggerService,
    _address,
    _handle_subagent,
    make_context,
    make_runtime,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("current_format_data_directory")]


async def _handle_subagent_result(
    context: ToolContext,
    arguments: JsonObject,
    *,
    runtime: Any,
    batch_tracker: SubAgentBatchTracker,
) -> JsonObject:
    return await _handle_subagent_status(
        context,
        {"action": "status", **arguments},
        runtime=runtime,
        batch_tracker=batch_tracker,
    )


async def test_foreground_result_keeps_handle_and_child_unread_until_parent_persistence(
    tmp_path: Path,
) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    trigger_service = RecordingTriggerService()
    tracker = SubAgentBatchTracker(trigger_service)
    persisted_callbacks: list[Any] = []
    context = make_context(
        nesting_depth=1,
        result_persisted_hook=persisted_callbacks.append,
    )

    task = asyncio.create_task(
        _handle_subagent(
            context,
            {"content": "spawn", "agent_id": "worker"},
            runtime=runtime,
            batch_tracker=tracker,
        )
    )
    await asyncio.sleep(0)
    manager.started[0]["run"].mark_completed(
        ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="child output",
        )
    )
    result = await task

    assert result["ok"] is True
    assert manager.started[0]["work_id"] == result["data"]["id"]
    assert len(persisted_callbacks) == 1
    child_session_id = result["data"]["session_id"]
    child_run_id = manager.started[0]["run"].id
    work_id = result["data"]["id"]
    runtime.chat_sessions.record_terminal_run(
        _address("worker", child_session_id),
        child_run_id,
        "completed",
        "2026-07-22T10:00:00+00:00",
    )
    manager.parent_run.request_cancel(reason="user")
    await asyncio.sleep(0)
    assert tracker.owned_entry("parent", "parent-session", None, work_id) is not None
    assert f"subagent:parent-run:{work_id}" in trigger_service.completion_deliveries
    assert runtime.chat_sessions.list_with_metadata("worker")[0]["has_unread_completion"] is True

    persisted_callbacks[0]()

    assert tracker.owned_entry("parent", "parent-session", None, work_id) is None
    assert f"subagent:parent-run:{work_id}" not in trigger_service.completion_deliveries
    assert runtime.chat_sessions.list_with_metadata("worker")[0]["has_unread_completion"] is False


async def test_executor_base_exception_reaches_subagent_completion_watcher(monkeypatch) -> None:
    class ExecutorAbort(BaseException):
        pass

    manager = ChatRunManager()
    trigger = RecordingTriggerService()
    tracker = SubAgentBatchTracker(trigger)
    delivered = asyncio.Event()
    original_complete = tracker.on_sub_agent_complete

    def record_completion(parent_key, run_id, result):
        original_complete(parent_key, run_id, result)
        delivered.set()

    monkeypatch.setattr(tracker, "on_sub_agent_complete", record_completion)

    async def execute(_run: Run) -> None:
        raise ExecutorAbort()

    run = await manager.start(_address("worker", "child-session"), execute)
    parent_key = ("parent", "parent-session", "parent-run")
    tracker.register(parent_key, run.agent_id, run.session_id, run.id)
    _track_subagent_completion(tracker, parent_key, run, None)
    assert run._task is not None
    with pytest.raises(ExecutorAbort):
        await run._task
    await asyncio.wait_for(delivered.wait(), timeout=1)
    owned = tracker.owned_entry("parent", "parent-session", None, run.id)
    assert owned is not None
    _, entry = owned
    assert entry.complete
    assert entry.result is not None
    assert entry.result["status"] == "cancelled"
    assert f"subagent:parent-run:{run.id}" in trigger.completion_deliveries
    await manager.aclose()


async def test_parent_cancel_during_spawn_window_still_cascades_and_tracks(
    tmp_path: Path,
) -> None:
    """A parent cancel landing after the child started must not orphan it.

    Regression: the tracker registration, parent-cancel cascade, and completion
    watcher used to happen only after the session-started emission - an await.
    A cancel arriving in that window left the live child running untracked,
    un-cascaded, and invisible to the batch.
    """
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    trigger_service = RecordingTriggerService()
    tracker = SubAgentBatchTracker(trigger_service)

    captured: dict[str, Any] = {}

    async def canceling_emit(_event_type: str, payload: JsonObject) -> None:
        data = payload.get("data", {})
        if data.get("run_id") is None:
            return
        # The running-child emission sits exactly in the historical orphan
        # window: the child Run is live while the spawn flow is suspended here.
        captured["work_id"] = data["id"]
        # Cancel while the child is still running - a terminal child could not
        # record the cascade anymore.
        manager.parent_run.request_cancel(reason="user")
        manager.started[0]["run"].mark_completed(
            ChatMessage.assistant(model="openai/gpt-5.2", content="child output")
        )

    context = make_context(nesting_depth=1, emit_hook=canceling_emit)

    result = await _handle_subagent(
        context,
        {"content": "spawn", "agent_id": "worker"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert result["ok"] is True
    assert captured["work_id"] == result["data"]["id"]
    child_run = manager.started[0]["run"]
    assert child_run.cancel_requested is True
    assert tracker.owned_entry("parent", "parent-session", None, captured["work_id"]) is not None


@pytest.mark.parametrize("all_entries", [False, True])
async def test_status_result_keeps_handle_and_child_unread_until_parent_persistence(
    tmp_path: Path,
    all_entries: bool,
) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    trigger_service = RecordingTriggerService()
    tracker = SubAgentBatchTracker(trigger_service)
    child = runtime.chat_sessions.create("worker", session_id="child-session")
    child.append(ChatMessage.assistant(model="openai/gpt-5.2", content="child output"))
    child.append(
        ChatMessage.run_summary(
            run_id="child-run",
            status="completed",
            iteration_count=1,
            timing={
                "started_at": "2026-07-22T10:00:00+00:00",
                "completed_at": "2026-07-22T10:00:01+00:00",
                "duration_ms": 1000,
            },
        )
    )
    runtime.chat_sessions.record_terminal_run(
        _address("worker", "child-session"),
        "child-run",
        "completed",
        "2026-07-22T10:00:00+00:00",
    )
    persisted_callbacks: list[Any] = []
    context = make_context(result_persisted_hook=persisted_callbacks.append)
    tracker.register(
        (context.agent_id, context.session_id, context.run_id),
        "worker",
        "child-session",
        "child-run",
        work_id="sub_child",
    )

    result = await _handle_subagent_result(
        context,
        {} if all_entries else {"id": "sub_child"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert result["ok"] is True
    snapshot = result["data"]["subagents"][0] if all_entries else result["data"]
    assert snapshot["id"] == "sub_child"
    assert snapshot["result"] == "child output"
    assert "run_id" not in snapshot
    assert len(persisted_callbacks) == 1
    tracker.on_sub_agent_complete(
        (context.agent_id, context.session_id, context.run_id),
        "child-run",
        {"status": "completed", "result": "child output"},
    )
    manager.parent_run.request_cancel(reason="user")
    assert tracker.owned_entry("parent", "parent-session", None, "sub_child") is not None
    assert "subagent:parent-run:sub_child" in trigger_service.completion_deliveries
    assert runtime.chat_sessions.list_with_metadata("worker")[0]["has_unread_completion"] is True

    persisted_callbacks[0]()

    assert tracker.owned_entry("parent", "parent-session", None, "sub_child") is None
    assert "subagent:parent-run:sub_child" not in trigger_service.completion_deliveries
    assert runtime.chat_sessions.list_with_metadata("worker")[0]["has_unread_completion"] is False

    if all_entries:
        empty = await _handle_subagent_result(context, {}, runtime=runtime, batch_tracker=tracker)
        assert empty["data"] == {"subagents": []}


@pytest.mark.parametrize("project_id", [None, "parent-project"])
async def test_status_all_is_scoped_across_parent_runs(
    tmp_path: Path, project_id: str | None
) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(project_id=project_id)
    for agent, session, run, project, work in [
        ("parent", "parent-session", "earlier-run", project_id, "sub_running"),
        ("parent", "parent-session", "parent-run", project_id, "sub_queued"),
        ("other", "parent-session", "other-agent-run", project_id, "sub_other_agent"),
        ("parent", "other-session", "other-session-run", project_id, "sub_other_session"),
        ("parent", "parent-session", "other-project-run", "other-project", "sub_other_project"),
    ]:
        parent_key = (agent, session, run)
        assert tracker.reserve_slot(parent_key, 8, project_id=project)
        tracker.register_queued(
            parent_key,
            "worker",
            "child",
            f"queue-{work}",
            project_id="child-project",
            work_id=work,
        )
    child_run = Run(
        run_id="child-run", agent_id="worker", session_id="child", project_id="child-project"
    )
    manager.runs[child_run.id] = child_run
    assert tracker.mark_started(
        ("parent", "parent-session", "earlier-run"), "queue-sub_running", child_run.id
    )

    result = await _handle_subagent_result(context, {}, runtime=runtime, batch_tracker=tracker)

    assert result["ok"] is True
    snapshots = result["data"]["subagents"]
    assert [item["id"] for item in snapshots] == ["sub_running", "sub_queued"]
    assert [item["status"] for item in snapshots] == ["running", "queued"]
    for item in snapshots:
        single = await _handle_subagent_result(
            context, {"id": item["id"]}, runtime=runtime, batch_tracker=tracker
        )
        assert item == single["data"]
        assert "run_id" not in item and "queue_item_id" not in item
    child_run.mark_completed({})


async def test_status_all_keeps_other_snapshots_when_one_lookup_fails(tmp_path: Path) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()
    parent_key = (context.agent_id, context.session_id, context.run_id)
    tracker.register(parent_key, "worker", "expected-child", "wrong-run", work_id="sub_wrong")
    wrong_run = Run(run_id="wrong-run", agent_id="other", session_id="other-session")
    manager.runs[wrong_run.id] = wrong_run
    tracker.register_queued(parent_key, "worker", "child", "queued-item", work_id="sub_queued")

    result = await _handle_subagent_result(context, {}, runtime=runtime, batch_tracker=tracker)

    assert result["ok"] is True
    failed, queued = result["data"]["subagents"]
    assert failed["id"] == "sub_wrong"
    assert failed["error"]["code"] == "run_not_found"
    assert queued["id"] == "sub_queued"
    assert queued["status"] == "queued"
    assert "other-session" not in str(result)
    wrong_run.mark_completed({})


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "cancel"},
        {"action": "status", "id": None},
        {"action": "status", "id": 1},
        {"action": "status", "id": " "},
        {"action": "status", "agent_id": "worker"},
        {"action": "status", "content": "invalid"},
        {"action": "status", "ids": []},
    ],
)
async def test_status_all_does_not_widen_invalid_calls(
    tmp_path: Path, arguments: JsonObject
) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    result = await _handle_subagent_impl(
        make_context(), arguments, runtime=runtime, batch_tracker=tracker
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"
    assert manager.started == []


async def test_descendant_admission_inherits_execution_owner_without_session_grants(tmp_path):
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    execution = RunExecutionOwner("fixture", "group", "peer", "generation", "epoch")
    context = replace(make_context(), execution_owner=execution, session_tool_grants=("hidden",))
    result = await _handle_subagent(
        context,
        {"content": "work", "agent_id": "worker"},
        runtime=runtime,
        batch_tracker=tracker,
    )
    assert result["ok"] is True
    assert manager.started[0]["execution_owner"] == execution
    address = SessionAddress(context.project_id, "worker", result["data"]["session_id"])
    assert runtime.chat_sessions.temporary_binding(address) is None
    manager.started[0]["run"].mark_completed({})


async def test_inspect_resolves_exact_completed_work_after_child_session_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    session = runtime.chat_sessions.create("worker", session_id="reused-child")
    old_timing = {
        "started_at": "2026-07-24T10:00:00+00:00",
        "completed_at": "2026-07-24T10:00:01+00:00",
        "duration_ms": 1000,
    }
    new_timing = {
        "started_at": "2026-07-24T11:00:00+00:00",
        "completed_at": "2026-07-24T11:00:01+00:00",
        "duration_ms": 1000,
    }
    session.append(ChatMessage.user("old request"))
    session.append(ChatMessage.assistant(model="openai/gpt-5.2", content="old result"))
    session.append(
        ChatMessage.run_summary(
            run_id="old-run",
            work_id="sub_old",
            status="completed",
            timing=old_timing,
            iteration_count=1,
        )
    )
    session.append(ChatMessage.user("new request"))
    session.append(ChatMessage.assistant(model="openai/gpt-5.2", content="new result"))
    session.append(
        ChatMessage.run_summary(
            run_id="new-run",
            work_id="sub_new",
            status="completed",
            timing=new_timing,
            iteration_count=1,
        )
    )

    def fail_full_load(self: ChatSession) -> list[ChatMessage]:
        raise AssertionError("Sub-Agent inspection must use the terminal Run projection")

    monkeypatch.setattr(ChatSession, "load", fail_full_load)

    result = SubAgentCoordinator(runtime, RecordingTriggerService()).inspect(
        "worker",
        "reused-child",
        "sub_old",
    )

    assert result is not None
    assert result["id"] == "sub_old"
    assert result["run_id"] == "old-run"
    assert result["status"] == "completed"
    assert result["result"] == "old result"
    assert result["timing"] == old_timing


async def test_inspect_prefers_matching_live_work_in_child_session(tmp_path: Path) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    runtime.chat_sessions.create("worker", session_id="live-child")
    active = Run(
        run_id="live-run",
        agent_id="worker",
        session_id="live-child",
        work_id="sub_live",
    )
    manager.busy_sessions[("worker", "live-child")] = active

    result = SubAgentCoordinator(runtime, RecordingTriggerService()).inspect(
        "worker",
        "live-child",
        "sub_live",
    )

    assert result is not None
    assert result["id"] == "sub_live"
    assert result["run_id"] == "live-run"
    assert result["status"] == "running"
    assert result["started_at"] == active.created_at
    assert result["result"] is None


async def test_qualified_subagent_result_uses_target_project_for_persisted_fallback(
    tmp_path: Path,
) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(project_id=None)
    session = runtime.chat_sessions.create("worker", session_id="project-child", project_id="vbot")
    session.append(ChatMessage.assistant(model="openai/gpt-5.2", content="project result"))
    session.append(
        ChatMessage.run_summary(
            run_id="missing-run",
            status="completed",
            iteration_count=1,
            timing={
                "started_at": "2026-07-24T10:00:00+00:00",
                "completed_at": "2026-07-24T10:00:01+00:00",
                "duration_ms": 1000,
            },
        )
    )
    tracker.register(
        (context.agent_id, context.session_id, context.run_id),
        "worker",
        "project-child",
        "missing-run",
        "vbot",
        work_id="sub_project",
    )

    result = await _handle_subagent_result(
        context,
        {"id": "sub_project"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert result["ok"] is True
    assert result["data"]["project_id"] == "vbot"
    assert result["data"]["result"] == "project result"


async def test_status_cannot_read_unowned_subagent_work(tmp_path: Path) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(project_id="acme")
    run = Run(
        run_id="cross-project-run",
        agent_id="worker",
        session_id="project-child",
        project_id="vbot",
    )
    manager.runs[run.id] = run
    run.mark_completed(ChatMessage.assistant(model="openai/gpt-5.2", content="live result"))

    result = await _handle_subagent_result(
        context,
        {"id": "sub_unowned"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "subagent_not_owned"


async def test_malformed_qualified_subagent_address_fails_cleanly(
    tmp_path: Path,
) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(project_id=None)
    arguments: JsonObject = {"agent_id": "worker@vbot@extra", "session_id": "child"}
    arguments["content"] = "spawn"
    result = await _handle_subagent(
        context,
        arguments,
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"
