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
from core.runs import (
    DEFAULT_RUN_ADMISSION,
    ActiveRunError,
    Run,
    RunAdmission,
    RunNotFoundError,
)
from core.sessions import SessionAddress
from core.storage import TemporaryFileManager
from core.subagents.subagents import (
    SubAgentBatchTracker,
    SubAgentCoordinator,
    _handle_subagent_status,
)
from core.subagents.subagents import (
    _handle_subagent as _handle_subagent_impl,
)
from core.tools.tools import ToolContext

pytestmark = pytest.mark.asyncio

JsonObject = dict[str, Any]
SUBAGENT_TOOL_NAME = "subagent"


def _address(
    agent_id: str,
    session_id: str,
    project_id: str | None = None,
) -> SessionAddress:
    return SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)


async def _handle_subagent(
    context: ToolContext,
    arguments: JsonObject,
    *,
    runtime: Any,
    batch_tracker: SubAgentBatchTracker,
) -> JsonObject:
    canonical = {"action": "run", **arguments}
    return await _handle_subagent_impl(
        context,
        canonical,
        runtime=runtime,
        batch_tracker=batch_tracker,
    )


async def _handle_subagent_result(
    context: ToolContext,
    arguments: JsonObject,
    *,
    runtime: Any,
    batch_tracker: SubAgentBatchTracker,
) -> JsonObject:
    return await _handle_subagent_status(
        context,
        {"action": "status", **arguments},
        runtime=runtime,
        batch_tracker=batch_tracker,
    )


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
        vbot_root=Path("app"),
        data_root=Path("data"),
        project_id=project_id,
        nesting_depth=nesting_depth,
        emit_hook=emit_hook,
        result_persisted_hook=result_persisted_hook,
        tool_settings=(
            None if allowed_agents is None else {"subagent": {"allowed_agents": allowed_agents}}
        ),
    )


async def test_foreground_result_keeps_handle_and_child_unread_until_parent_persistence(
    tmp_path: Path,
) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    trigger_service = RecordingTriggerService()
    tracker = SubAgentBatchTracker(trigger_service)
    persisted_callbacks: list[Any] = []
    context = make_context(
        nesting_depth=1,
        result_persisted_hook=persisted_callbacks.append,
    )

    task = asyncio.create_task(
        _handle_subagent(
            context,
            {"content": "spawn", "agent_id": "worker"},
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
    assert manager.started[0]["work_id"] == result["data"]["id"]
    assert len(persisted_callbacks) == 1
    child_session_id = result["data"]["session_id"]
    child_run_id = manager.started[0]["run"].id
    work_id = result["data"]["id"]
    runtime.chat_sessions.record_terminal_run(
        _address("worker", child_session_id),
        child_run_id,
        "completed",
        "2026-07-22T10:00:00+00:00",
    )
    manager.parent_run.request_cancel(reason="user")
    await asyncio.sleep(0)
    assert tracker.owned_entry("parent", "parent-session", None, work_id) is not None
    assert f"subagent:parent-run:{work_id}" in trigger_service.completion_deliveries
    assert runtime.chat_sessions.list_with_metadata("worker")[0]["has_unread_completion"] is True

    persisted_callbacks[0]()

    assert tracker.owned_entry("parent", "parent-session", None, work_id) is None
    assert f"subagent:parent-run:{work_id}" not in trigger_service.completion_deliveries
    assert runtime.chat_sessions.list_with_metadata("worker")[0]["has_unread_completion"] is False


async def test_status_result_keeps_handle_and_child_unread_until_parent_persistence(
    tmp_path: Path,
) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    trigger_service = RecordingTriggerService()
    tracker = SubAgentBatchTracker(trigger_service)
    child = runtime.chat_sessions.create("worker", session_id="child-session")
    child.append(ChatMessage.assistant(model="openai/gpt-5.2", content="child output"))
    child.append(
        ChatMessage.run_summary(
            run_id="child-run",
            status="completed",
            iteration_count=1,
            timing={
                "started_at": "2026-07-22T10:00:00+00:00",
                "completed_at": "2026-07-22T10:00:01+00:00",
                "duration_ms": 1000,
            },
        )
    )
    runtime.chat_sessions.record_terminal_run(
        _address("worker", "child-session"),
        "child-run",
        "completed",
        "2026-07-22T10:00:00+00:00",
    )
    persisted_callbacks: list[Any] = []
    context = make_context(result_persisted_hook=persisted_callbacks.append)
    tracker.register(
        (context.agent_id, context.session_id, context.run_id),
        "worker",
        "child-session",
        "child-run",
        work_id="sub_child",
    )

    result = await _handle_subagent_result(
        context,
        {"id": "sub_child"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert result["ok"] is True
    assert result["data"]["id"] == "sub_child"
    assert "run_id" not in result["data"]
    assert len(persisted_callbacks) == 1
    tracker.on_sub_agent_complete(
        (context.agent_id, context.session_id, context.run_id),
        "child-run",
        {"status": "completed", "result": "child output"},
    )
    manager.parent_run.request_cancel(reason="user")
    assert tracker.owned_entry("parent", "parent-session", None, "sub_child") is not None
    assert "subagent:parent-run:sub_child" in trigger_service.completion_deliveries
    assert runtime.chat_sessions.list_with_metadata("worker")[0]["has_unread_completion"] is True

    persisted_callbacks[0]()

    assert tracker.owned_entry("parent", "parent-session", None, "sub_child") is None
    assert "subagent:parent-run:sub_child" not in trigger_service.completion_deliveries
    assert runtime.chat_sessions.list_with_metadata("worker")[0]["has_unread_completion"] is False


class RecordingTriggerService:
    def __init__(self) -> None:
        self.completion_deliveries: dict[str, asyncio.Future[None]] = {}

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

    def submit_completion(
        self,
        agent_id: str,
        session_id: str,
        *,
        notice_id: str,
        origin_run_id: str,
        body: str,
        project_id: str | None = None,
        on_persisted: Any | None = None,
    ) -> asyncio.Future[None]:
        del agent_id, session_id, origin_run_id, body, project_id, on_persisted
        delivery: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.completion_deliveries[notice_id] = delivery
        return delivery

    def cancel_completion(
        self,
        agent_id: str,
        session_id: str,
        *,
        notice_id: str,
        project_id: str | None = None,
    ) -> bool:
        del agent_id, session_id, project_id
        delivery = self.completion_deliveries.pop(notice_id, None)
        if delivery is None:
            return False
        if not delivery.done():
            delivery.cancel()
        return True


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

    def resolve_agent(
        self,
        project_id: str | None,
        agent_id: str,
        *,
        run_overrides: Any | None = None,
    ) -> SimpleNamespace:
        del run_overrides
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
        address: SessionAddress,
        executor: Any,
        *,
        admission: RunAdmission = DEFAULT_RUN_ADMISSION,
    ) -> Run:
        agent_id = address.agent_id
        session_id = address.session_id
        if (agent_id, session_id) in self.busy_sessions:
            raise ActiveRunError(f"session already has an active run: {session_id}")
        run = Run(
            run_id=f"sub-run-{len(self.started) + 1}",
            agent_id=agent_id,
            session_id=session_id,
            project_id=address.project_id,
            working_project_id=admission.working_project_id,
            run_kind=admission.run_kind,
            work_id=admission.work_id,
        )
        self.started.append(
            {
                "agent_id": agent_id,
                "session_id": session_id,
                "executor": executor,
                "project_id": address.project_id,
                "working_project_id": admission.working_project_id,
                "run_kind": admission.run_kind,
                "work_id": admission.work_id,
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

    def active_run(
        self, *, agent_id: str, session_id: str, project_id: str | None = None
    ) -> Run | None:
        return self.busy_sessions.get((agent_id, session_id))

    def list_queued(
        self, agent_id: str, session_id: str, *, project_id: str | None = None
    ) -> list[Any]:
        return []


class FakeChildLoop:
    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    def child_loop(self, *, nesting_depth: int) -> FakeChildLoop:
        del nesting_depth
        return self

    def run_executor(self, content: str, *, agent_overrides: Any | None = None) -> Any:
        # The project anchor rides ``run.project_id`` (set by the run manager
        # from the project_id passed to start/enqueue), not the executor closure.
        del agent_overrides

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


async def test_inspect_resolves_exact_completed_work_after_child_session_reuse(
    tmp_path: Path,
) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    session = runtime.chat_sessions.create("worker", session_id="reused-child")
    old_timing = {
        "started_at": "2026-07-24T10:00:00+00:00",
        "completed_at": "2026-07-24T10:00:01+00:00",
        "duration_ms": 1000,
    }
    new_timing = {
        "started_at": "2026-07-24T11:00:00+00:00",
        "completed_at": "2026-07-24T11:00:01+00:00",
        "duration_ms": 1000,
    }
    session.append(ChatMessage.user("old request"))
    session.append(ChatMessage.assistant(model="openai/gpt-5.2", content="old result"))
    session.append(
        ChatMessage.run_summary(
            run_id="old-run",
            work_id="sub_old",
            status="completed",
            timing=old_timing,
            iteration_count=1,
        )
    )
    session.append(ChatMessage.user("new request"))
    session.append(ChatMessage.assistant(model="openai/gpt-5.2", content="new result"))
    session.append(
        ChatMessage.run_summary(
            run_id="new-run",
            work_id="sub_new",
            status="completed",
            timing=new_timing,
            iteration_count=1,
        )
    )

    result = SubAgentCoordinator(runtime, RecordingTriggerService()).inspect(
        "worker",
        "reused-child",
        "sub_old",
    )

    assert result is not None
    assert result["id"] == "sub_old"
    assert result["run_id"] == "old-run"
    assert result["status"] == "completed"
    assert result["result"] == "old result"
    assert result["timing"] == old_timing


async def test_inspect_prefers_matching_live_work_in_child_session(tmp_path: Path) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    runtime.chat_sessions.create("worker", session_id="live-child")
    active = Run(
        run_id="live-run",
        agent_id="worker",
        session_id="live-child",
        work_id="sub_live",
    )
    manager.busy_sessions[("worker", "live-child")] = active

    result = SubAgentCoordinator(runtime, RecordingTriggerService()).inspect(
        "worker",
        "live-child",
        "sub_live",
    )

    assert result is not None
    assert result["id"] == "sub_live"
    assert result["run_id"] == "live-run"
    assert result["status"] == "running"
    assert result["started_at"] == active.created_at
    assert result["result"] is None


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

    # Assert: the child session keeps the global identity layout, the child run
    # carries project_id None, and the parent link records project_id None —
    # today's behavior, exactly unchanged.
    assert result["ok"] is True
    child_session_id = result["data"]["session_id"]
    identity_session = tmp_path / "agents" / "worker" / "sessions" / f"{child_session_id}.jsonl"
    assert identity_session.exists()
    assert manager.started[0]["project_id"] is None
    assert manager.started[0]["run"].project_id is None
    metadata = runtime.chat_sessions.get_metadata(_address("worker", child_session_id))
    assert metadata["subagent_parent"]["project_id"] is None


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


async def test_qualified_subagent_result_uses_target_project_for_persisted_fallback(
    tmp_path: Path,
) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(project_id=None)
    session = runtime.chat_sessions.create("worker", session_id="project-child", project_id="vbot")
    session.append(ChatMessage.assistant(model="openai/gpt-5.2", content="project result"))
    session.append(
        ChatMessage.run_summary(
            run_id="missing-run",
            status="completed",
            iteration_count=1,
            timing={
                "started_at": "2026-07-24T10:00:00+00:00",
                "completed_at": "2026-07-24T10:00:01+00:00",
                "duration_ms": 1000,
            },
        )
    )
    tracker.register(
        (context.agent_id, context.session_id, context.run_id),
        "worker",
        "project-child",
        "missing-run",
        "vbot",
        work_id="sub_project",
    )

    result = await _handle_subagent_result(
        context,
        {"id": "sub_project"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert result["ok"] is True
    assert result["data"]["project_id"] == "vbot"
    assert result["data"]["result"] == "project result"


async def test_status_cannot_read_unowned_subagent_work(tmp_path: Path) -> None:
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
        {"id": "sub_unowned"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "subagent_not_owned"


async def test_malformed_qualified_subagent_address_fails_cleanly(
    tmp_path: Path,
) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(project_id=None)
    arguments: JsonObject = {"agent_id": "worker@vbot@extra", "session_id": "child"}
    arguments["content"] = "spawn"
    result = await _handle_subagent(
        context,
        arguments,
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"


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
