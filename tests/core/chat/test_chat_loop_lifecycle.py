"""Chat-loop tests grouped by lifecycle."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.core.chat.chat_loop_support import (
    RecordingReflection,
    StubAdapter,
    StubAgent,
    StubRuntime,
    build_chat_loop,
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
