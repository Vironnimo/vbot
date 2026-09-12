"""Tests for subagents titles."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.chat import ChatMessage
from core.subagents.tracker import SubAgentBatchTracker
from tests.core.subagents.subagents_test_support import (
    FakeRunManager,
    RecordingTriggerService,
    _address,
    _handle_subagent,
    make_context,
    make_runtime,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("current_format_data_directory")]


async def test_new_subagent_session_uses_description_as_automatic_title(tmp_path: Path) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()
    description = "A" * 80

    result = await _handle_subagent(
        context,
        {
            "content": "Inspect the Tool contract and report concise findings.",
            "description": description,
            "agent_id": "worker",
        },
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert result["ok"] is True
    child_session_id = result["data"]["session_id"]
    metadata = runtime.chat_sessions.get_metadata(_address("worker", child_session_id))
    assert metadata["auto_title"] == "A" * 48
    assert metadata["auto_title_initialized"] is True
    assert runtime.chat_sessions.list_with_metadata("worker")[0]["auto_title"] == "A" * 48
    manager.started[0]["run"].mark_completed(
        ChatMessage.assistant(model="openai/gpt-5.2", content="done")
    )
    await asyncio.sleep(0)


async def test_new_subagent_session_title_falls_back_to_normalized_content(tmp_path: Path) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()
    content = "  Inspect   session\ntitles and report the complete implementation outcome safely  "

    result = await _handle_subagent(
        context,
        {"content": content, "description": "   ", "agent_id": "worker"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert result["ok"] is True
    child_session_id = result["data"]["session_id"]
    metadata = runtime.chat_sessions.get_metadata(_address("worker", child_session_id))
    assert metadata["auto_title"] == "Inspect session titles and report the complete i"
    assert len(metadata["auto_title"]) == 48
    manager.started[0]["run"].mark_completed(
        ChatMessage.assistant(model="openai/gpt-5.2", content="done")
    )
    await asyncio.sleep(0)


async def test_continued_subagent_session_keeps_existing_titles(tmp_path: Path) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()
    runtime.chat_sessions.create("worker", session_id="existing")
    runtime.chat_sessions.set_auto_title(_address("worker", "existing"), "Original automatic title")
    runtime.chat_sessions.set_title(_address("worker", "existing"), "Manual session title")

    result = await _handle_subagent(
        context,
        {
            "content": "Now verify the remaining edge case.",
            "description": "Verify remaining edge case",
            "agent_id": "worker",
            "session_id": "existing",
        },
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert result["ok"] is True
    metadata = runtime.chat_sessions.get_metadata(_address("worker", "existing"))
    assert metadata["auto_title"] == "Original automatic title"
    assert metadata["title"] == "Manual session title"
    manager.started[0]["run"].mark_completed(
        ChatMessage.assistant(model="openai/gpt-5.2", content="done")
    )
    await asyncio.sleep(0)


async def test_subagent_rejects_non_string_description(tmp_path: Path) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())

    result = await _handle_subagent(
        make_context(),
        {"content": "spawn", "description": 123, "agent_id": "worker"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert result["error"] == {
        "code": "invalid_arguments",
        "message": "description must be a string",
    }
    assert manager.started == []
