"""Tests for project-scoped sub-agent spawning.

A sub-agent inherits its parent run's project end-to-end: the child session is
created under the project anchor, the child run is keyed to the project, and the
durable parent→child link records the project id. An identity parent run
(``project_id is None``) keeps today's behavior, exactly unchanged.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.agents import AgentNotFoundError
from core.chat import ChatMessage, ChatSessionManager
from core.runs import ActiveRunError, Run, RunNotFoundError
from core.subagents.subagents import SubAgentBatchTracker, _handle_subagent
from core.tools.tools import ToolContext

pytestmark = pytest.mark.asyncio

JsonObject = dict[str, Any]
SUBAGENT_TOOL_NAME = "subagent"


def make_context(
    *,
    agent_id: str = "parent",
    session_id: str = "parent-session",
    run_id: str = "parent-run",
    project_id: str | None = None,
) -> ToolContext:
    return ToolContext(
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
        tool_call_id="tool-call-one",
        tool_name=SUBAGENT_TOOL_NAME,
        tool_call_index=0,
        workspace=Path("workspace"),
        app_root=Path("app"),
        data_root=Path("data"),
        project_id=project_id,
    )


class RecordingTriggerService:
    async def trigger_run(
        self,
        agent_id: str,
        message: str,
        session_id: str | None = None,
        *,
        internal: bool = False,
    ) -> Run:
        return Run(run_id="trigger-run", agent_id=agent_id, session_id=session_id or "new")


class FakeStorage:
    def __init__(self) -> None:
        self.data_dir = Path("data")

    def load_subagent_settings(self) -> JsonObject:
        return {}


class FakeAgents:
    def __init__(self, agent_ids: set[str] | None = None) -> None:
        self._agent_ids = agent_ids or {"parent", "worker"}

    def get(self, agent_id: str) -> SimpleNamespace:
        if agent_id not in self._agent_ids:
            raise AgentNotFoundError(f"Agent not found: {agent_id}")
        return SimpleNamespace(id=agent_id)


class FakeRunManager:
    """Run manager that records the project_id passed to start/enqueue."""

    def __init__(self) -> None:
        self.parent_run = Run(run_id="parent-run", agent_id="parent", session_id="parent-session")
        self.started: list[dict[str, Any]] = []
        self.runs: dict[str, Run] = {self.parent_run.id: self.parent_run}
        self.busy_sessions: dict[tuple[str, str], Run] = {}

    async def start(
        self,
        *,
        agent_id: str,
        session_id: str,
        executor: Any,
        project_id: str | None = None,
    ) -> Run:
        if (agent_id, session_id) in self.busy_sessions:
            raise ActiveRunError(f"session already has an active run: {session_id}")
        run = Run(
            run_id=f"sub-run-{len(self.started) + 1}",
            agent_id=agent_id,
            session_id=session_id,
        )
        self.started.append(
            {
                "agent_id": agent_id,
                "session_id": session_id,
                "executor": executor,
                "project_id": project_id,
                "run": run,
            }
        )
        self.runs[run.id] = run
        return run

    def get(self, run_id: str) -> Run:
        try:
            return self.runs[run_id]
        except KeyError as exc:
            raise RunNotFoundError(f"run not found: {run_id}") from exc


class FakeChildLoop:
    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self.executor_project_ids: list[str | None] = []

    def child_loop(self, *, nesting_depth: int) -> FakeChildLoop:
        del nesting_depth
        return self

    def run_executor(self, content: str, *, project_id: str | None = None) -> Any:
        # Record the project_id baked into the child executor closure so a test
        # can prove the child run executes project-scoped.
        self.executor_project_ids.append(project_id)

        async def _execute(run: Run) -> ChatMessage:
            return ChatMessage.assistant(model="openai/gpt-5.2", content=f"handled: {content}")

        return _execute


def make_runtime(tmp_path: Path, manager: FakeRunManager) -> Any:
    child_loop = FakeChildLoop(None)
    return SimpleNamespace(
        agents=FakeAgents(),
        chat_sessions=ChatSessionManager(tmp_path),
        chat_run_manager=manager,
        storage=FakeStorage(),
        streaming_chat_loop=child_loop,
    )


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

    # Assert: the child session was created under the project anchor, never the
    # global identity layout.
    assert result["ok"] is True
    child_session_id = result["data"]["session_id"]
    project_session = (
        tmp_path
        / "projects"
        / "acme"
        / "agents"
        / "worker"
        / "sessions"
        / f"{child_session_id}.jsonl"
    )
    identity_session = tmp_path / "agents" / "worker" / "sessions" / f"{child_session_id}.jsonl"
    assert project_session.exists()
    assert not identity_session.exists()


async def test_project_subagent_run_is_keyed_to_project(tmp_path: Path) -> None:
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

    # Assert: the child run carries the parent project on the run key, and the
    # child executor closure was built project-scoped.
    assert result["ok"] is True
    assert manager.started[0]["project_id"] == "acme"
    assert runtime.streaming_chat_loop.executor_project_ids == ["acme"]


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
    metadata = runtime.chat_sessions.get_metadata("worker", child_session_id, "acme")
    assert metadata["is_subagent_session"] is True
    assert metadata["subagent_parent"] == {
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

    # Assert: the child session keeps the global identity layout, the child run
    # is keyed to None, and the parent link records project_id None — today's
    # behavior, exactly unchanged.
    assert result["ok"] is True
    child_session_id = result["data"]["session_id"]
    identity_session = tmp_path / "agents" / "worker" / "sessions" / f"{child_session_id}.jsonl"
    assert identity_session.exists()
    assert manager.started[0]["project_id"] is None
    assert runtime.streaming_chat_loop.executor_project_ids == [None]
    metadata = runtime.chat_sessions.get_metadata("worker", child_session_id)
    assert metadata["subagent_parent"]["project_id"] is None


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
    project_session = (
        tmp_path
        / "projects"
        / "acme"
        / "agents"
        / "parent"
        / "sessions"
        / f"{child_session_id}.jsonl"
    )
    assert project_session.exists()
    assert manager.started[0]["project_id"] == "acme"
    # Settle the non-blocking completion tracker task before the loop closes.
    started_run = manager.started[0]["run"]
    started_run.mark_completed(ChatMessage.assistant(model="openai/gpt-5.2", content="done"))
    await asyncio.sleep(0)
