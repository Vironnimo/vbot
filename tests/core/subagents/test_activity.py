"""Tests for the live activity file of one Sub-Agent Session."""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage
from core.runs import (
    ASSISTANT_OUTPUT_DELTA_EVENT,
    ASSISTANT_OUTPUT_EVENT,
    REASONING_EVENT,
    TOOL_CALL_OUTPUT_EVENT,
    TOOL_CALL_RESULT_EVENT,
    TOOL_CALL_STARTED_EVENT,
    USER_MESSAGE_EVENT,
    Run,
    RunInterruptedError,
)
from core.storage import TemporaryFileManager
from core.subagents import activity as activity_module
from core.subagents.activity import SubAgentActivity

# The note that marks activity which never reached the file.
MISSING_NOTE = activity_module._MISSING_ACTIVITY_NOTE.strip()


async def _create(tmp_path: Path) -> SubAgentActivity:
    activity = await SubAgentActivity.create(
        TemporaryFileManager(tmp_path),
        agent_id="worker",
        session_id="child-session",
    )
    assert activity is not None
    return activity


def _child_run(**options: Any) -> Run:
    return Run(run_id="child-run", agent_id="worker", session_id="child-session", **options)


async def _written(activity: SubAgentActivity) -> str:
    """Return the file once the ended Run's outcome and all earlier text are on disk."""
    await activity.drain()
    return activity.path.read_text(encoding="utf-8")


async def _turns() -> None:
    """Let the watcher handle what was emitted and a due flush hand it off."""
    for _ in range(5):
        await asyncio.sleep(0)


@contextmanager
def _busy_writer() -> Iterator[None]:
    """Keep the activity writer thread on one pending chunk until the block ends."""
    release = threading.Event()

    def hold() -> None:
        release.wait(timeout=10)

    assert activity_module._WRITER.hand_off(hold, limit=sys.maxsize)
    try:
        yield
    finally:
        release.set()


@pytest.mark.asyncio
async def test_activity_streams_assistant_and_safe_tool_summary_without_duplicates(
    tmp_path: Path,
) -> None:
    activity = await _create(tmp_path)
    run = _child_run()
    activity.follow(run)
    # The follower subscribes on its first step, while the child Run still works.
    await asyncio.sleep(0)

    run.emit(USER_MESSAGE_EVENT, {"message": {"content": "private user prompt"}})
    run.emit(REASONING_EVENT, {"message": {"reasoning": "hidden chain"}})
    run.emit(ASSISTANT_OUTPUT_DELTA_EVENT, {"content_delta": "Hello"})
    run.emit(ASSISTANT_OUTPUT_DELTA_EVENT, {"content_delta": " world"})
    run.emit(
        ASSISTANT_OUTPUT_EVENT,
        {"message": ChatMessage.assistant(model="test", content="Hello world").to_dict()},
    )
    run.emit(
        TOOL_CALL_STARTED_EVENT,
        {
            "tool_call": {
                "id": "call-one",
                "name": "read",
                "arguments": {"path": "secret-argument.txt"},
            },
            "display": {"summary": "notes.md", "hidden_argument_keys": ["path"]},
        },
    )
    run.emit(TOOL_CALL_OUTPUT_EVENT, {"content_delta": "secret tool stdout"})
    run.emit(
        TOOL_CALL_RESULT_EVENT,
        {
            "tool_call": {"id": "call-one", "name": "read"},
            "result": {"ok": True, "data": {"content": "secret tool result"}},
        },
    )
    run.mark_completed(ChatMessage.assistant(model="test", content="Hello world"))

    text = await _written(activity)

    assert text.rstrip().endswith("completed (`child-run`)")
    assert text.count("Hello world") == 1
    assert "`read` started — notes.md" in text
    assert "`read` completed" in text
    assert "private user prompt" not in text
    assert "hidden chain" not in text
    assert "secret-argument.txt" not in text
    assert "secret tool stdout" not in text
    assert "secret tool result" not in text


@pytest.mark.asyncio
async def test_activity_copies_non_streaming_assistant_output_and_failed_tool_state(
    tmp_path: Path,
) -> None:
    activity = await _create(tmp_path)
    run = _child_run()
    activity.follow(run)
    await asyncio.sleep(0)
    run.emit(
        ASSISTANT_OUTPUT_EVENT,
        {"message": ChatMessage.assistant(model="test", content="One-shot answer").to_dict()},
    )
    run.emit(
        TOOL_CALL_RESULT_EVENT,
        {
            "tool_call": {"id": "call-two", "name": "bash"},
            "result": {"ok": False, "error": {"message": "private failure body"}},
        },
    )
    run.mark_failed(RuntimeError("provider internals"))

    text = await _written(activity)

    assert text.rstrip().endswith("failed (`child-run`)")
    assert text.count("One-shot answer") == 1
    assert "`bash` failed" in text
    assert "private failure body" not in text
    assert "provider internals" not in text


@pytest.mark.asyncio
async def test_activity_records_interrupted_terminal_status(tmp_path: Path) -> None:
    activity = await _create(tmp_path)
    run = _child_run()
    activity.follow(run)

    run.mark_interrupted(RunInterruptedError("network"))

    text = await _written(activity)
    assert "Run status" in text
    assert text.rstrip().endswith("interrupted (`child-run`)")


@pytest.mark.asyncio
async def test_activity_writes_in_order_without_waiting_for_the_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(activity_module, "_FLUSH_INTERVAL_SECONDS", 0.0)
    activity = await _create(tmp_path)
    run = _child_run()

    with _busy_writer():
        activity.follow(run)
        await _turns()
        # Each step is handed to the writer as its own chunk.
        for word in ("alpha", "beta", "gamma"):
            run.emit(ASSISTANT_OUTPUT_DELTA_EVENT, {"content_delta": f"{word} "})
            await _turns()
        run.emit(TOOL_CALL_STARTED_EVENT, {"tool_call": {"id": "call-one", "name": "read"}})
        await _turns()
        run.emit(
            TOOL_CALL_RESULT_EVENT,
            {"tool_call": {"id": "call-one", "name": "read"}, "result": {"ok": True}},
        )
        await _turns()
        run.emit(
            ASSISTANT_OUTPUT_EVENT,
            {"message": ChatMessage.assistant(model="test", content="final answer").to_dict()},
        )
        run.mark_completed(ChatMessage.assistant(model="test", content="final answer"))
        await _turns()
        # The Run ran to its end while the writer had not written anything.
        assert "Run status" not in activity.path.read_text(encoding="utf-8")

    text = await _written(activity)

    in_file_order = [
        "running (`child-run`)",
        "alpha beta gamma",
        "`read` started",
        "`read` completed",
        "final answer",
        "completed (`child-run`)",
    ]
    positions = [text.find(part) for part in in_file_order]
    assert -1 not in positions, text
    assert positions == sorted(positions), text


@pytest.mark.asyncio
async def test_writer_backlog_drops_assistant_text_but_keeps_headings_tools_and_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(activity_module, "_FLUSH_INTERVAL_SECONDS", 0.0)
    # The busy writer's pending chunk alone fills the backlog.
    monkeypatch.setattr(activity_module, "_PENDING_WRITE_LIMIT", 1)
    activity = await _create(tmp_path)
    run = _child_run()

    with _busy_writer():
        activity.follow(run)
        await _turns()
        # A section's first chunk carries its heading and is written in full.
        run.emit(ASSISTANT_OUTPUT_DELTA_EVENT, {"content_delta": "Streaming"})
        await _turns()
        run.emit(ASSISTANT_OUTPUT_DELTA_EVENT, {"content_delta": " dropped text"})
        await _turns()
        run.emit(TOOL_CALL_STARTED_EVENT, {"tool_call": {"id": "call-one", "name": "read"}})
        await _turns()
        run.mark_completed(ChatMessage.assistant(model="test", content="Streaming"))

    text = await _written(activity)

    assert "dropped text" not in text
    in_file_order = [
        "running (`child-run`)",
        "— Assistant",
        "Streaming",
        MISSING_NOTE,
        "— Tool",
        "`read` started",
        "completed (`child-run`)",
    ]
    positions = [text.find(part) for part in in_file_order]
    assert -1 not in positions, text
    assert positions == sorted(positions), text
    assert text.rstrip().endswith("completed (`child-run`)")


@pytest.mark.asyncio
async def test_activity_records_the_outcome_after_the_run_evicted_its_watcher(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="vbot.subagents.activity")
    activity = await _create(tmp_path)
    run = _child_run(subscriber_queue_limit=1)
    activity.follow(run)
    await asyncio.sleep(0)

    # Two events before the watcher's next step overflow its queue: the Run
    # evicts it and its event stream ends while the Run keeps working.
    run.emit(ASSISTANT_OUTPUT_DELTA_EVENT, {"content_delta": "early"})
    run.emit(ASSISTANT_OUTPUT_DELTA_EVENT, {"content_delta": "lost"})
    await _turns()
    run.mark_cancelled()

    text = await _written(activity)

    assert "lost" not in text
    assert MISSING_NOTE in text
    assert text.rstrip().endswith("cancelled (`child-run`)")
    assert [
        record.levelno for record in caplog.records if record.name == "vbot.subagents.activity"
    ] == [logging.WARNING]


@pytest.mark.asyncio
async def test_activity_write_failure_does_not_change_run_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activity = await _create(tmp_path)
    original_open = Path.open

    def fail_activity_append(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == activity.path and args and args[0] == "a":
            raise OSError("disk unavailable")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_activity_append)
    run = _child_run()
    activity.follow(run)
    expected = ChatMessage.assistant(model="test", content="canonical result")
    run.mark_completed(expected)

    assert await run.wait() is expected
    await activity.drain()


@pytest.mark.asyncio
async def test_one_file_records_each_followed_run_in_start_order(tmp_path: Path) -> None:
    temporary_files = TemporaryFileManager(tmp_path)
    activity = await SubAgentActivity.create(
        temporary_files, agent_id="worker", session_id="child-session"
    )
    assert activity is not None
    first = Run(run_id="first-run", agent_id="worker", session_id="child-session")
    second = Run(run_id="second-run", agent_id="worker", session_id="child-session")
    activity.follow(first)
    activity.follow(second)
    second.mark_completed(ChatMessage.assistant(model="test", content="second answer"))
    first.mark_completed(ChatMessage.assistant(model="test", content="first answer"))
    await activity.drain()

    text = activity.path.read_text(encoding="utf-8")
    assert text.index("completed (`first-run`)") < text.index("running (`second-run`)")
    assert text.rstrip().endswith("completed (`second-run`)")
    # Between Runs the file is no longer protected, so retention can collect it;
    # the next Run protects it again.
    assert activity.path not in temporary_files._active  # noqa: SLF001
    third = Run(run_id="third-run", agent_id="worker", session_id="child-session")
    activity.follow(third)
    await _turns()
    await activity_module._WRITER.drain()  # noqa: SLF001
    assert activity.path in {path.resolve() for path in temporary_files._active}  # noqa: SLF001
    third.mark_completed(ChatMessage.assistant(model="test", content="third"))
    await activity.drain()
