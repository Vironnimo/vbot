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
from core.projects import AgentResolutionError
from core.runs import ActiveRunError, Run, RunNotFoundError
from core.storage import TemporaryFileManager
from core.subagents.subagents import (
    FORCED_FOREGROUND_NOTE,
    SubAgentBatchTracker,
    _handle_subagent,
    _handle_subagent_result,
)
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
    nesting_depth: int = 0,
    emit_hook: Any | None = None,
    allowed_agents: list[str] | None = None,
    result_persisted_hook: Any | None = None,
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
        nesting_depth=nesting_depth,
        emit_hook=emit_hook,
        result_persisted_hook=result_persisted_hook,
        tool_settings=(
            None if allowed_agents is None else {"subagent": {"allowed_agents": allowed_agents}}
        ),
    )


async def test_foreground_result_acknowledges_child_only_after_parent_persistence(
    tmp_path: Path,
) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    persisted_callbacks: list[Any] = []
    context = make_context(
        result_persisted_hook=persisted_callbacks.append,
    )

    task = asyncio.create_task(
        _handle_subagent(
            context,
            {"content": "spawn", "agent_id": "worker", "background": False},
            runtime=runtime,
            batch_tracker=tracker,
        )
    )
    await asyncio.sleep(0)
    manager.started[0]["run"].mark_completed(
        ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="child output",
        )
    )
    result = await task

    assert result["ok"] is True
    assert len(persisted_callbacks) == 1
    child_session_id = result["data"]["session_id"]
    child_run_id = result["data"]["run_id"]
    runtime.chat_sessions.record_terminal_run(
        "worker",
        child_session_id,
        child_run_id,
        "completed",
        "2026-07-22T10:00:00+00:00",
    )
    assert runtime.chat_sessions.list_with_metadata("worker")[0]["has_unread_completion"] is True

    persisted_callbacks[0]()

    assert runtime.chat_sessions.list_with_metadata("worker")[0]["has_unread_completion"] is False


async def test_result_lookup_acknowledges_persisted_child_run_only_after_parent_persistence(
    tmp_path: Path,
) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    child = runtime.chat_sessions.create("worker", session_id="child-session")
    child.append(ChatMessage.assistant(model="openai/gpt-5.2", content="child output"))
    child.append(
        ChatMessage.run_summary(
            run_id="child-run",
            status="completed",
            timing={
                "started_at": "2026-07-22T10:00:00+00:00",
                "completed_at": "2026-07-22T10:00:01+00:00",
                "duration_ms": 1000,
            },
        )
    )
    runtime.chat_sessions.record_terminal_run(
        "worker",
        "child-session",
        "child-run",
        "completed",
        "2026-07-22T10:00:00+00:00",
    )
    persisted_callbacks: list[Any] = []
    context = make_context(result_persisted_hook=persisted_callbacks.append)

    result = await _handle_subagent_result(
        context,
        {"agent_id": "worker", "session_id": "child-session"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert result["ok"] is True
    assert result["data"]["run_id"] == "child-run"
    assert len(persisted_callbacks) == 1
    assert runtime.chat_sessions.list_with_metadata("worker")[0]["has_unread_completion"] is True

    persisted_callbacks[0]()

    assert runtime.chat_sessions.list_with_metadata("worker")[0]["has_unread_completion"] is False


class RecordingTriggerService:
    async def trigger_run(
        self,
        agent_id: str,
        message: str,
        session_id: str | None = None,
        *,
        internal: bool = False,
        project_id: str | None = None,
    ) -> Run:
        return Run(run_id="trigger-run", agent_id=agent_id, session_id=session_id or "new")


class FakeStorage:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.temporary_files = TemporaryFileManager(data_dir)

    def load_subagent_settings(self) -> JsonObject:
        return {}


class FakeAgents:
    def __init__(self, agent_ids: set[str] | None = None) -> None:
        self._agent_ids = agent_ids or {"parent", "worker"}

    def get(self, agent_id: str) -> SimpleNamespace:
        if agent_id not in self._agent_ids:
            raise AgentNotFoundError(f"Agent not found: {agent_id}")
        return SimpleNamespace(id=agent_id)


class FakeAgentResolver:
    """Resolver seam for the subagent target validation.

    Resolves the target under the parent run's project — identity or project,
    both delegate to the same known-id set here — and raises
    :class:`AgentResolutionError` for an unknown target, matching how the real
    resolver fails an off-Team / unknown-agent spawn. Records the
    ``(project_id, agent_id)`` it was asked to resolve so a test can prove the
    child inherits the parent's project.
    """

    def __init__(self, agents: FakeAgents) -> None:
        self._agents = agents
        self.calls: list[tuple[str | None, str]] = []

    def resolve_agent(self, project_id: str | None, agent_id: str) -> SimpleNamespace:
        self.calls.append((project_id, agent_id))
        try:
            return self._agents.get(agent_id)
        except AgentNotFoundError as error:
            raise AgentResolutionError(str(error)) from error


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
        working_project_id: str | None = None,
    ) -> Run:
        if (agent_id, session_id) in self.busy_sessions:
            raise ActiveRunError(f"session already has an active run: {session_id}")
        run = Run(
            run_id=f"sub-run-{len(self.started) + 1}",
            agent_id=agent_id,
            session_id=session_id,
            project_id=project_id,
            working_project_id=working_project_id,
        )
        self.started.append(
            {
                "agent_id": agent_id,
                "session_id": session_id,
                "executor": executor,
                "project_id": project_id,
                "working_project_id": working_project_id,
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

    def child_loop(self, *, nesting_depth: int) -> FakeChildLoop:
        del nesting_depth
        return self

    def run_executor(self, content: str) -> Any:
        # The project anchor rides ``run.project_id`` (set by the run manager
        # from the project_id passed to start/enqueue), not the executor closure.
        async def _execute(run: Run) -> ChatMessage:
            return ChatMessage.assistant(model="openai/gpt-5.2", content=f"handled: {content}")

        return _execute


def make_runtime(
    tmp_path: Path, manager: FakeRunManager, *, agent_ids: set[str] | None = None
) -> Any:
    child_loop = FakeChildLoop(None)
    agents = FakeAgents(agent_ids)
    return SimpleNamespace(
        agents=agents,
        agent_resolver=FakeAgentResolver(agents),
        chat_sessions=ChatSessionManager(tmp_path),
        chat_run_manager=manager,
        storage=FakeStorage(tmp_path),
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
    assert runtime.chat_sessions.get("worker", child_session_id, "vbot")
    metadata = runtime.chat_sessions.get_metadata("worker", child_session_id, "vbot")
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
    # carries project_id None, and the parent link records project_id None —
    # today's behavior, exactly unchanged.
    assert result["ok"] is True
    child_session_id = result["data"]["session_id"]
    identity_session = tmp_path / "agents" / "worker" / "sessions" / f"{child_session_id}.jsonl"
    assert identity_session.exists()
    assert manager.started[0]["project_id"] is None
    assert manager.started[0]["run"].project_id is None
    metadata = runtime.chat_sessions.get_metadata("worker", child_session_id)
    assert metadata["subagent_parent"]["project_id"] is None


async def test_qualified_subagent_result_uses_target_project_for_persisted_fallback(
    tmp_path: Path,
) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(project_id=None)
    session = runtime.chat_sessions.create("worker", session_id="project-child", project_id="vbot")
    session.append(ChatMessage.assistant(model="openai/gpt-5.2", content="project result"))
    tracker.register(
        (context.agent_id, context.session_id, context.run_id),
        "worker",
        "project-child",
        "missing-run",
        "vbot",
    )

    result = await _handle_subagent_result(
        context,
        {
            "agent_id": "worker@vbot",
            "session_id": "project-child",
            "run_id": "missing-run",
        },
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert result["ok"] is True
    assert result["data"]["project_id"] == "vbot"
    assert result["data"]["result"] == "project result"


async def test_project_parent_cannot_read_cross_project_subagent_result(tmp_path: Path) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(project_id="acme")
    run = Run(
        run_id="cross-project-run",
        agent_id="worker",
        session_id="project-child",
        project_id="vbot",
    )
    manager.runs[run.id] = run
    run.mark_completed(ChatMessage.assistant(model="openai/gpt-5.2", content="live result"))

    result = await _handle_subagent_result(
        context,
        {
            "agent_id": "worker@vbot",
            "session_id": "project-child",
            "run_id": run.id,
        },
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "agent_not_allowed"


@pytest.mark.parametrize("tool_name", ["subagent", "subagent_result"])
async def test_malformed_qualified_subagent_address_fails_cleanly(
    tmp_path: Path,
    tool_name: str,
) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(project_id=None)
    arguments: JsonObject = {"agent_id": "worker@vbot@extra", "session_id": "child"}
    if tool_name == "subagent":
        arguments["content"] = "spawn"

    handler = _handle_subagent if tool_name == "subagent" else _handle_subagent_result
    result = await handler(
        context,
        arguments,
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"


@pytest.mark.parametrize("tool_name", ["subagent", "subagent_result"])
async def test_subagent_tools_reject_unknown_arguments(
    tmp_path: Path,
    tool_name: str,
) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()
    arguments: JsonObject = {"session_id": "child", "unexpected": True}
    if tool_name == "subagent":
        arguments["content"] = "spawn"

    handler = _handle_subagent if tool_name == "subagent" else _handle_subagent_result
    result = await handler(
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
        def resolve_agent(self, _project_id: str | None, _agent_id: str) -> Any:
            raise AgentResolutionError("off team")

    runtime = SimpleNamespace(agent_resolver=_RaisingResolver())
    failure = _validate_target_agent(runtime, "ghost", "acme")

    assert failure is not None
    assert failure["error"]["code"] == "agent_not_found"


async def test_subagent_blank_session_id_creates_new_session(tmp_path: Path) -> None:
    # Models routinely emit an omitted optional string field as "" (schema-valid
    # for ``type: string``). A blank session_id must mean "create a new session",
    # exactly like omitting it — not a hard rejection.
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

    # Assert: a fresh project-scoped session was created, never an empty-id lookup.
    assert result["ok"] is True
    child_session_id = result["data"]["session_id"]
    assert child_session_id
    project_session = (
        tmp_path
        / "projects"
        / "acme"
        / "agents"
        / "worker"
        / "sessions"
        / f"{child_session_id}.jsonl"
    )
    assert project_session.exists()
    # Settle the background completion tracker task before the loop closes.
    started_run = manager.started[0]["run"]
    started_run.mark_completed(ChatMessage.assistant(model="openai/gpt-5.2", content="done"))
    await asyncio.sleep(0)


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


async def test_project_subagent_forced_foreground_at_depth_stays_project_scoped(
    tmp_path: Path,
) -> None:
    # Option A composes with project inheritance: a depth >= 1 spawn is forced to
    # the foreground (returns the finished child payload plus the forced-foreground
    # note) while the child run still carries the parent's project end-to-end.
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(project_id="acme", nesting_depth=1)

    # Act: a background request that Option A forces to the foreground. Drive the
    # started run to completion so the foreground wait resolves.
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
    assert result["data"]["spawn_note"] == FORCED_FOREGROUND_NOTE
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
