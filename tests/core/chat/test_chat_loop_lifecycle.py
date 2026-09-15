"""Chat-loop tests grouped by lifecycle."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from core.chat.continuation import ContinuationTracker
from core.providers.errors import ProviderTimeoutError
from core.runs import RunAdmission, RunExecutionOwner, RunStatus
from core.sessions import ChatSession
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
async def test_owned_descendant_skips_titles_and_reflection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = StubAgent(
        id="coder",
        model="openrouter/anthropic/claude-sonnet-4",
        allowed_tools=["*"],
        workspace=tmp_path / "workspace-coder",
    )
    adapter = StubAdapter([{"content": "Done", "reasoning": None, "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.chat_sessions.create("coder", session_id="session-one")

    async def record_owned_run(*_args: Any, **_kwargs: Any) -> None:
        pass

    monkeypatch.setattr(runtime.chat_sessions, "record_run_owner_async", record_owned_run)
    reflection = RecordingReflection()

    class Titles:
        calls: list[dict[str, Any]] = []

        def notify_user_message(self, **kwargs: Any) -> None:
            self.calls.append(kwargs)

    loop = build_chat_loop(runtime, reflection_service=reflection, session_title_service=Titles())
    owner = RunExecutionOwner("swarm", "group", "participant", "generation", "epoch")
    run = await runtime.chat_run_manager.start(
        session_address("coder", "session-one"),
        loop.run_executor("Continue the assigned task"),
        admission=RunAdmission(owner=owner),
    )
    await run.wait()

    assert reflection.calls == []
    assert Titles.calls == []


@pytest.mark.asyncio
async def test_internal_bootstrap_can_resume_process_restart_continuation(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openrouter/anthropic/claude-sonnet-4")
    adapter = StubAdapter([{"content": "Verified", "reasoning": None, "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
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

    assert session.load_continuation_records() == []


@pytest.mark.asyncio
async def test_ordinary_internal_run_does_not_consume_continuation(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openrouter/anthropic/claude-sonnet-4")
    adapter = StubAdapter([{"content": "Done", "reasoning": None, "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    tracker = ContinuationTracker(session, run_id="run-before-restart", request="update vBot")
    await tracker.interrupt("process_restart")

    run = await build_chat_loop(runtime).start_run(
        "coder", "unrelated internal work", session_id="session-one", internal=True
    )
    await run.wait()

    assert session.load_continuation_records()


@pytest.mark.asyncio
async def test_run_summary_write_failure_preserves_result_and_resolves_continuation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = StubAgent(id="coder", model="openrouter/anthropic/claude-sonnet-4")
    adapter = StubAdapter([{"content": "Verified", "reasoning": None, "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    tracker = ContinuationTracker(session, run_id="run-before-restart", request="update vBot")
    await tracker.interrupt("process_restart")
    append_async = ChatSession.append_async

    async def fail_run_summary(self, message):
        if message.role == "run_summary":
            raise OSError("injected summary write failure")
        await append_async(self, message)

    monkeypatch.setattr(ChatSession, "append_async", fail_run_summary)

    run = await build_chat_loop(runtime).start_run(
        "coder",
        "Verify the update",
        session_id="session-one",
        internal=True,
        resume_process_restart=True,
    )
    result = await run.wait()

    assert run.status is RunStatus.COMPLETED
    assert result.content == "Verified"
    assert session.load_continuation_records() == []
    assert all(message.role != "run_summary" for message in session.load())


@pytest.mark.asyncio
async def test_continuation_finalization_failure_does_not_replace_run_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = StubAgent(id="coder", model="openrouter/anthropic/claude-sonnet-4")
    adapter = StubAdapter([{"content": "Done", "reasoning": None, "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
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
    )
    await run.wait()

    persisted = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert [message.role for message in persisted] == ["note", "assistant", "run_summary"]
    assert persisted[-1].run_id == run.id
    assert persisted[-1].status == "completed"
    assert persisted[-1].iteration_count == 1
    assert run.events[-1].payload["iteration_count"] == 1
    activity = runtime.chat_sessions.list_with_metadata("coder")[0]
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
        async def send(self, messages, *, model_id, **kwargs):
            attempts = 0

            async def request():
                nonlocal attempts
                attempts += 1
                if attempts == 1:
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
    assert status[1]["max_attempts"] == 4
    assert "private-provider-detail" not in str(status)
    assert run.iteration_count == 1
    assert not any(
        message.role == "error"
        for message in runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("expected", [False, True])
async def test_run_failures_remain_visible_in_history_once(tmp_path, expected, caplog):
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
        if record.name in {"vbot.chat", "vbot.runs"}
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
