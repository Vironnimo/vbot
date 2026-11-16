"""Native Factory contracts through its real host, Chat and canonical Sessions."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, override

import pytest
import pytest_asyncio

from core.extensions.operations import ExtensionOperations
from core.runs import Run, RunCancelledError
from core.runtime.runtime import Runtime
from core.sessions import SessionAddress
from core.utils.config import Config
from tests.core.chat.chat_loop_adapter_support import StubAdapter
from tests.core.runtime.runtime_test_support import (
    extension_record,
    tool_context,
    write_agent_skill,
)

type Json = dict[str, Any]
MODEL = "openai/gpt-5.2"


class ControlledModel(StubAdapter):
    """Explicit request/answer rendezvous; no network or wall-clock polling."""

    def __init__(self) -> None:
        super().__init__([], on_demand_tools=False)
        self.pending: asyncio.Queue[Json] = asyncio.Queue()

    @override
    def request_context_kwargs(self, **context: Any) -> Json:
        return {"test_context": context}

    @override
    async def send(self, messages: list[Json], *, model_id: str, **kwargs: Any) -> Json:
        answer: asyncio.Future[Json] = asyncio.get_running_loop().create_future()
        request = {"messages": messages, "model_id": model_id, "kwargs": kwargs, "answer": answer}
        self.requests.append(request)
        self.pending.put_nowait(request)
        return await answer

    def tool(self, name: str, arguments: Json, *, request: Json | None = None) -> None:
        (request or self.requests[-1])["answer"].set_result(
            {
                "content": "",
                "tool_calls": [
                    {"id": f"call-{len(self.requests)}", "name": name, "arguments": arguments}
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
        )


@dataclass
class NativeFactoryRuntime:
    runtime: Runtime
    operations: ExtensionOperations
    model: ControlledModel
    service: Any

    async def operation(self, name: str, /, **arguments: Any) -> Json:
        return await self.operations.invoke(name, arguments)

    async def request(self) -> Json:
        async with asyncio.timeout(3):
            return await self.model.pending.get()

    async def settle(self) -> None:
        """Drain only Factory work and its canonical Runs, after answering requests."""
        async with asyncio.timeout(3):
            await self._settle()

    async def _settle(self) -> None:
        while True:
            tasks = [task for task in self.service._tasks if not task.done()]
            runs = self.runtime.chat_run_manager.active_runs()
            if not tasks and not runs:
                return
            outcomes = await asyncio.gather(
                *tasks, *(run.wait() for run in runs), return_exceptions=True
            )
            for outcome in outcomes:
                if isinstance(outcome, BaseException) and not isinstance(
                    outcome, RunCancelledError
                ):
                    raise outcome

    async def finish(self, run: Run) -> None:
        completed = asyncio.create_task(run.wait())
        extra_request = asyncio.create_task(self.model.pending.get())
        try:
            async with asyncio.timeout(3):
                done, _ = await asyncio.wait(
                    {completed, extra_request}, return_when=asyncio.FIRST_COMPLETED
                )
            assert completed in done, self.runtime.chat_sessions.get(
                SessionAddress(run.project_id, run.agent_id, run.session_id)
            ).load()
            await completed
        finally:
            for task in (completed, extra_request):
                task.cancel()
            await asyncio.gather(completed, extra_request, return_exceptions=True)

    async def drive(self, answer: Callable[[Json], Json]) -> None:
        """Answer concurrently admitted Sessions until this Factory becomes idle."""
        settled = asyncio.create_task(self.settle())
        pending: asyncio.Task[Json] | None = None
        try:
            async with asyncio.timeout(3):
                while not settled.done():
                    pending = asyncio.create_task(self.model.pending.get())
                    done, _ = await asyncio.wait(
                        {settled, pending}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if pending in done:
                        request = pending.result()
                        request["answer"].set_result(answer(request))
                    else:
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)
                await settled
        finally:
            if pending is not None:
                pending.cancel()
            settled.cancel()
            await asyncio.gather(
                settled, *([pending] if pending is not None else []), return_exceptions=True
            )


@pytest_asyncio.fixture
async def native_factory_runtime(
    tmp_path: Path,
    current_format_data_directory: None,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[NativeFactoryRuntime]:
    monkeypatch.setenv("OPENAI_API_KEY", "test-owned-factory-key")
    runtime = Runtime(Config(data_dir=tmp_path), operational_producers="disabled")
    model = ControlledModel()
    monkeypatch.setattr(runtime, "get_adapter", lambda _connection: model)
    runtime.start()
    try:
        await runtime.fire_extension_startup()
        record = extension_record(runtime, "factory")
        assert record.status == "loaded"
        operations = record.declarations.operations
        assert operations is not None
        tool = next(item for item in record.declarations.tools if item.name == "factory_card")
        service = tool.handler.__self__
        yield NativeFactoryRuntime(runtime, operations, model, service)
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_native_assignment_runs_the_real_identity_without_a_workflow(
    native_factory_runtime: NativeFactoryRuntime, tmp_path: Path
) -> None:
    harness = native_factory_runtime
    runtime = harness.runtime
    repository = tmp_path / "repository"
    repository.mkdir()
    runtime.projects.create("product", "Product", repository, sources=[])
    leader = runtime.agents.create("leader", model=MODEL, librarian_enabled=False)
    ordinary = runtime.chat_sessions.create(leader.id)
    factory = (
        await harness.operation("factory.create", project_id="product", leader_agent_id=leader.id)
    )["factory"]
    specialist = (
        await harness.operation(
            "member.create",
            factory_id=factory["id"],
            name="Implementer",
            model=MODEL,
            request_id="create-specialist",
        )
    )["member"]
    identity = runtime.agents.get(specialist["agent_id"])
    runtime.memory.add_entry(Path(identity.workspace), "agent", "test-owned-personal-memory")
    write_agent_skill(tmp_path, identity.id, "private-contract", "test-owned-private-skill")
    runtime.refresh_skills_for("product", identity.id)
    Path(identity.workspace).joinpath("SOUL.md").write_text(
        "test-owned-identity-sentinel", encoding="utf-8"
    )
    task = (
        await harness.operation(
            "task.create",
            factory_id=factory["id"],
            title="Native task",
            objective="test-owned-objective",
            conditions="test-owned-conditions",
            assignee_id=specialist["id"],
        )
    )["task"]
    request = await harness.request()
    runs = runtime.chat_run_manager.active_runs()
    assert len(runs) == 1
    run = runs[0]
    assert (run.agent_id, run.project_id, run.working_project_id) == (identity.id, None, "product")
    binding = runtime.chat_sessions.temporary_binding(
        SessionAddress(None, identity.id, run.session_id)
    )
    assert binding is not None
    resolved = runtime.agent_resolver.resolve_temporary_agent(
        binding.address, generation_id=binding.generation_id
    )
    assert resolved.id == identity.id
    assert resolved.workspace == identity.workspace
    text = str(request["messages"])
    assert "test-owned-identity-sentinel" in text
    assert "test-owned-private-skill" in text
    assert "test-owned-personal-memory" in text
    tools = {item["name"] for item in request["kwargs"].get("tools", [])}
    assert {"factory_tasks", "factory_topics"} <= tools
    assert not {"factory_members", "factory_settings"} & tools
    assert not {"factory_submit_work", "factory_report_blocked"} & tools
    assert (await harness.operation("workflow.list"))["workflows"] == []
    harness.model.tool(
        "factory_tasks",
        {
            "action": "complete",
            "id": task["id"],
            "outcome": "test-owned-outcome",
            "evidence": "test-owned-evidence",
        },
    )
    await harness.finish(run)
    await harness.settle()
    detail = await harness.operation("task.get", id=task["id"])
    assert detail["task"]["state"] == "done"
    assert len(detail["results"]) == 1
    assert detail["results"][0]["outcome"] == "test-owned-outcome"
    history = runtime.chat_sessions.get(binding.address).load()
    assert [message.name for message in history if message.role == "tool"] == ["factory_tasks"]
    assert runtime.chat_sessions.get(ordinary.address).load() == []
    assert runtime.agents.get(leader.id).workspace == leader.workspace
    ordinary_run = await runtime.chat_loop.start_run(
        leader.id, "test-owned-ordinary-input", session_id=ordinary.id
    )
    ordinary_request = await harness.request()
    ordinary_tools = {item["name"] for item in ordinary_request["kwargs"].get("tools", [])}
    assert (
        not {"factory_tasks", "factory_topics", "factory_members", "factory_settings"}
        & ordinary_tools
    )
    assert ordinary_run.execution_owner is None
    ordinary_request["answer"].set_result(
        {
            "content": "test-owned-ordinary-answer",
            "tool_calls": None,
            "usage": {"input_tokens": 10, "output_tokens": 1},
        }
    )
    await harness.finish(ordinary_run)
    assert harness.model.pending.empty()


@pytest.mark.asyncio
@pytest.mark.parametrize("pause_scope", ["task", "factory"])
async def test_native_pause_survives_restart_and_fences_the_previous_run(
    native_factory_runtime: NativeFactoryRuntime, tmp_path: Path, pause_scope: str
) -> None:
    harness = native_factory_runtime
    runtime = harness.runtime
    repository = tmp_path / "repository"
    repository.mkdir()
    runtime.projects.create("product", "Product", repository, sources=[])
    leader = runtime.agents.create("leader", model=MODEL, librarian_enabled=False)
    factory = (
        await harness.operation("factory.create", project_id="product", leader_agent_id=leader.id)
    )["factory"]
    task = (
        await harness.operation(
            "task.create",
            factory_id=factory["id"],
            title="Pause safely",
            objective="test-owned-paused-objective",
            conditions="test-owned-completion-condition",
            assignee_id=factory["leader_member_id"],
        )
    )["task"]
    await harness.request()
    run = runtime.chat_run_manager.active_runs()[0]
    old_context = replace(
        tool_context("factory_tasks", tmp_path, agent_id=run.agent_id),
        session_id=run.session_id,
        run_id=run.id,
        execution_owner=run.execution_owner,
        session_tool_grants=("factory_tasks",),
    )
    task = (await harness.operation("task.get", id=task["id"]))["task"]
    if pause_scope == "task":
        await harness.operation("task.pause", id=task["id"], expected_revision=task["revision"])
    else:
        current_factory = (await harness.operation("factory.get", id=factory["id"]))["factory"]
        await harness.operation(
            "factory.update",
            id=factory["id"],
            expected_revision=current_factory["revision"],
            state="paused",
        )
    await harness.settle()
    assert (await harness.operation("task.get", id=task["id"]))["task"]["state"] == "paused"
    topic = (
        await harness.operation(
            "topic.create",
            factory_id=factory["id"],
            task_id=task["id"],
            title="Unread while paused",
            member_ids=[factory["leader_member_id"]],
        )
    )["topic"]
    await harness.operation(
        "topic.post",
        id=topic["id"],
        text="test-owned-paused-unread",
        recipients=[factory["leader_member_id"]],
        source_receipt="paused-unread",
    )
    await harness.settle()
    assert len(harness.model.requests) == 1
    await runtime.aclose()
    runtime.start()
    await runtime.fire_extension_startup()
    record = extension_record(runtime, "factory")
    assert record.declarations.operations is not None
    harness.operations = record.declarations.operations
    harness.service = next(
        item for item in record.declarations.tools if item.name == "factory_card"
    ).handler.__self__
    await harness.settle()
    detail = await harness.operation("task.get", id=task["id"])
    assert detail["task"]["state"] == "paused"
    assert len(harness.model.requests) == 1
    assert [
        post["text"] for post in (await harness.operation("topic.get", id=topic["id"]))["posts"]
    ] == ["test-owned-paused-unread"]
    refused = await runtime.tools.dispatch(
        old_context,
        {
            "action": "complete",
            "id": task["id"],
            "outcome": "stale-outcome",
            "evidence": "stale-evidence",
        },
    )
    assert refused["error"]["code"] == "stale_run"
    assert (await harness.operation("task.get", id=task["id"]))["results"] == []
    if pause_scope == "task":
        await harness.operation(
            "task.resume", id=task["id"], expected_revision=detail["task"]["revision"]
        )
    else:
        current_factory = (await harness.operation("factory.get", id=factory["id"]))["factory"]
        await harness.operation(
            "factory.update",
            id=factory["id"],
            expected_revision=current_factory["revision"],
            state="active",
        )
    resumed_request = await harness.request()
    assert "test-owned-paused-unread" in str(resumed_request["messages"])
    resumed = runtime.chat_run_manager.active_runs()[0]
    assert resumed.id != run.id
    harness.model.tool(
        "factory_tasks",
        {
            "action": "complete",
            "id": task["id"],
            "outcome": "resumed-outcome",
            "evidence": "resumed-evidence",
        },
    )
    await harness.finish(resumed)
    await harness.settle()
    assert (await harness.operation("task.get", id=task["id"]))["task"]["state"] == "done"
    assert len(harness.model.requests) == 2


@pytest.mark.asyncio
async def test_native_peer_invitation_and_user_posts_deliver_once_to_the_same_session(
    native_factory_runtime: NativeFactoryRuntime, tmp_path: Path
) -> None:
    harness = native_factory_runtime
    runtime = harness.runtime
    repository = tmp_path / "repository"
    repository.mkdir()
    runtime.projects.create("product", "Product", repository, sources=[])
    leader = runtime.agents.create("leader", model=MODEL, librarian_enabled=False)
    peer = runtime.agents.create("peer", model=MODEL, librarian_enabled=False)
    factory = (
        await harness.operation("factory.create", project_id="product", leader_agent_id=leader.id)
    )["factory"]
    member = (await harness.operation("member.add", factory_id=factory["id"], agent_id=peer.id))[
        "member"
    ]
    topic = (
        await harness.operation(
            "topic.create",
            factory_id=factory["id"],
            title="Direct consultation",
            member_ids=[factory["leader_member_id"]],
            text="test-owned-opening-post",
        )
    )["topic"]
    first = await harness.request()
    assert first["kwargs"]["test_context"]["agent_id"] == leader.id
    harness.model.tool(
        "factory_topics",
        {
            "action": "invite",
            "id": topic["id"],
            "member_ids": [member["id"]],
            "text": "test-owned-peer-invitation",
        },
        request=first,
    )
    peer_requests: list[Json] = []

    def reply(request: Json) -> Json:
        if request["kwargs"]["test_context"]["agent_id"] == peer.id:
            peer_requests.append(request)
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": f"peer-reply-{len(peer_requests)}",
                        "name": "factory_topics",
                        "arguments": {
                            "action": "post",
                            "id": topic["id"],
                            "text": f"test-owned-peer-answer-{len(peer_requests)}",
                            "recipients": [factory["leader_member_id"]],
                        },
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
        return {
            "content": "[SILENT]",
            "tool_calls": None,
            "usage": {"input_tokens": 10, "output_tokens": 1},
        }

    await harness.drive(reply)
    assert len(peer_requests) == 1
    assert "test-owned-peer-invitation" in str(peer_requests[0]["messages"])
    first_address = peer_requests[0]["kwargs"]["test_context"]["session_id"]
    posted = {
        "id": topic["id"],
        "text": "test-owned-user-followup",
        "recipients": [member["id"]],
        "source_receipt": "user-followup-receipt",
    }
    await harness.operation("topic.post", **posted)
    await harness.drive(reply)
    assert len(peer_requests) == 2
    assert peer_requests[1]["kwargs"]["test_context"]["session_id"] == first_address
    assert "test-owned-user-followup" in str(peer_requests[1]["messages"])
    request_count = len(harness.model.requests)
    await harness.operation("topic.post", **posted)
    await harness.settle()
    assert len(harness.model.requests) == request_count
    detail = await harness.operation("topic.get", id=topic["id"])
    assert [post["text"] for post in detail["posts"]].count("test-owned-user-followup") == 1
    assert [
        post["text"] for post in detail["posts"] if post["author"] == f"member:{member['id']}"
    ] == ["test-owned-peer-answer-1", "test-owned-peer-answer-2"]
    history = runtime.chat_sessions.get(SessionAddress(None, peer.id, first_address)).load()
    assert len([message for message in history if message.role == "run_summary"]) == 2
    assert (
        sum(
            "test-owned-user-followup" in (message.content or "")
            for message in history
            if message.role in {"user", "note"}
        )
        == 1
    )
    runtime.agents.create("new-peer", model=MODEL, librarian_enabled=False)
    newcomer = (
        await harness.operation("member.add", factory_id=factory["id"], agent_id="new-peer")
    )["member"]
    current_topic = (await harness.operation("topic.get", id=topic["id"]))["topic"]
    before_invitation = len(harness.model.requests)
    await harness.operation(
        "topic.invite",
        id=topic["id"],
        expected_revision=current_topic["revision"],
        member_ids=[newcomer["id"]],
        text="test-owned-later-invitation",
    )
    invitation = await harness.request()
    assert invitation["kwargs"]["test_context"]["agent_id"] == "new-peer"
    context = str(invitation["messages"])
    assert "test-owned-opening-post" in context
    assert "test-owned-peer-answer-1" in context
    assert "test-owned-peer-answer-2" in context
    assert f"member:{member['id']}" in context
    assert "test-owned-later-invitation" in context
    invitation["answer"].set_result(
        {
            "content": "[SILENT]",
            "tool_calls": None,
            "usage": {"input_tokens": 10, "output_tokens": 1},
        }
    )
    await harness.settle()
    assert len(harness.model.requests) == before_invitation + 1
    native = harness.service.records().native
    for index in range(105):
        await native.add_post(
            topic["id"], f"test-owned-old-history-{index}", author=f"member:{member['id']}"
        )
    await native.add_post(
        topic["id"], "test-owned-recent-question", author=f"member:{member['id']}"
    )
    await native.add_post(topic["id"], "test-owned-recent-answer", author=f"member:{member['id']}")
    runtime.agents.create("latest-peer", model=MODEL, librarian_enabled=False)
    latest_peer = (
        await harness.operation("member.add", factory_id=factory["id"], agent_id="latest-peer")
    )["member"]
    current_topic = (await harness.operation("topic.get", id=topic["id"]))["topic"]
    before_invitation = len(harness.model.requests)
    await harness.operation(
        "topic.invite",
        id=topic["id"],
        expected_revision=current_topic["revision"],
        member_ids=[latest_peer["id"]],
        text="test-owned-recent-invitation",
    )
    invitation = await harness.request()
    assert invitation["kwargs"]["test_context"]["agent_id"] == "latest-peer"
    context = str(invitation["messages"])
    assert "test-owned-recent-question" in context
    assert "test-owned-recent-answer" in context
    assert "test-owned-recent-invitation" in context
    assert "test-owned-old-history-0" not in context
    assert "test-owned-opening-post" not in context
    invitation["answer"].set_result(
        {
            "content": "[SILENT]",
            "tool_calls": None,
            "usage": {"input_tokens": 10, "output_tokens": 1},
        }
    )
    await harness.settle()
    harness.service.request_schedule()
    await harness.settle()
    assert len(harness.model.requests) == before_invitation + 1
    history = await native.list_posts(topic["id"], after=0, limit=1000)
    assert history[0]["text"] == "test-owned-opening-post"
    latest_sequence = history[-1]["sequence"]
    markers = await native.topic_participants(topic["id"])
    assert (
        next(item for item in markers if item["member_id"] == latest_peer["id"])["read_sequence"]
        == latest_sequence
    )


@pytest.mark.asyncio
async def test_native_consultation_wakes_the_task_owner_without_a_leader_relay(
    native_factory_runtime: NativeFactoryRuntime, tmp_path: Path
) -> None:
    harness = native_factory_runtime
    runtime = harness.runtime
    repository = tmp_path / "repository"
    repository.mkdir()
    runtime.projects.create("product", "Product", repository, sources=[])
    leader = runtime.agents.create("leader", model=MODEL, librarian_enabled=False)
    factory = (
        await harness.operation("factory.create", project_id="product", leader_agent_id=leader.id)
    )["factory"]
    members: list[Json] = []
    for agent_id in ("implementer", "reviewer"):
        runtime.agents.create(agent_id, model=MODEL, librarian_enabled=False)
        members.append(
            (await harness.operation("member.add", factory_id=factory["id"], agent_id=agent_id))[
                "member"
            ]
        )
    implementer, reviewer = members
    task = (
        await harness.operation(
            "task.create",
            factory_id=factory["id"],
            title="Consult a peer",
            objective="test-owned-task-consultation",
            conditions="test-owned-peer-and-user-evidence",
            assignee_id=implementer["id"],
        )
    )["task"]
    initial = await harness.request()
    initial_run = runtime.chat_run_manager.active_runs()[0]
    origin_session = initial_run.session_id
    harness.model.tool(
        "factory_topics",
        {
            "action": "create",
            "title": "Peer question",
            "task_id": task["id"],
            "member_ids": [reviewer["id"]],
            "text": "test-owned-direct-question",
        },
        request=initial,
    )
    peer_request: Json | None = None
    while True:
        request = await harness.request()
        context = request["kwargs"]["test_context"]
        if context["agent_id"] == reviewer["agent_id"]:
            assert peer_request is None
            peer_request = request
        else:
            assert context["session_id"] == origin_session
            request["answer"].set_result(
                {
                    "content": "test-owned-await-peer",
                    "tool_calls": None,
                    "usage": {"input_tokens": 10, "output_tokens": 1},
                }
            )
            await initial_run.wait()
            break
    blocked = (await harness.operation("task.get", id=task["id"]))["task"]
    assert (blocked["state"], blocked["reason"]) == ("blocked", "input")
    if peer_request is None:
        peer_request = await harness.request()
    assert peer_request["kwargs"]["test_context"]["agent_id"] == reviewer["agent_id"]
    topic = (await harness.operation("topic.list", factory_id=factory["id"]))["topics"][0]
    harness.model.tool(
        "factory_topics",
        {
            "action": "post",
            "id": topic["id"],
            "text": "test-owned-consultation-answer",
            "recipients": [implementer["id"]],
        },
        request=peer_request,
    )
    resumed_requests: list[Json] = []

    def wait_for_user(request: Json) -> Json:
        assert request["kwargs"]["test_context"]["session_id"] == origin_session
        assert "test-owned-consultation-answer" in str(request["messages"])
        resumed_requests.append(request)
        return {
            "content": "test-owned-await-user",
            "tool_calls": None,
            "usage": {"input_tokens": 10, "output_tokens": 1},
        }

    await harness.drive(wait_for_user)
    assert len(resumed_requests) == 1
    before_user = len(harness.model.requests)
    await harness.operation(
        "topic.post",
        id=topic["id"],
        text="test-owned-user-task-info",
        recipients=[implementer["id"]],
        source_receipt="task-user-info",
    )

    def complete(request: Json) -> Json:
        assert request["kwargs"]["test_context"]["session_id"] == origin_session
        assert "test-owned-user-task-info" in str(request["messages"])
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "consultation-complete",
                    "name": "factory_tasks",
                    "arguments": {
                        "action": "complete",
                        "id": task["id"],
                        "outcome": "test-owned-consultation-result",
                        "evidence": "test-owned-peer-and-user-evidence",
                    },
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }

    await harness.drive(complete)
    assert len(harness.model.requests) == before_user + 1
    assert (await harness.operation("task.get", id=task["id"]))["task"]["state"] == "done"
    harness.service.request_schedule()
    await harness.settle()
    assert len(harness.model.requests) == before_user + 1
    assert all(
        request["kwargs"]["test_context"]["agent_id"] != leader.id
        for request in harness.model.requests
    )
    history = runtime.chat_sessions.get(
        SessionAddress(None, implementer["agent_id"], origin_session)
    ).load()
    assert len([message for message in history if message.role == "run_summary"]) == 3
    assert (
        sum(
            "test-owned-consultation-answer" in (message.content or "")
            for message in history
            if message.role == "note"
        )
        == 1
    )
    assert (
        sum(
            "test-owned-user-task-info" in (message.content or "")
            for message in history
            if message.role == "note"
        )
        == 1
    )


@pytest.mark.asyncio
async def test_native_parent_owns_completion_after_dependent_children(
    native_factory_runtime: NativeFactoryRuntime, tmp_path: Path
) -> None:
    harness = native_factory_runtime
    runtime = harness.runtime
    repository = tmp_path / "repository"
    repository.mkdir()
    runtime.projects.create("product", "Product", repository, sources=[])
    leader = runtime.agents.create("leader", model=MODEL, librarian_enabled=False)
    factory = (
        await harness.operation("factory.create", project_id="product", leader_agent_id=leader.id)
    )["factory"]
    members: list[Json] = []
    for agent_id in ("first-worker", "second-worker"):
        runtime.agents.create(agent_id, model=MODEL, librarian_enabled=False)
        members.append(
            (await harness.operation("member.add", factory_id=factory["id"], agent_id=agent_id))[
                "member"
            ]
        )
    parent = (
        await harness.operation(
            "task.create",
            factory_id=factory["id"],
            title="Integrate children",
            objective="test-owned-parent-objective",
            conditions="test-owned-parent-integrated-evidence",
            assignee_id=factory["leader_member_id"],
        )
    )["task"]
    parent_request = await harness.request()
    parent_run = runtime.chat_run_manager.active_runs()[0]
    parent_address = SessionAddress(None, leader.id, parent_run.session_id)
    held: dict[str, Json] = {}

    async def next_parent() -> Json:
        while True:
            request = await harness.request()
            context = request["kwargs"]["test_context"]
            if context["agent_id"] == leader.id:
                assert context["session_id"] == parent_address.session_id
                return request
            assert context["agent_id"] not in held
            held[context["agent_id"]] = request

    harness.model.tool(
        "factory_tasks",
        {
            "action": "create",
            "parent_id": parent["id"],
            "title": "First child",
            "objective": "test-owned-first-child",
            "conditions": "test-owned-first-evidence",
            "assignee_id": members[0]["id"],
        },
        request=parent_request,
    )
    parent_request = await next_parent()
    children = (await harness.operation("task.get", id=parent["id"]))["children"]
    assert len(children) == 1
    first = children[0]
    harness.model.tool(
        "factory_tasks",
        {
            "action": "create",
            "parent_id": parent["id"],
            "title": "Dependent child",
            "objective": "test-owned-second-child",
            "conditions": "test-owned-second-evidence",
            "assignee_id": members[1]["id"],
            "dependencies": [first["id"]],
        },
        request=parent_request,
    )
    parent_request = await next_parent()
    children = (await harness.operation("task.get", id=parent["id"]))["children"]
    second = next(child for child in children if child["id"] != first["id"])
    assert second["dependencies"] == [first["id"]]
    assert second["state"] in {"ready", "blocked"}
    assert not any(
        request["kwargs"]["test_context"]["agent_id"] == "second-worker"
        for request in harness.model.requests
    )
    harness.model.tool(
        "factory_tasks",
        {
            "action": "complete",
            "id": parent["id"],
            "outcome": "premature-parent-outcome",
            "evidence": "premature-parent-evidence",
        },
        request=parent_request,
    )
    parent_request = await next_parent()
    tool_results = [
        message
        for message in runtime.chat_sessions.get(parent_address).load()
        if message.role == "tool"
    ]
    refused = tool_results[-1].content
    assert isinstance(refused, str)
    assert json.loads(refused)["error"]["code"] == "children_unfinished"
    assert (await harness.operation("task.get", id=parent["id"]))["results"] == []
    parent_request["answer"].set_result(
        {
            "content": "test-owned-await-children",
            "tool_calls": None,
            "usage": {"input_tokens": 10, "output_tokens": 1},
        }
    )
    await parent_run.wait()
    if "first-worker" not in held:
        first_request = await harness.request()
        assert first_request["kwargs"]["test_context"]["agent_id"] == "first-worker"
        held["first-worker"] = first_request
    assert (await harness.operation("task.get", id=second["id"]))["task"]["state"] == "blocked"
    harness.model.tool(
        "factory_tasks",
        {
            "action": "complete",
            "id": first["id"],
            "outcome": "test-owned-first-result",
            "evidence": "test-owned-first-evidence",
        },
        request=held.pop("first-worker"),
    )
    parent_request = await next_parent()
    assert "test-owned-first-result" in str(parent_request["messages"])
    assert (await harness.operation("task.get", id=parent["id"]))["task"]["state"] != "done"
    parent_request["answer"].set_result(
        {
            "content": "test-owned-await-second-child",
            "tool_calls": None,
            "usage": {"input_tokens": 10, "output_tokens": 1},
        }
    )
    current_parent = runtime.chat_run_manager.active_run(
        agent_id=leader.id, session_id=parent_address.session_id, project_id=None
    )
    assert current_parent is not None
    await current_parent.wait()
    if "second-worker" not in held:
        second_request = await harness.request()
        assert second_request["kwargs"]["test_context"]["agent_id"] == "second-worker"
        held["second-worker"] = second_request
    assert (await harness.operation("task.get", id=first["id"]))["task"]["state"] == "done"
    harness.model.tool(
        "factory_tasks",
        {
            "action": "complete",
            "id": second["id"],
            "outcome": "test-owned-second-result",
            "evidence": "test-owned-second-evidence",
        },
        request=held.pop("second-worker"),
    )
    parent_request = await next_parent()
    assert "test-owned-second-result" in str(parent_request["messages"])
    detail = await harness.operation("task.get", id=parent["id"])
    assert detail["task"]["state"] != "done"
    assert detail["results"] == []
    assert {child["state"] for child in detail["children"]} == {"done"}
    current_parent = runtime.chat_run_manager.active_run(
        agent_id=leader.id, session_id=parent_address.session_id, project_id=None
    )
    assert current_parent is not None
    harness.model.tool(
        "factory_tasks",
        {
            "action": "complete",
            "id": parent["id"],
            "outcome": "test-owned-integrated-result",
            "evidence": "test-owned-parent-integrated-evidence",
        },
        request=parent_request,
    )
    await harness.finish(current_parent)
    await harness.settle()
    detail = await harness.operation("task.get", id=parent["id"])
    assert detail["task"]["state"] == "done"
    assert [result["outcome"] for result in detail["results"]] == ["test-owned-integrated-result"]
    assert not held


@pytest.mark.asyncio
async def test_owned_descendant_cannot_expand_the_factory_skill_selection(
    native_factory_runtime: NativeFactoryRuntime, tmp_path: Path
) -> None:
    harness = native_factory_runtime
    runtime = harness.runtime
    repository = tmp_path / "repository"
    repository.mkdir()
    runtime.projects.create("product", "Product", repository, sources=[])
    leader = runtime.agents.create("leader", model=MODEL, librarian_enabled=False)
    peer = runtime.agents.create("broad-peer", model=MODEL, librarian_enabled=False)
    write_agent_skill(tmp_path, peer.id, "permitted", "test-permitted-private-description")
    write_agent_skill(tmp_path, peer.id, "withheld", "test-withheld-private-description")
    runtime.refresh_skills_for("product", peer.id)
    factory = (
        await harness.operation(
            "factory.create",
            project_id="product",
            leader_agent_id=leader.id,
            config={"allowed_skills": ["permitted"], "allowed_tools": ["subagent", "skill"]},
        )
    )["factory"]
    task = (
        await harness.operation(
            "task.create",
            factory_id=factory["id"],
            title="Bound delegated Skills",
            objective="test-owned-delegation",
            conditions="test-owned-result",
            assignee_id=factory["leader_member_id"],
        )
    )["task"]
    initial = await harness.request()
    root_run = runtime.chat_run_manager.active_runs()[0]
    harness.model.tool(
        "subagent",
        {
            "agent_id": peer.id,
            "content": "Inspect the assigned work",
            "description": "Inspect bounded Skills",
        },
        request=initial,
    )
    held = [await harness.request(), await harness.request()]
    child = next(item for item in held if item["kwargs"]["test_context"]["agent_id"] == peer.id)
    parent = next(item for item in held if item["kwargs"]["test_context"]["agent_id"] == leader.id)
    child_text = str(child["messages"])
    assert "test-permitted-private-description" in child_text
    assert "test-withheld-private-description" not in child_text
    child_tools = {item["name"] for item in child["kwargs"].get("tools", [])}
    assert "skill" in child_tools
    assert (
        not {"factory_tasks", "factory_topics", "factory_members", "factory_settings"} & child_tools
    )
    harness.model.tool("skill", {"name": "withheld"}, request=child)
    refused = await harness.request()
    assert refused["kwargs"]["test_context"]["agent_id"] == peer.id
    child_results = [message for message in refused["messages"] if message.get("role") == "tool"]
    assert child_results
    assert "test-withheld-private-description" not in str(child_results)
    assert "skill_not_allowed" in str(child_results) or "skill_not_found" in str(child_results)
    refused["answer"].set_result({"content": "test-owned-child-result", "tool_calls": None})
    child_run = next(
        run for run in runtime.chat_run_manager.active_runs() if run.agent_id == peer.id
    )
    await harness.finish(child_run)
    harness.model.tool(
        "factory_tasks",
        {
            "action": "complete",
            "id": task["id"],
            "outcome": "test-owned-result",
            "evidence": "test-owned-evidence",
        },
        request=parent,
    )
    await harness.finish(root_run)
    await harness.settle()
    assert (await harness.operation("task.get", id=task["id"]))["task"]["state"] == "done"
