"""Tests for durable provider-neutral Run continuation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage, ToolCall
from core.chat.continuation import (
    CONTINUATION_RECORD_VERSION,
    ContinuationTracker,
    fold_continuation_records,
    inject_continuation_reminder,
    normalize_interruption_cause,
    recover_continuation,
    render_continuation_reminder,
)
from core.chat.streaming import StreamingChunkTimeoutError
from core.providers.errors import NetworkError, ProviderTimeoutError
from core.sessions import ChatSession, ChatSessionManager
from core.utils.errors import ProviderError


def _record(record_type: str, run_id: str = "run-one", **fields: Any) -> dict[str, Any]:
    return {
        "version": CONTINUATION_RECORD_VERSION,
        "type": record_type,
        "run_id": run_id,
        "timestamp": "2026-07-11T12:00:00+00:00",
        **fields,
    }


class _ManualClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleepers: list[tuple[float, asyncio.Future[None]]] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        future = asyncio.get_running_loop().create_future()
        self.sleepers.append((self.now + delay, future))
        await future

    def advance(self, seconds: float) -> None:
        self.now += seconds
        remaining: list[tuple[float, asyncio.Future[None]]] = []
        for due, future in self.sleepers:
            if future.done():
                continue
            if due <= self.now:
                future.set_result(None)
            else:
                remaining.append((due, future))
        self.sleepers = remaining


def _session(tmp_path: Path, *, session_id: str = "session") -> ChatSession:
    return ChatSessionManager(tmp_path).create("agent", session_id=session_id)


@pytest.mark.asyncio
async def test_ten_trackers_coalesce_many_deltas_to_one_periodic_flush_each(
    tmp_path: Path,
) -> None:
    clock = _ManualClock()
    batches: list[list[list[dict[str, Any]]]] = [[] for _ in range(10)]
    trackers: list[ContinuationTracker] = []
    sessions = ChatSessionManager(tmp_path)
    for index in range(10):
        session = sessions.create("agent", session_id=f"session-{index}")

        def sink(records: list[dict[str, Any]], *, index: int = index) -> None:
            batches[index].append(records)

        tracker = ContinuationTracker(
            session,
            run_id=f"run-{index}",
            request="work",
            record_sink=sink,
            clock=clock,
            sleep=clock.sleep,
        )
        await tracker.start()
        trackers.append(tracker)
        for _ in range(100):
            tracker.record_stream_delta(reasoning="r", content="c")

    await asyncio.sleep(0)
    clock.advance(1.99)
    await asyncio.sleep(0)
    assert all(len(run_batches) == 1 for run_batches in batches)

    clock.advance(0.01)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert all(len(run_batches) == 2 for run_batches in batches)
    assert all(run_batches[1][0]["type"] == "stream_delta" for run_batches in batches)

    for tracker in trackers:
        for _ in range(100):
            tracker.record_stream_delta(content="more")
        await tracker.record_assistant_boundary(
            message_id="assistant",
            reasoning="r" * 100,
            content="c" * 100,
            interrupted=False,
        )
    assert all(len(run_batches) == 3 for run_batches in batches)
    assert all(
        [record["type"] for record in run_batches[2]] == ["stream_delta", "assistant_boundary"]
        for run_batches in batches
    )
    await asyncio.gather(*(tracker.resolve() for tracker in trackers))


@pytest.mark.asyncio
async def test_boundary_timer_cancellation_cannot_lose_next_dirty_flush(tmp_path: Path) -> None:
    clock = _ManualClock()
    batches: list[list[dict[str, Any]]] = []
    session = _session(tmp_path)
    tracker = ContinuationTracker(
        session,
        run_id="run-one",
        request="work",
        record_sink=batches.append,
        clock=clock,
        sleep=clock.sleep,
    )
    await tracker.start()
    tracker.record_stream_delta(reasoning="before")
    await asyncio.sleep(0)

    await tracker.record_assistant_boundary(
        message_id="assistant-one",
        reasoning="before",
        content=None,
        interrupted=False,
    )
    tracker.record_stream_delta(reasoning="after")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    clock.advance(2.0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert batches[-1][0]["type"] == "stream_delta"
    assert batches[-1][0]["reasoning_delta"] == "after"
    await tracker.resolve()


@pytest.mark.asyncio
async def test_periodic_flush_cannot_land_after_its_assistant_boundary(tmp_path: Path) -> None:
    batches: list[list[dict[str, Any]]] = []
    stream_write_started = asyncio.Event()
    release_stream_write = asyncio.Event()

    async def sink(records: list[dict[str, Any]]) -> None:
        if records[0]["type"] == "stream_delta":
            stream_write_started.set()
            await release_stream_write.wait()
        batches.append(records)

    tracker = ContinuationTracker(
        _session(tmp_path),
        run_id="run-one",
        request="work",
        record_sink=sink,
        flush_interval=0,
    )
    await tracker.start()
    tracker.record_stream_delta(reasoning="before")
    await stream_write_started.wait()

    boundary = asyncio.create_task(
        tracker.record_assistant_boundary(
            message_id="assistant-one",
            reasoning="before",
            content=None,
            interrupted=False,
        )
    )
    await asyncio.sleep(0)
    assert not boundary.done()

    release_stream_write.set()
    await boundary
    assert [batch[0]["type"] for batch in batches] == [
        "run_started",
        "stream_delta",
        "assistant_boundary",
    ]
    await tracker.resolve()


def test_fold_discards_replayed_attempt_before_accepting_replacement_delta() -> None:
    records = [
        _record(
            "run_started",
            checkpoint_id="checkpoint",
            origin_run_id="run-one",
            request="work",
        ),
        _record(
            "stream_delta",
            step=1,
            reasoning_delta="discarded",
            content_delta="",
        ),
        _record("stream_attempt_discarded", step=1),
        _record(
            "stream_delta",
            step=1,
            reasoning_delta="replacement",
            content_delta="",
        ),
        _record("run_interrupted", cause="network"),
    ]

    state = fold_continuation_records(records)

    assert state is not None
    assert state.reasoning == "replacement"


def test_fold_preserves_chain_across_repeated_interruptions() -> None:
    records = [
        _record(
            "run_started",
            checkpoint_id="checkpoint",
            origin_run_id="run-one",
            request="first request",
        ),
        _record("stream_delta", step=1, reasoning_delta="plan", content_delta="partial"),
        _record("run_interrupted", cause="network"),
        _record(
            "run_started",
            run_id="run-two",
            checkpoint_id="checkpoint",
            origin_run_id="run-one",
            request=None,
        ),
        _record(
            "stream_delta",
            run_id="run-two",
            step=1,
            reasoning_delta="continued plan",
            content_delta="",
        ),
        _record(
            "run_interrupted",
            run_id="run-two",
            cause="timeout",
        ),
    ]

    state = fold_continuation_records(records)

    assert state is not None
    assert state.origin_run_id == "run-one"
    assert state.latest_run_id == "run-two"
    assert state.cause == "timeout"
    assert state.reasoning == "plan\n\ncontinued plan"


@pytest.mark.parametrize(
    ("error", "cause"),
    [
        (NetworkError("offline"), "network"),
        (ProviderTimeoutError("slow"), "timeout"),
        (StreamingChunkTimeoutError("stalled"), "timeout"),
        (ProviderError("rejected", retryable=False), "provider"),
        (RuntimeError("bug"), "internal"),
    ],
)
def test_normalizes_all_post_admission_interruption_causes(
    error: BaseException,
    cause: str,
) -> None:
    assert normalize_interruption_cause(error) == cause


def test_prompt_warns_before_repeating_unknown_write_edit_or_bash() -> None:
    records = [
        _record(
            "run_started",
            checkpoint_id="checkpoint",
            origin_run_id="run-one",
            request="change the repository",
        ),
        _record("tool_started", tool_call_id="write-1", name="write"),
        _record("tool_started", tool_call_id="read-1", name="read"),
        _record("run_interrupted", cause="process_restart"),
    ]
    state = fold_continuation_records(records)
    assert state is not None

    reminder = render_continuation_reminder(state, context_window=32_000)

    assert "Their actual filesystem or process effects may be uncertain." in reminder
    assert "write (write-1)" in reminder
    assert "read (read-1): unknown" in reminder


def test_fold_references_ten_completed_tools_and_keeps_one_dangling_unknown() -> None:
    records = [
        _record(
            "run_started",
            checkpoint_id="checkpoint",
            origin_run_id="run-one",
            request="work",
        )
    ]
    for index in range(10):
        records.extend(
            [
                _record(
                    "tool_started",
                    tool_call_id=f"read-{index}",
                    name="read",
                ),
                _record(
                    "tool_result",
                    tool_call_id=f"read-{index}",
                    name="read",
                    ok=True,
                ),
            ]
        )
    records.extend(
        [
            _record("tool_started", tool_call_id="edit-dangling", name="edit"),
            _record("run_interrupted", cause="process_restart"),
        ]
    )

    state = fold_continuation_records(records)

    assert state is not None
    assert len(state.operations) == 11
    assert sum(operation["status"] == "completed" for operation in state.operations.values()) == 10
    assert state.operations["edit-dangling"]["status"] == "unknown"
    assert (
        "Their actual filesystem or process effects may be uncertain."
        in render_continuation_reminder(
            state,
            context_window=32_000,
        )
    )


def test_prompt_truncation_keeps_original_request_operations_warning_and_marker() -> None:
    records = [
        _record(
            "run_started",
            checkpoint_id="checkpoint",
            origin_run_id="run-one",
            request="ORIGINAL REQUEST",
        ),
        _record(
            "stream_delta",
            step=1,
            reasoning_delta="old plan " * 2_000,
            content_delta="partial",
        ),
        _record("tool_started", tool_call_id="bash-1", name="bash"),
        _record("run_interrupted", cause="internal"),
    ]
    state = fold_continuation_records(records)
    assert state is not None

    reminder = render_continuation_reminder(state, context_window=4_000)

    assert len(reminder) <= 4_000
    assert "ORIGINAL REQUEST" in reminder
    assert "bash-1" in reminder
    assert "SAFETY:" in reminder
    assert "truncated to fit" in reminder


def test_reminder_neutrally_describes_interruption_without_directing_model() -> None:
    state = fold_continuation_records(
        [
            _record(
                "run_started",
                checkpoint_id="checkpoint",
                origin_run_id="run-one",
                request="Original request",
            ),
            _record("stream_delta", step=1, reasoning_delta="Recorded plan"),
            _record("run_interrupted", cause="user"),
        ]
    )
    assert state is not None

    reminder = render_continuation_reminder(state, context_window=32_000)

    assert (
        "The previous Run was interrupted. "
        "The checkpoint below records what happened before the interruption."
    ) in reminder
    assert "Resume the interrupted work" not in reminder
    assert "Treat canonical Tool Calls" not in reminder


def test_injection_places_reminder_immediately_before_new_turn_and_deduplicates() -> None:
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "correction"},
    ]

    injected = inject_continuation_reminder(
        messages,
        '<continuation-checkpoint id="one">state</continuation-checkpoint>',
    )
    reinjected = inject_continuation_reminder(
        injected,
        '<continuation-checkpoint id="one">state</continuation-checkpoint>',
    )

    assert reinjected[-1]["content"] == "correction"
    assert "continuation-checkpoint" in reinjected[-2]["content"]
    assert (
        sum("continuation-checkpoint" in str(message.get("content")) for message in reinjected) == 1
    )


@pytest.mark.asyncio
async def test_recover_classifies_abandoned_journal_as_process_restart(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.append_continuation_record(
        _record(
            "run_started",
            checkpoint_id="checkpoint",
            origin_run_id="run-one",
            request="work",
        )
    )

    state = await recover_continuation(session)

    assert state is not None
    assert state.cause == "process_restart"


@pytest.mark.asyncio
async def test_restart_reconciliation_uses_only_current_transcript_tail(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.append(ChatMessage.user("old work"))
    session.append(
        ChatMessage.assistant(
            model="test/model",
            content=None,
            tool_calls=[ToolCall(id="old-edit", name="edit")],
        )
    )
    session.append(
        ChatMessage.run_summary(
            run_id="old-run",
            status="completed",
            iteration_count=1,
            timing={
                "started_at": "2026-07-11T11:00:00+00:00",
                "completed_at": "2026-07-11T11:00:01+00:00",
                "duration_ms": 1_000,
            },
        )
    )
    session.append(ChatMessage.user("new work"))
    session.append(
        ChatMessage.assistant(
            model="test/model",
            content=None,
            tool_calls=[ToolCall(id="new-bash", name="bash")],
        )
    )
    session.append(
        ChatMessage.tool(
            tool_call_id="new-bash",
            name="bash",
            content='{"ok":true}',
        )
    )
    session.append_continuation_record(
        _record(
            "run_started",
            checkpoint_id="checkpoint",
            origin_run_id="new-run",
            run_id="new-run",
            request="new work",
        )
    )

    state = await recover_continuation(session)

    assert state is not None
    assert set(state.operations) == {"new-bash"}
    assert state.operations["new-bash"]["status"] == "completed"


@pytest.mark.asyncio
async def test_recover_clears_stale_journal_when_transcript_proves_normal_completion(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    session.append(ChatMessage.user("work"))
    session.append(ChatMessage.assistant(model="test/model", content="done"))
    session.append(
        ChatMessage.run_summary(
            run_id="run-one",
            status="completed",
            iteration_count=1,
            timing={
                "started_at": "2026-07-11T12:00:00+00:00",
                "completed_at": "2026-07-11T12:00:01+00:00",
                "duration_ms": 1_000,
            },
        )
    )
    session.append_continuation_record(
        _record(
            "run_started",
            checkpoint_id="checkpoint",
            origin_run_id="run-one",
            request="work",
        )
    )

    assert await recover_continuation(session) is None
    assert session.load_continuation_records() == []
