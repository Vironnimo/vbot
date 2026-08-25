"""Chat-loop tests grouped by lifecycle."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.chat.continuation import ContinuationTracker
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
        agent_id="coder",
        session_id="session-one",
        executor=loop.run_executor("Do work"),
        project_id=None,
        work_id="sub-work-one",
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
