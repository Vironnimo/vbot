"""Chat Run lifecycle: run-end observers, admission records, restart Continuations and
failure outcomes."""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any, override

import pytest

import core.tools.change_tracker as change_tracker_module
from core.attachments import AttachmentStore
from core.chat import ChatMessage, ChatSessionError
from core.chat.block_resolver import ContentBlockResolver
from core.chat.content_blocks import TextBlock
from core.chat.continuation import ContinuationCause, ContinuationTracker
from core.chat.streaming import StreamingChunkTimeoutError
from core.compaction import CompactionService
from core.providers.errors import NetworkError, ProviderError, ProviderTimeoutError
from core.runs import (
    PROVIDER_REQUEST_STATUS_EVENT,
    RunAdmission,
    RunCancelledError,
    RunExecutionOwner,
    RunStatus,
)
from core.tools.change_tracker import ChangeTracker
from core.utils.errors import VBotError
from tests.core.chat.chat_loop_support import (
    RecordingReflection,
    StubAdapter,
    StubAgent,
    StubRuntime,
    build_chat_loop,
    history,
    session_address,
)

JsonObject = dict[str, Any]

MODEL = "openrouter/anthropic/claude-sonnet-4"
SESSION = session_address("coder", "session-one")


def _runtime(tmp_path: Path, responses: list[Any], **adapter_options: Any) -> Any:
    agent = StubAgent(id="coder", model=MODEL, allowed_tools=["*"])
    adapter = StubAdapter(responses, **adapter_options)
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.chat_sessions.create("coder", session_id="session-one")
    return runtime


def _answer(content: str = "Done") -> JsonObject:
    return {"content": content, "reasoning": None, "tool_calls": None}


async def _interrupted_by_restart(
    runtime: Any, cause: ContinuationCause = "process_restart"
) -> Any:
    """A Session whose previous Run was interrupted, by default by a process restart."""
    session = runtime.chat_sessions.get(SESSION)
    session.start_run("run-before-restart")
    tracker = ContinuationTracker(session, run_id="run-before-restart", request="update vBot")
    await tracker.interrupt(cause)
    return session


def _sent_text(runtime: Any) -> str:
    return "\n".join(
        str(message.get("content") or "") for message in runtime.adapter.requests[0]["messages"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_point", ["start_run", "queue_run"])
@pytest.mark.parametrize("session_state", ["missing", "archived", "recreated"])
async def test_start_run_without_an_existing_session_sends_and_creates_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry_point: str,
    session_state: str,
) -> None:
    runtime = _runtime(tmp_path, [_answer()])
    loop = build_chat_loop(runtime)
    start = getattr(loop, entry_point)

    if session_state == "missing":
        with pytest.raises(ChatSessionError):
            await start("coder", "Hi", session_id="missing-session")
        assert not runtime.chat_sessions.exists(session_address("coder", "missing-session"))
    else:
        # The initial presence check has completed, but Chat has not yet handed
        # the captured generation to Run admission.
        original = runtime.chat_sessions.get(SESSION)
        original.append(ChatMessage.user("Original history"))
        entered, release = asyncio.Event(), asyncio.Event()
        get_async = runtime.chat_sessions.get_async

        async def paused_get(address):
            session = await get_async(address)
            entered.set()
            await release.wait()
            return session

        monkeypatch.setattr(runtime.chat_sessions, "get_async", paused_get)
        pending = asyncio.create_task(start("coder", "Hi", session_id=SESSION.session_id))
        await asyncio.wait_for(entered.wait(), 5)
        async with runtime.chat_runs.session_admission_guard(SESSION):
            await runtime.chat_sessions.archive(SESSION)
            if session_state == "recreated":
                replacement = runtime.chat_sessions.create("coder", session_id=SESSION.session_id)
                replacement.append(ChatMessage.user("Replacement history"))
        release.set()
        admitted = await pending
        run = admitted if entry_point == "start_run" else await admitted.future
        with pytest.raises(ChatSessionError):
            await run.wait()
        assert run.events[-1].payload["history_persisted"] is False
        assert not runtime.chat_runs.has_activity_for_session(
            "coder", SESSION.session_id, project_id=None
        )
        if session_state == "recreated":
            assert [message.content for message in replacement.load()] == ["Replacement history"]
        else:
            assert not runtime.chat_sessions.exists(SESSION)

    assert runtime.adapter.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["user", "internal"])
async def test_run_end_notifies_the_reflection_service(tmp_path: Path, origin: str) -> None:
    runtime = _runtime(tmp_path, [_answer("Hello")])
    reflection = RecordingReflection()
    loop = build_chat_loop(runtime, reflection_service=reflection)

    run = await loop.start_run(
        "coder", "Hi", session_id="session-one", internal=origin == "internal"
    )
    await run.wait()

    # The loop reports the internal flag verbatim; the service is the one that gates it.
    assert len(reflection.calls) == 1
    call = reflection.calls[0]
    assert (call["agent_id"], call["session_id"], call["agent"].id) == (
        "coder",
        "session-one",
        "coder",
    )
    assert call["iteration_count"] == 1
    assert call["internal"] is (origin == "internal")
    assert call["outcome"] == "success"


class _RecordingResolver(ContentBlockResolver):
    def __init__(self, store: AttachmentStore) -> None:
        super().__init__(store)
        self.current_turns: list[str] = []

    @override
    async def resolve_messages(
        self, messages: list[dict[str, Any]], *, current_user_message_id: str, **kwargs: Any
    ) -> list[dict[str, Any]]:
        self.current_turns.append(current_user_message_id)
        return await super().resolve_messages(
            messages, current_user_message_id=current_user_message_id, **kwargs
        )


@pytest.mark.asyncio
async def test_child_loop_runs_like_a_live_run_of_its_parent(tmp_path: Path) -> None:
    # A sub-agent's child loop keeps its parent's wiring: it streams like the
    # parent, resolves content blocks with the parent's attachment resolver,
    # compacts with the parent's Compaction service and reports its run end to the
    # parent's reflection service. Only the nesting depth differs (its effect on the
    # offered Tools: test_chat_loop_tool_definitions.py).
    stream = [{"type": "content_delta", "text": "Hello"}, {"type": "finish", "reason": "stop"}]
    runtime = _runtime(tmp_path, [], stream_responses=[stream])
    resolver = _RecordingResolver(AttachmentStore(tmp_path))
    compaction = CompactionService()
    reflection = RecordingReflection()
    parent = build_chat_loop(
        runtime,
        streaming=True,
        attachment_resolver=resolver,
        compaction_service=compaction,
        reflection_service=reflection,
    )

    child = parent.child_loop(nesting_depth=2)
    answer = await child.send(
        "coder", [TextBlock(type="text", text="Hi")], session_id="session-one"
    )

    assert answer.content == "Hello"
    assert (len(runtime.adapter.stream_requests), runtime.adapter.requests) == (1, [])
    [user] = [message for message in history(runtime) if message.role == "user"]
    assert resolver.current_turns == [user.id]
    assert child.compaction_service is compaction
    assert [(call["session_id"], call["outcome"]) for call in reflection.calls] == [
        ("session-one", "success")
    ]


@pytest.mark.asyncio
async def test_run_end_notification_failure_never_breaks_the_run(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, [_answer("Hello")])
    reflection = RecordingReflection(raise_on_notify=True)

    assistant = await build_chat_loop(runtime, reflection_service=reflection).send(
        "coder", "Hi", session_id="session-one"
    )

    assert assistant.content == "Hello"


@pytest.mark.asyncio
async def test_owned_descendant_records_its_admission_and_skips_titles_and_reflection(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, [_answer()])
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
    title_calls: list[dict[str, Any]] = []

    class Titles:
        def notify_user_message(self, **kwargs: Any) -> None:
            title_calls.append(kwargs)

    loop = build_chat_loop(runtime, reflection_service=reflection, session_title_service=Titles())
    owner = RunExecutionOwner("swarm", "group", "participant", binding.generation_id, "epoch")
    run = await runtime.chat_run_manager.start(
        SESSION,
        loop.run_executor("Continue the assigned task"),
        admission=RunAdmission(owner=owner, work_id="sub-work-one"),
    )
    await run.wait()

    assert reflection.calls == []
    assert title_calls == []
    owned = runtime.chat_sessions.owned_runs(owner_name="swarm", group_id="group")
    assert [(record.run_id, record.owner, record.address) for record in owned] == [
        (run.id, owner, SESSION)
    ]
    assert owned[0].terminal_status == "completed"
    summary = history(runtime)[-1]
    assert (summary.role, summary.run_id, summary.work_id) == (
        "run_summary",
        run.id,
        "sub-work-one",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cause", "resume", "consumed"),
    [
        ("process_restart", False, False),
        ("process_restart", True, True),
        ("network", True, False),
    ],
    ids=["not-resuming", "resuming", "not-a-restart"],
)
async def test_only_a_resuming_internal_run_consumes_a_restart_continuation(
    tmp_path: Path, cause: ContinuationCause, resume: bool, consumed: bool
) -> None:
    runtime = _runtime(tmp_path, [_answer("Verified")])
    session = await _interrupted_by_restart(runtime, cause)
    before = session.load_continuation()
    assert before is not None

    run = await build_chat_loop(runtime).start_run(
        "coder",
        "Verify the update and report",
        session_id="session-one",
        internal=True,
        resume_process_restart=resume,
    )
    await run.wait()

    # Any other internal Run leaves the checkpoint untouched for the next user Run.
    assert ("<continuation-checkpoint" in _sent_text(runtime)) is consumed
    after = session.load_continuation()
    if consumed:
        assert after is None
    else:
        assert after is not None
        assert (after.checkpoint_id, after.latest_run_id) == (
            before.checkpoint_id,
            before.latest_run_id,
        )


@pytest.mark.asyncio
async def test_run_commit_failure_preserves_output_and_recoverable_continuation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(tmp_path, [_answer("Verified")])
    session = await _interrupted_by_restart(runtime)

    async def fail_finish(*_args: Any) -> None:
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(tmp_path, [_answer()])
    await _interrupted_by_restart(runtime)

    async def fail_resolve(_self: ContinuationTracker) -> None:
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
    runtime = _runtime(tmp_path, [_answer("System work done")])

    run = await build_chat_loop(runtime).start_run(
        "coder",
        "internal note",
        session_id="session-one",
        internal=True,
        contributes_to_agent_activity=False,
        source_session_id="reviewed-session",
    )
    await run.wait()
    events = await runtime.timelines.events(run)

    assert run.source_session_id == "reviewed-session"
    persisted = history(runtime)
    assert [message.role for message in persisted] == ["note", "assistant", "run_summary"]
    assert persisted[-1].run_id == run.id
    assert persisted[-1].status == "completed"
    assert persisted[-1].iteration_count == 1
    assert events[-1].payload["iteration_count"] == 1
    activity = runtime.chat_sessions.list_summaries("coder")[0]
    assert activity["latest_completion_run_id"] is None
    assert activity["has_unread_completion"] is False
    assert all(event.contributes_to_agent_activity is False for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "error_kind", "streaming"),
    [
        (ProviderTimeoutError("private-provider-detail"), "timeout", False),
        (NetworkError("private-provider-detail"), "network_error", False),
        (StreamingChunkTimeoutError("private-provider-detail"), "timeout", True),
    ],
    ids=["provider-timeout", "network", "chunk-stall"],
)
async def test_provider_retry_is_visible_before_answer_without_leaking_error(
    tmp_path: Path,
    recovery_waits: list[float],
    failure: VBotError,
    error_kind: str,
    streaming: bool,
) -> None:
    streamed_answer = [
        {"type": "content_delta", "text": "done"},
        {"type": "finish", "reason": "stop"},
    ]
    runtime = _runtime(
        tmp_path, [failure, {"content": "done"}], stream_responses=[[failure], streamed_answer]
    )

    run = await build_chat_loop(runtime, streaming=streaming).start_run(
        "coder", "Hi", session_id="session-one"
    )
    await run.wait()
    events = await runtime.timelines.events(run)

    status = [event.payload for event in events if event.type == PROVIDER_REQUEST_STATUS_EVENT]
    retrying = [item for item in status if item["state"] == "retrying"]
    # A stalled stream announces its retry before the recovery backoff does.
    assert [item["state"] for item in status] == [
        "waiting",
        *["retrying"] * len(retrying),
        "waiting",
        "finished",
    ]
    assert {item["error_kind"] for item in retrying} == {error_kind}
    scheduled = next(item for item in retrying if "attempt" in item)
    assert (scheduled["attempt"], scheduled["max_attempts"]) == (2, 9)
    assert "private-provider-detail" not in str(status)
    assert len(recovery_waits) == 1
    assert run.iteration_count == 1
    assert all(message.role != "error" for message in history(runtime))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "error_kind", "expected", "retries"),
    [
        (RuntimeError("private-internal-detail"), "internal_error", False, 0),
        (ProviderTimeoutError("timeout sentinel"), "timeout", True, 8),
        (ProviderError("provider failed", retryable=False), "provider_fatal", True, 0),
    ],
    ids=["unexpected", "exhausted-retries", "fatal-provider-error"],
)
async def test_run_failures_remain_visible_in_history_once(
    tmp_path: Path,
    recovery_waits: list[float],
    caplog: pytest.LogCaptureFixture,
    failure: Exception,
    error_kind: str,
    expected: bool,
    retries: int,
) -> None:
    runtime = _runtime(tmp_path, [failure] * 9)

    run = await build_chat_loop(runtime).start_run("coder", "Hi", session_id="session-one")
    with pytest.raises(type(failure)) as raised:
        await run.wait()

    assert raised.value is failure
    errors = [message for message in history(runtime) if message.role == "error"]
    assert len(errors) == 1
    assert errors[0].error_kind == error_kind
    assert "private-internal-detail" not in str(errors[0].content)
    assert run.events[-1].payload["error_message_id"] == errors[0].id
    # One terminal line reports the failure and its retry count; attempts log at DEBUG.
    diagnostics = [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING and f"run={run.id}" in record.getMessage()
    ]
    assert len(diagnostics) == 1
    assert diagnostics[0].name == "vbot.runs"
    assert diagnostics[0].levelno == (logging.WARNING if expected else logging.ERROR)
    assert bool(diagnostics[0].exc_info) is not expected
    assert f"retries={retries}" in diagnostics[0].getMessage()


@pytest.mark.asyncio
async def test_preparation_failure_reaches_summary_and_completion_observers(
    tmp_path: Path,
) -> None:
    class BrokenTitles:
        def notify_user_message(self, **_kwargs: Any) -> None:
            raise RuntimeError("preparation sentinel")

    runtime = _runtime(tmp_path, [])
    reflection = RecordingReflection()
    loop = build_chat_loop(
        runtime, reflection_service=reflection, session_title_service=BrokenTitles()
    )

    run = await loop.start_run("coder", "Hi", session_id="session-one")
    with pytest.raises(RuntimeError):
        await run.wait()

    messages = history(runtime)
    assert (messages[-1].role, messages[-1].status) == ("run_summary", "failed")
    assert (messages[-2].role, messages[-2].error_kind) == ("error", "internal_error")
    assert reflection.calls[0]["outcome"] == "error"
    assert run.status is RunStatus.FAILED
    assert run.iteration_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_final_change_stats_allow_loop_progress_and_survive_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    runtime = _runtime(tmp_path, [{"content": "Done"}])
    tracker = ChangeTracker()
    runtime.change_tracker = tracker
    session = runtime.chat_sessions.get(SESSION)
    entered = asyncio.Event()
    release = threading.Event()
    event_loop = asyncio.get_running_loop()
    event_loop_thread = threading.get_ident()
    diff_threads: list[int] = []
    original_diff = change_tracker_module._line_diff_counts

    def slow_diff(before: str, after: str) -> tuple[int, int]:
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
            "file_stats": [{"path": str(tmp_path / "file.txt"), "added": 1, "removed": 1}],
        }
        messages = session.load()
        assert messages[-1].change_stats == run.terminal_payload_extras["change_stats"]
        assert session.load_continuation() is None
    finally:
        release.set()
        await runtime.chat_runs.aclose()
