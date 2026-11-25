"""Contracts for Simplified Software Factory Job Run execution engine."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.agents.temporary_execution import TemporaryRunAdmission, TemporaryRunInput
from core.sessions import SessionAddress
from resources.extensions.factory._job_execution import FactoryJobExecution
from resources.extensions.factory.store import FactoryStore
from tests.resources.extensions.factory.factory_test_support import FakeHost

type Json = dict[str, Any]
pytestmark = pytest.mark.asyncio


@dataclass
class _FakeBinding:
    participant_id: str
    address: SessionAddress
    generation_id: str = "gen-1"
    config: Json = field(default_factory=dict)


@dataclass
class _FakeMessage:
    role: str
    content: str
    run_id: str = "run-1"
    tool_calls: list[Any] = field(default_factory=list)


class _FakeTemporaryAgents:
    def __init__(self) -> None:
        self.bindings: dict[str, dict[str, _FakeBinding]] = {}
        self.open_epochs: dict[str, str] = {}
        self.started: list[tuple[str, str, TemporaryRunInput]] = []
        self.closed: list[tuple[str, str]] = []
        self.snapshots: dict[str, list[_FakeMessage]] = {}
        self.live_runs: dict[str, dict[str, Any]] = {}
        self._counter = 0

    async def list(self, group_id: str, *, after: str = "", limit: int = 100) -> list[_FakeBinding]:
        _ = after, limit
        return list(self.bindings.get(group_id, {}).values())

    async def create(
        self,
        group_id: str,
        participant_id: str,
        config: Any,
        *,
        project_id: str | None = None,
    ) -> _FakeBinding:
        binding = _FakeBinding(
            participant_id=participant_id,
            address=SessionAddress(
                project_id=project_id,
                agent_id=config.identity_agent or "worker",
                session_id=f"sess-{group_id}",
            ),
            config={"name": config.name},
        )
        self.bindings.setdefault(group_id, {})[participant_id] = binding
        return binding

    async def open_group(self, group_id: str) -> SimpleNamespace:
        epoch = self.open_epochs.setdefault(group_id, f"grp-{len(self.open_epochs) + 1}")
        return SimpleNamespace(owner_name="factory", group_id=group_id, epoch=epoch)

    async def start(
        self,
        handle: Any,
        participant_id: str,
        run_input: TemporaryRunInput,
    ) -> TemporaryRunAdmission:
        self._counter += 1
        run_id = f"run-{self._counter}"
        self.started.append((handle.group_id, participant_id, run_input))
        self.live_runs.setdefault(handle.group_id, {})[run_id] = SimpleNamespace(
            record=SimpleNamespace(terminal_status=None),
            run=SimpleNamespace(status=SimpleNamespace(value="running")),
        )
        return TemporaryRunAdmission(run_id=run_id, status="running", existing=False)

    async def inspect(self, group_id: str, participant_id: str, query: Json) -> SimpleNamespace:
        _ = participant_id, query
        return SimpleNamespace(page=SimpleNamespace(messages=self.snapshots.get(group_id, [])))

    async def close_group(self, group_id: str, reason: str = "extension") -> Json:
        self.closed.append((group_id, reason))
        self.open_epochs.pop(group_id, None)
        self.live_runs.pop(group_id, None)
        return {"closed": True}

    async def groups(self, *, after: str = "", limit: int = 100) -> tuple[str, ...]:
        _ = after, limit
        return tuple(self.bindings.keys())

    async def owned_runs(self, group_id: str, run_ids: Any) -> dict[str, Any]:
        group_runs = self.live_runs.get(group_id, {})
        return {run_id: group_runs[run_id] for run_id in run_ids if run_id in group_runs}


class _FakeService:
    def __init__(self, store: FactoryStore, host: Any, groups: _FakeTemporaryAgents) -> None:
        self.store = store
        self.host: Any = host
        self.host.temporary_agents = groups
        self.lock = asyncio.Lock()
        self._closing = False
        self.schedule_requests = 0

    def records(self) -> FactoryStore:
        return self.store

    async def project(self, project_id: str) -> Json:
        return {"id": project_id, "cwd": str(self.host.data_dir / project_id)}

    def request_schedule(self) -> None:
        self.schedule_requests += 1


def _request_context(group_id: str, run_id: str = "run-1") -> SimpleNamespace:
    return SimpleNamespace(
        binding=SimpleNamespace(group_id=group_id),
        run_id=run_id,
    )


async def _setup(
    tmp_path: Path, factory_store: FactoryStore
) -> tuple[
    FactoryJobExecution,
    FactoryStore,
    FakeHost,
    _FakeTemporaryAgents,
    _FakeService,
    Json,
    Json,
]:
    host = FakeHost(tmp_path)
    groups = _FakeTemporaryAgents()
    service = _FakeService(factory_store, host, groups)
    engine = FactoryJobExecution(service)
    factory = await factory_store.native.create_factory(
        "project", leader_agent_id="leader", leader_agent_proof="inc-1"
    )
    worker = await factory_store.native.add_member(
        factory["id"], "worker", agent_proof="inc-2", role="Worker"
    )
    return engine, factory_store, host, groups, service, factory, worker


async def test_schedule_admits_queued_runs_respecting_wip_limit(
    tmp_path: Path, factory_store: FactoryStore
) -> None:
    engine, store, _, groups, _, factory, worker = await _setup(tmp_path, factory_store)
    assert engine.owns("fjob.jrun_123")
    assert not engine.owns("native.fex_123")

    job = await store.jobs.create_job(
        factory["id"], name="Nightly", prompt="Run tests", agent_member_id=worker["id"]
    )
    r1 = await store.jobs.create_run(factory["id"], job["id"], origin="manual")
    r2 = await store.jobs.create_run(factory["id"], job["id"], origin="manual")
    r3 = await store.jobs.create_run(factory["id"], job["id"], origin="manual")

    await engine.schedule(wip_limit=2)

    assert await engine.running_count() == 2
    assert (await store.jobs.get_run(r1["id"]))["state"] == "running"
    assert (await store.jobs.get_run(r2["id"]))["state"] == "running"
    assert (await store.jobs.get_run(r3["id"]))["state"] == "queued"
    assert [item[0] for item in groups.started] == [r1["group_id"], r2["group_id"]]
    assert (await store.jobs.get_run(r1["id"]))["session_address"] == {
        "project_id": "project",
        "agent_id": "worker",
        "session_id": f"sess-{r1['group_id']}",
    }


async def test_normal_completion_without_tool_settles_done_with_summary(
    tmp_path: Path, factory_store: FactoryStore
) -> None:
    engine, store, _, groups, service, factory, worker = await _setup(tmp_path, factory_store)
    job = await store.jobs.create_job(
        factory["id"], name="Build", prompt="Build project", agent_member_id=worker["id"]
    )
    run = await store.jobs.create_run(factory["id"], job["id"], origin="manual")
    await engine.schedule(wip_limit=1)

    groups.snapshots[run["group_id"]] = [
        _FakeMessage(role="user", content="Build project", run_id="run-1"),
        _FakeMessage(role="assistant", content="All checks passed.", run_id="run-1"),
    ]
    await engine.run_finished(_request_context(run["group_id"], "run-1"), outcome="success")

    settled = await store.jobs.get_run(run["id"])
    assert settled["state"] == "done"
    assert settled["summary"] == "All checks passed."
    assert service.schedule_requests >= 1


async def test_completion_after_needs_attention_keeps_state_and_notifies(
    tmp_path: Path, factory_store: FactoryStore
) -> None:
    engine, store, host, _, _, factory, worker = await _setup(tmp_path, factory_store)
    job = await store.jobs.create_job(
        factory["id"], name="Deploy", prompt="Deploy app", agent_member_id=worker["id"]
    )
    run = await store.jobs.create_run(factory["id"], job["id"], origin="manual")
    await engine.schedule(wip_limit=1)

    await store.jobs.transition_run(
        run["id"], "needs_attention", question="Which environment should I deploy to?"
    )
    await engine.run_finished(_request_context(run["group_id"], "run-1"), outcome="success")

    settled = await store.jobs.get_run(run["id"])
    assert settled["state"] == "needs_attention"
    assert settled["question"] == "Which environment should I deploy to?"
    assert await engine.attention_count() == 1
    assert not host.notifications


async def test_reply_run_resumes_needs_attention_and_delivers_to_same_session(
    tmp_path: Path, factory_store: FactoryStore
) -> None:
    engine, store, _, groups, _, factory, worker = await _setup(tmp_path, factory_store)
    job = await store.jobs.create_job(
        factory["id"], name="Deploy", prompt="Deploy app", agent_member_id=worker["id"]
    )
    run = await store.jobs.create_run(factory["id"], job["id"], origin="manual")
    await engine.schedule(wip_limit=1)
    await store.jobs.transition_run(
        run["id"], "needs_attention", question="Which environment?", reason="waiting_input"
    )

    resumed = await engine.reply_run(run["id"], "Deploy to staging")
    assert resumed["state"] == "running"
    assert resumed["question"] == ""
    assert resumed["reason"] == ""
    assert len(groups.started) == 2
    group_id, participant_id, run_input = groups.started[-1]
    assert group_id == run["group_id"]
    assert participant_id == run["participant_id"]
    assert run_input.kind == "continuation"
    assert run_input.content == "Deploy to staging"


async def test_cancel_run_transitions_cancelled_and_frees_wip(
    tmp_path: Path, factory_store: FactoryStore
) -> None:
    engine, store, _, groups, service, factory, worker = await _setup(tmp_path, factory_store)
    job = await store.jobs.create_job(
        factory["id"], name="Lint", prompt="Run linter", agent_member_id=worker["id"]
    )
    run = await store.jobs.create_run(factory["id"], job["id"], origin="manual")
    await engine.schedule(wip_limit=1)

    cancelled = await engine.cancel_run(run["id"])
    assert cancelled["state"] == "cancelled"
    assert await engine.running_count() == 0
    assert (run["group_id"], "cancelled") in groups.closed
    assert service.schedule_requests >= 1


async def test_reconcile_marks_orphaned_running_runs_as_failed(
    tmp_path: Path, factory_store: FactoryStore
) -> None:
    engine, store, host, _, _, factory, worker = await _setup(tmp_path, factory_store)
    job = await store.jobs.create_job(
        factory["id"], name="Index", prompt="Build index", agent_member_id=worker["id"]
    )
    run = await store.jobs.create_run(factory["id"], job["id"], origin="manual")
    await store.jobs.transition_run(run["id"], "running")

    await engine.reconcile()

    reconciled = await store.jobs.get_run(run["id"])
    assert reconciled["state"] == "failed"
    assert reconciled["reason"] == "Interrupted by server restart."
    assert host.notifications
    assert host.notifications[-1]["key"] == run["id"]


async def test_tool_access_overlap_and_reply_run_edge_cases(
    tmp_path: Path, factory_store: FactoryStore
) -> None:
    engine, store, _, groups, _, factory, worker = await _setup(tmp_path, factory_store)
    await store.native.update_factory(
        factory["id"],
        {"config": {"allowed_tools": ["read_file", "bash"]}},
        actor="user",
        expected_revision=factory["revision"],
    )
    await store.native.update_member(
        worker["id"],
        {"config": {"denied_tools": ["bash"]}},
        actor="user",
        expected_revision=worker["revision"],
    )
    updated_factory = await store.native.get_factory(factory["id"])
    updated_worker = await store.native.get_member(worker["id"])
    job = await store.jobs.create_job(
        factory["id"], name="Overlap", prompt="Check files", agent_member_id=worker["id"]
    )
    run = await store.jobs.create_run(factory["id"], job["id"], origin="manual")

    config = await engine._build_config(updated_factory, updated_worker, run)
    assert "bash" not in config.tool_access.allowed
    assert "factory" in config.tool_access.denied

    # Resuming a failed run with no session_address sends initial + reply and clears finished_at.
    failed = await store.jobs.transition_run(run["id"], "failed", reason="Earlier crash")
    assert failed["finished_at"] is not None

    resumed = await engine.reply_run(run["id"], "Retry with clean state")
    assert resumed["state"] == "running"
    assert resumed["finished_at"] is None
    _, _, run_input = groups.started[-1]
    assert run_input.kind == "initial"
    assert "Check files" in run_input.content
    assert "Retry with clean state" in run_input.content

    # Stale run_finished is ignored when run_id doesn't match _active_run_ids.
    await engine.run_finished(_request_context(run["group_id"], "stale-run-id"), outcome="error")
    assert (await store.jobs.get_run(run["id"]))["state"] == "running"

    # reply_run rolls back to failed if groups.start raises.
    await store.jobs.transition_run(run["id"], "needs_attention", question="Continue?")

    async def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("start boom")

    groups.start = _boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="start boom"):
        await engine.reply_run(run["id"], "Go ahead")
    after_boom = await store.jobs.get_run(run["id"])
    assert after_boom["state"] == "failed"
    assert "start boom" in after_boom["reason"]


async def test_cancel_via_management_does_not_deadlock_when_close_triggers_run_finished(
    tmp_path: Path, factory_store: FactoryStore
) -> None:
    from resources.extensions.factory._job_management import FactoryJobManagement

    engine, store, _, groups, service, factory, worker = await _setup(tmp_path, factory_store)
    service.job_execution = engine  # type: ignore[attr-defined]
    service.job_triggers = SimpleNamespace()  # type: ignore[attr-defined]
    management = FactoryJobManagement(service)  # type: ignore[arg-type]

    job = await store.jobs.create_job(
        factory["id"], name="Deadlock Check", prompt="Long task", agent_member_id=worker["id"]
    )
    run = await store.jobs.create_run(factory["id"], job["id"], origin="manual")
    await engine.schedule(wip_limit=1)

    # Simulate host close_group synchronously invoking run_finished (which acquires service.lock).
    original_close = groups.close_group

    async def _draining_close(group_id: str, reason: str = "extension") -> Json:
        await engine.run_finished(_request_context(group_id, "run-1"), outcome="cancelled")
        return await original_close(group_id, reason)

    groups.close_group = _draining_close  # type: ignore[method-assign]

    result = await asyncio.wait_for(
        management.operation("job_run.cancel", {"run_id": run["id"]}),
        timeout=2.0,
    )
    assert result is not None
    assert result["run"]["state"] == "cancelled"
    assert (run["group_id"], "cancelled") in groups.closed
