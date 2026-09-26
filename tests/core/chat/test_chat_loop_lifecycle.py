"""Chat-loop tests grouped by lifecycle."""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any

import pytest

import core.tools.change_tracker as change_tracker_module
from core.chat.continuation import ContinuationTracker
from core.providers.errors import ProviderTimeoutError
from core.runs import RunAdmission, RunCancelledError, RunExecutionOwner, RunStatus
from core.tools.change_tracker import ChangeTracker
from core.utils.retry import retry_async
from tests.core.chat.chat_loop_support import (
    RecordingReflection,
    StubAdapter,
    StubAgent,
    StubRuntime,
    build_chat_loop,
    session_address,
)

JsonObject = dict[str, Any]


@pytest.mark.asyncio
async def test_run_end_notifies_reflection_service_on_success(tmp_path: Path) -> None:
    agent = StubAgent(
        id="coder",
        model="openrouter/anthropic/claude-sonnet-4",
        allowed_tools=["*"],
        workspace=tmp_path / "workspace-coder",
    )
    adapter = StubAdapter([{"content": "Hello", "reasoning": None, "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    reflection = RecordingReflection()

    await build_chat_loop(runtime, reflection_service=reflection).send(
        "coder", "Hi", session_id="session-one"
    )

    assert len(reflection.calls) == 1
    call = reflection.calls[0]
    assert call["agent_id"] == "coder"
    assert call["session_id"] == "session-one"
    assert call["iteration_count"] == 1
    assert call["agent"].id == "coder"
    assert call["internal"] is False
    assert call["outcome"] == "success"


@pytest.mark.asyncio
async def test_run_end_notifies_reflection_with_internal_flag(tmp_path: Path) -> None:
    agent = StubAgent(
        id="coder",
        model="openrouter/anthropic/claude-sonnet-4",
        allowed_tools=["*"],
        workspace=tmp_path / "workspace-coder",
    )
    adapter = StubAdapter([{"content": "Done", "reasoning": None, "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.chat_sessions.create("coder", session_id="session-one")
    reflection = RecordingReflection()

    run = await build_chat_loop(runtime, reflection_service=reflection).start_run(
        "coder", "internal note", session_id="session-one", internal=True
    )
    await run.wait()

    # The loop reports the flag verbatim; the service is the one that gates it.
    assert len(reflection.calls) == 1
    assert reflection.calls[0]["internal"] is True


@pytest.mark.asyncio
async def test_owned_descendant_skips_titles_and_reflection(tmp_path: Path) -> None:
    agent = StubAgent(
        id="coder",
        model="openrouter/anthropic/claude-sonnet-4",
        allowed_tools=["*"],
        workspace=tmp_path / "workspace-coder",
    )
    adapter = StubAdapter([{"content": "Done", "reasoning": None, "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.chat_sessions.create("coder", session_id="session-one")
    # The owner's participant binding lives in its own bound Session; the owned
    # Run executes in a descendant Session the owner continues.
    binding = runtime.chat_sessions.create_bound_temporary_session(
        session_address("coder", "participant-session"),
        owner_name="swarm",
        group_id="group",
        participant_id="participant",
        config={},
    )
    reflection = RecordingReflection()

    class Titles:
        calls: list[dict[str, Any]] = []

        def notify_user_message(self, **kwargs: Any) -> None:
            self.calls.append(kwargs)

    loop = build_chat_loop(runtime, reflection_service=reflection, session_title_service=Titles())
    owner = RunExecutionOwner("swarm", "group", "participant", binding.generation_id, "epoch")
    run = await runtime.chat_run_manager.start(
        session_address("coder", "session-one"),
        loop.run_executor("Continue the assigned task"),
        admission=RunAdmission(owner=owner),
    )
    await run.wait()

    assert reflection.calls == []
    assert Titles.calls == []
    # Admission recorded the execution owner with the Run.
    owned = runtime.chat_sessions.owned_runs(owner_name="swarm", group_id="group")
    assert [(record.run_id, record.owner, record.address) for record in owned] == [
        (run.id, owner, session_address("coder", "session-one"))
    ]
    assert owned[0].terminal_status == "completed"


@pytest.mark.asyncio
async def test_internal_bootstrap_can_resume_process_restart_continuation(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openrouter/anthropic/claude-sonnet-4")
    adapter = StubAdapter([{"content": "Verified", "reasoning": None, "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.start_run("run-before-restart")
    tracker = ContinuationTracker(session, run_id="run-before-restart", request="update vBot")
    await tracker.interrupt("process_restart")

    run = await build_chat_loop(runtime).start_run(
        "coder",
        "Verify the update and report",
        session_id="session-one",
        internal=True,
        resume_process_restart=True,
    )
    await run.wait()

    assert session.load_continuation() is None


@pytest.mark.asyncio
async def test_ordinary_internal_run_does_not_consume_continuation(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openrouter/anthropic/claude-sonnet-4")
    adapter = StubAdapter([{"content": "Done", "reasoning": None, "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.start_run("run-before-restart")
    tracker = ContinuationTracker(session, run_id="run-before-restart", request="update vBot")
    await tracker.interrupt("process_restart")

    run = await build_chat_loop(runtime).start_run(
        "coder", "unrelated internal work", session_id="session-one", internal=True
    )
    await run.wait()

    assert session.load_continuation() is not None


@pytest.mark.asyncio
async def test_run_commit_failure_preserves_output_and_recoverable_continuation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = StubAgent(id="coder", model="openrouter/anthropic/claude-sonnet-4")
    adapter = StubAdapter([{"content": "Verified", "reasoning": None, "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.start_run("run-before-restart")
    tracker = ContinuationTracker(session, run_id="run-before-restart", request="update vBot")
    await tracker.interrupt("process_restart")

    async def fail_finish(*args):
        raise OSError("injected Run transaction failure")

    monkeypatch.setattr(runtime.chat_sessions, "finish_run", fail_finish)

    run = await build_chat_loop(runtime).start_run(
        "coder",
        "Verify the update",
        session_id="session-one",
        internal=True,
        resume_process_restart=True,
    )
    with pytest.raises(OSError):
        await run.wait()

    assert run.status is RunStatus.FAILED
    assert run.events[-1].payload["history_persisted"] is False
    assert any(message.content == "Verified" for message in session.load())
    assert session.load_continuation() is not None
    assert session.find_run_summary(run_id=run.id) is None


@pytest.mark.asyncio
async def test_continuation_finalization_failure_does_not_replace_run_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = StubAgent(id="coder", model="openrouter/anthropic/claude-sonnet-4")
    adapter = StubAdapter([{"content": "Done", "reasoning": None, "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.start_run("run-before-restart")
    tracker = ContinuationTracker(session, run_id="run-before-restart", request="update vBot")
    await tracker.interrupt("process_restart")

    async def fail_resolve(_self):
        raise OSError("injected Continuation finalization failure")

    monkeypatch.setattr(ContinuationTracker, "resolve", fail_resolve)

    run = await build_chat_loop(runtime).start_run(
        "coder",
        "Finish the update",
        session_id="session-one",
        internal=True,
        resume_process_restart=True,
    )
    result = await run.wait()

    assert run.status is RunStatus.COMPLETED
    assert result.content == "Done"


@pytest.mark.asyncio
async def test_run_excluded_from_agent_activity_still_persists_its_session_history(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openrouter/anthropic/claude-sonnet-4")
    adapter = StubAdapter([{"content": "System work done", "reasoning": None, "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.chat_sessions.create("coder", session_id="session-one")

    run = await build_chat_loop(runtime).start_run(
        "coder",
        "internal note",
        session_id="session-one",
        internal=True,
        contributes_to_agent_activity=False,
        source_session_id="reviewed-session",
    )
    await run.wait()

    assert run.source_session_id == "reviewed-session"
    persisted = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert [message.role for message in persisted] == ["note", "assistant", "run_summary"]
    assert persisted[-1].run_id == run.id
    assert persisted[-1].status == "completed"
    assert persisted[-1].iteration_count == 1
    assert run.events[-1].payload["iteration_count"] == 1
    activity = runtime.chat_sessions.list_summaries("coder")[0]
    assert activity["latest_completion_run_id"] is None
    assert activity["has_unread_completion"] is False
    assert all(event.contributes_to_agent_activity is False for event in run.events)


@pytest.mark.asyncio
async def test_run_summary_persists_durable_work_id(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openrouter/anthropic/claude-sonnet-4")
    adapter = StubAdapter([{"content": "Sub-Agent done", "reasoning": None, "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.chat_sessions.create("coder", session_id="session-one")
    loop = build_chat_loop(runtime)

    run = await runtime.chat_run_manager.start(
        session_address("coder", "session-one"),
        loop.run_executor("Do work"),
        admission=RunAdmission(work_id="sub-work-one"),
    )
    await run.wait()

    persisted = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert persisted[-1].role == "run_summary"
    assert persisted[-1].run_id == run.id
    assert persisted[-1].work_id == "sub-work-one"


@pytest.mark.asyncio
async def test_run_end_notification_failure_never_breaks_the_run(tmp_path: Path) -> None:
    agent = StubAgent(
        id="coder",
        model="openrouter/anthropic/claude-sonnet-4",
        allowed_tools=["*"],
        workspace=tmp_path / "workspace-coder",
    )
    adapter = StubAdapter([{"content": "Hello", "reasoning": None, "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    reflection = RecordingReflection(raise_on_notify=True)

    assistant = await build_chat_loop(runtime, reflection_service=reflection).send(
        "coder", "Hi", session_id="session-one"
    )

    assert assistant.content == "Hello"


@pytest.mark.asyncio
async def test_child_loop_shares_the_reflection_service(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openrouter/anthropic/claude-sonnet-4")
    adapter = StubAdapter([{"content": "Hello", "reasoning": None, "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    reflection = RecordingReflection()

    parent = build_chat_loop(runtime, reflection_service=reflection)
    child = parent.child_loop(nesting_depth=1)

    assert child._reflection_service is reflection


@pytest.mark.asyncio
async def test_provider_retry_is_visible_before_answer_without_leaking_error(tmp_path):
    class RetryingAdapter(StubAdapter):
        attempts = 0

        async def send(self, messages, *, model_id, **kwargs):
            async def request():
                self.attempts += 1
                if self.attempts == 1:
                    raise ProviderTimeoutError("private-provider-detail")
                return await super(RetryingAdapter, self).send(
                    messages, model_id=model_id, **kwargs
                )

            return await retry_async(request, initial_delay=0)

    agent = StubAgent(id="coder", model="openrouter/anthropic/claude-sonnet-4")
    runtime = StubRuntime(
        data_dir=tmp_path, agent=agent, adapter=RetryingAdapter([{"content": "done"}])
    )
    runtime.chat_sessions.create("coder", session_id="session-one")
    run = await build_chat_loop(runtime).start_run("coder", "Hi", session_id="session-one")
    await run.wait()
    status = [event.payload for event in run.events if event.type == "provider_request_status"]
    assert [item["state"] for item in status] == ["waiting", "retrying", "waiting", "finished"]
    assert status[1]["error_kind"] == "timeout"
    assert status[1]["attempt"] == 2
    assert status[1]["max_attempts"] == 9
    assert "private-provider-detail" not in str(status)
    assert run.iteration_count == 1
    assert not any(
        message.role == "error"
        for message in runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("expected", [False, True])
async def test_run_failures_remain_visible_in_history_once(tmp_path, expected, caplog, monkeypatch):
    monkeypatch.setattr("core.chat.recovery.compute_retry_delay", lambda *a, **kw: (0, False))
    failure = (
        ProviderTimeoutError("timeout sentinel")
        if expected
        else RuntimeError("private-internal-detail")
    )

    class FailingAdapter(StubAdapter):
        async def send(self, *_args, **_kwargs):
            raise failure

    agent = StubAgent(id="coder", model="openrouter/anthropic/claude-sonnet-4")
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=FailingAdapter([]))
    runtime.chat_sessions.create("coder", session_id="session-one")
    run = await build_chat_loop(runtime).start_run("coder", "Hi", session_id="session-one")
    with pytest.raises(type(failure)) as raised:
        await run.wait()
    assert raised.value is failure
    errors = [
        message
        for message in runtime.chat_sessions.get(session_address("coder", "session-one")).load()
        if message.role == "error"
    ]
    assert len(errors) == 1
    assert errors[0].error_kind == ("timeout" if expected else "internal_error")
    assert "private-internal-detail" not in errors[0].content
    assert run.events[-1].payload["error_message_id"] == errors[0].id
    diagnostics = [
        record
        for record in caplog.records
        if record.name == "vbot.runs"
        and record.levelno >= logging.WARNING
        and isinstance(record.args, tuple)
        and record.args[:1] == (run.id,)
    ]
    assert len(diagnostics) == 1
    assert diagnostics[0].levelno == (logging.WARNING if expected else logging.ERROR)
    assert bool(diagnostics[0].exc_info) is not expected


@pytest.mark.asyncio
async def test_preparation_failure_reaches_summary_and_completion_observers(tmp_path):
    class BrokenTitles:
        def notify_user_message(self, **kwargs):
            raise RuntimeError("preparation sentinel")

    agent = StubAgent(id="coder", model="openrouter/anthropic/claude-sonnet-4")
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=StubAdapter([]))
    runtime.chat_sessions.create("coder", session_id="session-one")
    reflection = RecordingReflection()
    loop = build_chat_loop(
        runtime, reflection_service=reflection, session_title_service=BrokenTitles()
    )
    run = await loop.start_run("coder", "Hi", session_id="session-one")
    with pytest.raises(RuntimeError):
        await run.wait()
    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert messages[-1].role == "run_summary"
    assert messages[-1].status == "failed"
    assert messages[-2].role == "error"
    assert messages[-2].error_kind == "internal_error"
    assert reflection.calls[0]["outcome"] == "error"
    assert run.status is RunStatus.FAILED
    assert run.iteration_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_final_change_stats_allow_loop_progress_and_survive_cancel(
    tmp_path, monkeypatch, cancel
):
    agent = StubAgent(id="coder", model="openai/test")
    runtime = StubRuntime(
        data_dir=tmp_path, agent=agent, adapter=StubAdapter([{"content": "Done"}])
    )
    tracker = ChangeTracker()
    runtime.change_tracker = tracker
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    entered = asyncio.Event()
    release = threading.Event()
    event_loop = asyncio.get_running_loop()
    event_loop_thread = threading.get_ident()
    diff_threads = []
    original_diff = change_tracker_module._line_diff_counts

    def slow_diff(before, after):
        diff_threads.append(threading.get_ident())
        event_loop.call_soon_threadsafe(entered.set)
        assert release.wait(5), "Event Loop did not progress while final statistics were computed"
        return original_diff(before, after)

    monkeypatch.setattr(change_tracker_module, "_line_diff_counts", slow_diff)
    run = await build_chat_loop(runtime).start_run("coder", "Work", session_id=session.id)
    key = (session.address, run.id)
    tracker.record_write(key, tmp_path / "file.txt", "before\n", "after\n")
    try:
        await asyncio.wait_for(entered.wait(), 5)
        # The work detached its input before the diff and cannot leak it to another Run.
        assert tracker.peek_run_stats(key) is None
        assert run.status == RunStatus.RUNNING
        if cancel:
            run.request_cancel(reason="user")
        release.set()
        if cancel:
            with pytest.raises(RunCancelledError):
                await run.wait()
        else:
            await run.wait()
        assert diff_threads and all(thread != event_loop_thread for thread in diff_threads)
        assert len(diff_threads) == 1
        assert run.terminal_payload_extras["change_stats"] == {
            "files": 1,
            "added": 1,
            "removed": 1,
            "paths": [str(tmp_path / "file.txt")],
        }
        messages = session.load()
        assert messages[-1].change_stats == run.terminal_payload_extras["change_stats"]
        assert session.load_continuation() is None
    finally:
        release.set()
        await runtime.chat_runs.aclose()
