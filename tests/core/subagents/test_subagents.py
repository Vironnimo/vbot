"""Tests for subagents."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.chat import ChatMessage
from core.projects import AgentResolutionError
from core.subagents.subagents import _handle_subagent as _handle_subagent_impl
from core.subagents.tracker import SubAgentBatchTracker
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


async def test_project_subagent_session_lives_under_project_anchor(tmp_path: Path) -> None:
    # Arrange: a parent run scoped to project "acme".
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(project_id="acme")

    # Act
    result = await _handle_subagent(
        context,
        {"content": "spawn", "agent_id": "worker"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert: the child Session was created in the project scope, never in the
    # identity scope.
    assert result["ok"] is True
    child_session_id = result["data"]["session_id"]
    assert runtime.chat_sessions.exists(_address("worker", child_session_id, "acme"))
    assert not runtime.chat_sessions.exists(_address("worker", child_session_id))


async def test_project_subagent_run_carries_project_id(tmp_path: Path) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(project_id="acme")

    # Act
    result = await _handle_subagent(
        context,
        {"content": "spawn", "agent_id": "worker"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert: the parent project reaches start(), and rides the created child
    # Run (run.project_id) so its session I/O is project-scoped.
    assert result["ok"] is True
    assert manager.started[0]["project_id"] == "acme"
    assert manager.started[0]["run"].project_id == "acme"


async def test_identity_parent_can_spawn_qualified_project_agent(tmp_path: Path) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    emitted_events: list[tuple[str, JsonObject]] = []
    context = make_context(
        project_id=None,
        emit_hook=lambda event_type, payload: emitted_events.append((event_type, payload)),
    )

    result = await _handle_subagent(
        context,
        {"content": "spawn", "agent_id": "worker@vbot"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert result["ok"] is True
    assert result["data"]["agent_id"] == "worker"
    assert result["data"]["project_id"] == "vbot"
    assert runtime.agent_resolver.calls[-1] == ("vbot", "worker")
    assert manager.started[0]["project_id"] == "vbot"
    assert manager.parent_run.project_id is None
    assert emitted_events[0][1]["data"]["project_id"] == "vbot"
    child_session_id = result["data"]["session_id"]
    assert runtime.chat_sessions.get(_address("worker", child_session_id, "vbot"))
    metadata = runtime.chat_sessions.get_metadata(_address("worker", child_session_id, "vbot"))
    assert metadata["subagent_parent"]["project_id"] is None
    manager.started[0]["run"].mark_completed(
        ChatMessage.assistant(model="openai/gpt-5.2", content="done")
    )
    await asyncio.sleep(0)


async def test_identity_parent_explicit_targets_use_canonical_addresses(tmp_path: Path) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(
        project_id=None,
        allowed_agents=["worker@vbot"],
    )

    denied = await _handle_subagent(
        context,
        {"content": "spawn", "agent_id": "worker"},
        runtime=runtime,
        batch_tracker=tracker,
    )
    allowed = await _handle_subagent(
        context,
        {"content": "spawn", "agent_id": "worker@vbot"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert denied["ok"] is False
    assert denied["error"]["code"] == "agent_not_allowed"
    assert allowed["ok"] is True
    assert allowed["data"]["project_id"] == "vbot"
    assert len(manager.started) == 1
    manager.started[0]["run"].mark_completed(
        ChatMessage.assistant(model="openai/gpt-5.2", content="done")
    )
    await asyncio.sleep(0)


async def test_empty_additional_target_policy_rejects_other_agent_but_allows_self(
    tmp_path: Path,
) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(allowed_agents=[])

    denied = await _handle_subagent(
        context,
        {"content": "spawn", "agent_id": "worker"},
        runtime=runtime,
        batch_tracker=tracker,
    )
    allowed = await _handle_subagent(
        context,
        {"content": "self spawn"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert denied["ok"] is False
    assert denied["error"]["code"] == "agent_not_allowed"
    assert allowed["ok"] is True
    assert allowed["data"]["agent_id"] == "parent"
    assert runtime.agent_resolver.calls == [(None, "parent"), (None, "parent")]
    assert len(manager.started) == 1
    assert not (tmp_path / "agents" / "worker" / "sessions").exists()
    manager.started[0]["run"].mark_completed(
        ChatMessage.assistant(model="openai/gpt-5.2", content="done")
    )
    await asyncio.sleep(0)


async def test_project_parent_cannot_spawn_qualified_agent_in_another_project(
    tmp_path: Path,
) -> None:
    manager = FakeRunManager()
    manager.parent_run.project_id = "acme"
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(project_id="acme")

    result = await _handle_subagent(
        context,
        {"content": "spawn", "agent_id": "worker@vbot"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "agent_not_allowed"
    assert runtime.agent_resolver.calls == []
    assert manager.started == []
    assert manager.parent_run.project_id == "acme"


async def test_project_subagent_parent_link_metadata_carries_project_id(
    tmp_path: Path,
) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(project_id="acme")

    # Act
    result = await _handle_subagent(
        context,
        {"content": "spawn", "agent_id": "worker"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert: the durable parent link in the child session metadata records the
    # project id so the child session stays addressable after a restart. The
    # metadata is read back under the same project anchor.
    assert result["ok"] is True
    child_session_id = result["data"]["session_id"]
    metadata = runtime.chat_sessions.get_metadata(_address("worker", child_session_id, "acme"))
    assert metadata["is_subagent_session"] is True
    assert metadata["subagent_parent"] == {
        "id": result["data"]["id"],
        "agent_id": "parent",
        "session_id": "parent-session",
        "run_id": "parent-run",
        "tool_call_id": "tool-call-one",
        "tool_call_index": 0,
        "project_id": "acme",
    }


async def test_identity_subagent_session_unchanged_and_link_project_is_none(
    tmp_path: Path,
) -> None:
    # Arrange: an identity parent run (no project).
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(project_id=None)

    # Act
    result = await _handle_subagent(
        context,
        {"content": "spawn", "agent_id": "worker"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert: the child Session keeps the identity scope, the child run
    # carries project_id None, and the parent link records project_id None —
    # today's behavior, exactly unchanged.
    assert result["ok"] is True
    child_session_id = result["data"]["session_id"]
    assert runtime.chat_sessions.exists(_address("worker", child_session_id))
    assert not runtime.chat_sessions.exists(_address("worker", child_session_id, "acme"))
    assert manager.started[0]["project_id"] is None
    assert manager.started[0]["run"].project_id is None
    metadata = runtime.chat_sessions.get_metadata(_address("worker", child_session_id))
    assert metadata["subagent_parent"]["project_id"] is None


@pytest.mark.parametrize("action", ["run", "status", "cancel"])
async def test_subagent_actions_reject_unknown_arguments(tmp_path: Path, action: str) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()
    arguments: JsonObject = {"action": action, "unexpected": True}
    if action == "run":
        arguments["content"] = "spawn"
    else:
        arguments["id"] = "sub_test"

    result = await _handle_subagent_impl(
        context,
        arguments,
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert result["ok"] is False
    assert result["error"] == {
        "code": "invalid_arguments",
        "message": "Unknown argument(s): unexpected",
    }
    assert manager.started == []


async def test_project_subagent_routes_into_existing_project_session(
    tmp_path: Path,
) -> None:
    # Arrange: an existing project-scoped session for the worker.
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(project_id="acme")
    runtime.chat_sessions.create("worker", session_id="existing", project_id="acme")

    # Act
    result = await _handle_subagent(
        context,
        {"content": "spawn", "agent_id": "worker", "session_id": "existing"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert: the existing project session is reused and the run is project-keyed.
    assert result["ok"] is True
    assert result["data"]["session_id"] == "existing"
    assert manager.started[0]["project_id"] == "acme"


async def test_project_subagent_rejects_missing_project_session(tmp_path: Path) -> None:
    # Arrange: a session id that exists only in the identity layout, not under
    # the project anchor — the project spawn must not find it.
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(project_id="acme")
    runtime.chat_sessions.create("worker", session_id="identity-only")

    # Act
    result = await _handle_subagent(
        context,
        {"content": "spawn", "agent_id": "worker", "session_id": "identity-only"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is False
    assert result["error"]["code"] == "session_not_found"
    assert manager.started == []


async def test_subagent_self_spawn_inherits_parent_project(tmp_path: Path) -> None:
    # Arrange: spawning the calling agent itself (no agent_id) inside a project
    # must still create the child session under the project anchor.
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(project_id="acme")

    # Act
    result = await _handle_subagent(
        context,
        {"content": "spawn"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is True
    assert result["data"]["agent_id"] == "parent"
    child_session_id = result["data"]["session_id"]
    assert runtime.chat_sessions.exists(_address("parent", child_session_id, "acme"))
    assert manager.started[0]["project_id"] == "acme"
    # Settle the background completion tracker task before the loop closes.
    started_run = manager.started[0]["run"]
    started_run.mark_completed(ChatMessage.assistant(model="openai/gpt-5.2", content="done"))
    await asyncio.sleep(0)


async def test_subagent_target_validation_resolves_under_parent_project(
    tmp_path: Path,
) -> None:
    # The target is validated through the resolver with the parent run's project,
    # so the child inherits the project end-to-end at the resolution seam too.
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(project_id="acme")

    result = await _handle_subagent(
        context,
        {"content": "spawn", "agent_id": "worker"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert result["ok"] is True
    assert ("acme", "worker") in runtime.agent_resolver.calls
    started_run = manager.started[0]["run"]
    started_run.mark_completed(ChatMessage.assistant(model="openai/gpt-5.2", content="done"))
    await asyncio.sleep(0)


async def test_subagent_unresolvable_target_returns_failure_envelope(tmp_path: Path) -> None:
    # A target the resolver cannot resolve (off-Team / unknown agent) must return
    # a clean agent_not_found failure envelope, not let the error escape the tool.
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(project_id="acme")

    result = await _handle_subagent(
        context,
        {"content": "spawn", "agent_id": "ghost"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "agent_not_found"
    assert manager.started == []


async def test_resolver_failure_maps_to_tool_failure_not_raised() -> None:
    # Guard the contract directly: a resolver raise becomes a failure envelope.
    from core.subagents.subagents import _validate_target_agent

    class _RaisingResolver:
        def resolve_agent(
            self,
            _project_id: str | None,
            _agent_id: str,
            *,
            run_overrides: Any | None = None,
        ) -> Any:
            del run_overrides
            raise AgentResolutionError("off team")

    runtime = SimpleNamespace(agent_resolver=_RaisingResolver())
    failure = _validate_target_agent(runtime, "ghost", "acme")

    assert failure is not None
    assert failure["error"]["code"] == "agent_not_found"


async def test_subagent_blank_session_id_is_rejected(tmp_path: Path) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(project_id="acme")

    # Act
    result = await _handle_subagent(
        context,
        {"content": "spawn", "agent_id": "worker", "session_id": ""},
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "session_not_found"
    assert manager.started == []


async def test_subagent_blank_agent_id_falls_back_to_calling_agent(tmp_path: Path) -> None:
    # A blank (whitespace-only) agent_id must fall back to the calling agent,
    # exactly like omitting it.
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(project_id="acme")

    # Act
    result = await _handle_subagent(
        context,
        {"content": "spawn", "agent_id": "   "},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is True
    assert result["data"]["agent_id"] == "parent"
    # Settle the background completion tracker task before the loop closes.
    started_run = manager.started[0]["run"]
    started_run.mark_completed(ChatMessage.assistant(model="openai/gpt-5.2", content="done"))
    await asyncio.sleep(0)


async def test_project_subagent_foreground_at_depth_stays_project_scoped(
    tmp_path: Path,
) -> None:
    # A depth >= 1 caller runs its child in the foreground while project scope
    # still carries end-to-end.
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(project_id="acme", nesting_depth=1)

    # Drive the started Run to completion so the foreground wait resolves.
    task = asyncio.create_task(
        _handle_subagent(
            context,
            {"content": "spawn", "agent_id": "worker"},
            runtime=runtime,
            batch_tracker=tracker,
        )
    )
    await asyncio.sleep(0)
    started_run = manager.started[0]["run"]
    started_run.mark_completed(ChatMessage.assistant(model="openai/gpt-5.2", content="child done"))
    result = await task

    # Assert
    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert result["data"]["result"] == "child done"
    assert result["data"]["delivery"] == "inline"
    assert manager.started[0]["project_id"] == "acme"
    assert started_run.project_id == "acme"


async def test_subagent_non_string_session_id_is_rejected(tmp_path: Path) -> None:
    # A present-but-non-string session_id is still a clean invalid_arguments
    # failure — leniency is only for blank strings, not for the wrong type.
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(project_id="acme")

    # Act
    result = await _handle_subagent(
        context,
        {"content": "spawn", "agent_id": "worker", "session_id": 123},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"
    assert manager.started == []
