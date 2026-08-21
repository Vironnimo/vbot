"""Tests for live Sub-Agent activity-file projection."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage
from core.runs import (
    ASSISTANT_OUTPUT_DELTA_EVENT,
    ASSISTANT_OUTPUT_EVENT,
    REASONING_EVENT,
    TOOL_CALL_RESULT_EVENT,
    TOOL_CALL_STARTED_EVENT,
    TOOL_CALL_STDOUT_EVENT,
    USER_MESSAGE_EVENT,
    Run,
    RunInterruptedError,
)
from core.storage import TemporaryFileManager
from core.subagents.activity import SubAgentActivity


async def _wait_for_text(path: Path, expected: str) -> str:
    for _ in range(50):
        text = path.read_text(encoding="utf-8")
        if expected in text:
            return text
        await asyncio.sleep(0.01)
    raise AssertionError(f"{expected!r} did not appear in {path}")


@pytest.mark.asyncio
async def test_activity_streams_assistant_and_safe_tool_summary_without_duplicates(
    tmp_path: Path,
) -> None:
    activity = SubAgentActivity.create(
        TemporaryFileManager(tmp_path),
        agent_id="worker",
        session_id="child-session",
    )
    assert activity is not None
    run = Run(run_id="child-run", agent_id="worker", session_id="child-session")
    activity.attach(run)

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
    run.emit(TOOL_CALL_STDOUT_EVENT, {"content_delta": "secret tool stdout"})
    run.emit(
        TOOL_CALL_RESULT_EVENT,
        {
            "tool_call": {"id": "call-one", "name": "read"},
            "result": {"ok": True, "data": {"content": "secret tool result"}},
        },
    )
    run.mark_completed(ChatMessage.assistant(model="test", content="Hello world"))

    text = await _wait_for_text(activity.path, "completed (`child-run`)")

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
    activity = SubAgentActivity.create(
        TemporaryFileManager(tmp_path),
        agent_id="worker",
        session_id="child-session",
    )
    assert activity is not None
    run = Run(run_id="child-run", agent_id="worker", session_id="child-session")
    activity.attach(run)
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

    text = await _wait_for_text(activity.path, "failed (`child-run`)")

    assert text.count("One-shot answer") == 1
    assert "`bash` failed" in text
    assert "private failure body" not in text
    assert "provider internals" not in text


@pytest.mark.asyncio
async def test_activity_records_interrupted_terminal_status(tmp_path: Path) -> None:
    activity = SubAgentActivity.create(
        TemporaryFileManager(tmp_path),
        agent_id="worker",
        session_id="child-session",
    )
    assert activity is not None
    run = Run(run_id="child-run", agent_id="worker", session_id="child-session")
    activity.attach(run)

    run.mark_interrupted(RunInterruptedError("network"))

    text = await _wait_for_text(activity.path, "interrupted (`child-run`)")
    assert "Run status" in text


@pytest.mark.asyncio
async def test_activity_write_failure_does_not_change_run_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activity = SubAgentActivity.create(
        TemporaryFileManager(tmp_path),
        agent_id="worker",
        session_id="child-session",
    )
    assert activity is not None
    original_open = Path.open

    def fail_activity_append(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == activity.path and args and args[0] == "a":
            raise OSError("disk unavailable")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_activity_append)
    run = Run(run_id="child-run", agent_id="worker", session_id="child-session")
    activity.attach(run)
    expected = ChatMessage.assistant(model="test", content="canonical result")
    run.mark_completed(expected)

    assert await run.wait() is expected
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_attach_keeps_watch_task_reference_until_run_completes(tmp_path: Path) -> None:
    activity = SubAgentActivity.create(
        TemporaryFileManager(tmp_path),
        agent_id="worker",
        session_id="child-session",
    )
    assert activity is not None
    run = Run(run_id="child-run", agent_id="worker", session_id="child-session")
    activity.attach(run)

    watch_task = activity._watch_task
    assert watch_task is not None
    assert not watch_task.done()

    run.mark_completed(ChatMessage.assistant(model="test", content="done"))
    await watch_task

    assert "completed (`child-run`)" in activity.path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_attach_twice_starts_a_single_watcher(tmp_path: Path) -> None:
    activity = SubAgentActivity.create(
        TemporaryFileManager(tmp_path),
        agent_id="worker",
        session_id="child-session",
    )
    assert activity is not None
    run = Run(run_id="child-run", agent_id="worker", session_id="child-session")
    activity.attach(run)
    first_watcher = activity._watch_task
    assert first_watcher is not None

    activity.attach(run)

    assert activity._watch_task is first_watcher
    run.mark_completed(ChatMessage.assistant(model="test", content="done"))
    await first_watcher
