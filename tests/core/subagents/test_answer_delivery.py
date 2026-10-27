"""Complete answers survive steering from real Chat Runs to the Parent Session."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, override

import pytest

from core.activity import SessionActivity
from core.automation import TriggerService
from core.chat import ChatMessage
from core.permissions import PermissionBoundary
from core.projects import AgentOverrides
from core.runs import SILENT_ANSWER, ChatRunManager, Run, RunAdmission, RunKind, RunNotFoundError
from core.sessions import SessionAddress
from core.storage import TemporaryFileManager
from core.subagents import SubAgentCoordinator
from core.subagents.links import link_session
from core.tools import ToolContext
from tests.core.chat.chat_loop_support import StubAdapter, StubAgent, StubRuntime, build_chat_loop


class _SteeringAdapter(StubAdapter):
    """Hold the first answer until the Parent has steered its Run."""

    def __init__(self) -> None:
        super().__init__(
            [
                {"content": "MAIN_REPORT", "phase": "final_answer"},
                {"content": "ADDENDUM", "phase": "final_answer"},
                {"content": "LATER_REPORT", "phase": "final_answer"},
            ]
        )
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.second_entered = asyncio.Event()
        self.second_release = asyncio.Event()

    @override
    async def send(
        self, messages: list[dict[str, Any]], *, model_id: str, **kwargs: Any
    ) -> dict[str, Any]:
        if not self.requests:
            self.entered.set()
            await self.release.wait()
        elif len(self.requests) == 1:
            self.second_entered.set()
            await self.second_release.wait()
        return await super().send(messages, model_id=model_id, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("background_completion", [False, True])
async def test_subagent_results_keep_parent_automation_settings_after_origin_eviction(
    tmp_path: Path, background_completion: bool
) -> None:
    class HeldAdapter(StubAdapter):
        def __init__(self) -> None:
            responses = ["Parent waiting", SILENT_ANSWER if background_completion else "Report"]
            if background_completion:
                responses.append("Background report")
            responses.extend(["Parent handled report", "Manual answer"])
            super().__init__([{"content": content} for content in responses])
            self.parent_entered, self.parent_release = asyncio.Event(), asyncio.Event()
            self.child_entered, self.child_release = asyncio.Event(), asyncio.Event()

        @override
        async def send(self, messages, *, model_id, **kwargs):
            if any(message.get("content") == "Child task" for message in messages):
                self.child_entered.set()
                await self.child_release.wait()
            elif any(message.get("content") == "Automate" for message in messages):
                self.parent_entered.set()
                await self.parent_release.wait()
            return await super().send(messages, model_id=model_id, **kwargs)

    adapter = HeldAdapter()
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=StubAgent(id="coder", model="openai/gpt-5.2", thinking_effort="low"),
        adapter=adapter,
    )
    runtime.storage.temporary_files = TemporaryFileManager(tmp_path)
    runtime.terminal_manager = runtime.command_manager = None
    sessions = runtime.chat_sessions
    manager = ChatRunManager(persistence=sessions, completed_run_retention_limit=1)
    runtime.chat_runs = runtime.chat_run_manager = manager
    loop = runtime.chat_loop = build_chat_loop(runtime)
    service = TriggerService(loop, manager, runtime, sessions=sessions)
    coordinator = SubAgentCoordinator(runtime, service)
    coordinator.install(manager)
    overrides = AgentOverrides(model="openai/gpt-mini", thinking_effort="high")
    parent = await loop.start_run_in_new_session(
        "coder",
        "Automate",
        run_kind=RunKind.CRON,
        schedule_id="automation",
        run_overrides=overrides,
    )
    other_release = asyncio.Event()

    async def other_work(_run):
        await other_release.wait()
        return "done"

    other = await manager.start(SessionAddress(None, "coder", "unrelated"), other_work)
    continuations: asyncio.Queue[Run] = asyncio.Queue()
    manager.add_run_started_callback(
        lambda run: (
            continuations.put_nowait(run)
            if run.session_id == parent.session_id and run.run_kind is RunKind.SYSTEM
            else None
        )
    )
    try:
        await asyncio.wait_for(adapter.parent_entered.wait(), 2)
        result = await coordinator.spawn(
            ToolContext(
                agent_id="coder",
                session_id=parent.session_id,
                run_id=parent.id,
                tool_call_id="delegate",
                tool_name="subagent",
                tool_call_index=0,
                workspace=tmp_path,
                vbot_root=tmp_path,
                data_root=tmp_path,
                permissions=PermissionBoundary.UNRESTRICTED,
            ),
            {
                "action": "run",
                "description": "Review",
                "content": "Child task",
                "model": "openai/gpt-5.2",
                "thinking_effort": "low",
            },
        )
        assert result["ok"], result
        child_address = SessionAddress(None, "coder", result["data"]["session_id"])
        child = manager.active_run(
            agent_id="coder", session_id=child_address.session_id, project_id=None
        )
        assert child is not None
        await asyncio.wait_for(adapter.child_entered.wait(), 2)
        assert child.parent_source is not None and child.parent_source.run_overrides == overrides
        assert child.run_overrides is None and child.schedule_id is None
        adapter.parent_release.set()
        await parent.wait()

        other_release.set()
        await other.wait()
        with pytest.raises(RunNotFoundError):
            manager.get(parent.id)
        adapter.child_release.set()
        await child.wait()
        if background_completion:
            await service.submit_completion(
                child_address,
                notice_id="child-command",
                origin_run_id=child.id,
                body="Child command completed",
            )
        follow_up = await asyncio.wait_for(continuations.get(), 2)
        answer = await follow_up.wait()
        assert answer.content == "Parent handled report"
        assert (answer.model, answer.thinking_effort) == ("openai/gpt-mini", "high")
        assert follow_up.schedule_id == "automation" and follow_up.run_overrides == overrides
        normal = await loop.start_run("coder", "Manual", session_id=parent.session_id)
        manual = await normal.wait()
        assert (manual.model, manual.thinking_effort) == ("openai/gpt-5.2", "low")
        child_settings = runtime.agent_resolver.session_overrides(child_address)
        assert (child_settings.model, child_settings.thinking_effort) == ("openai/gpt-5.2", "low")
    finally:
        adapter.parent_release.set()
        adapter.child_release.set()
        other_release.set()
        await manager.aclose()
        await service.aclose()
        await coordinator.drain_activity()
        sessions.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial_parent", "steered_parent"), [(True, True), (True, False), (False, True)]
)
async def test_steered_answers_arrive_once_in_order_and_later_runs_do_not_replay_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initial_parent: bool, steered_parent: bool
) -> None:
    adapter = _SteeringAdapter()
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=StubAgent(id="coder", model="openai/gpt-5.2"), adapter=adapter
    )
    runtime.storage.temporary_files = TemporaryFileManager(tmp_path)
    runtime.terminal_manager = None
    runtime.command_manager = None
    sessions, manager = runtime.chat_sessions, runtime.chat_run_manager
    parent = sessions.create("coder", session_id="parent")
    child = sessions.create("coder", session_id="child")
    link_session(
        sessions,
        child.address,
        subagent_id="sub-review",
        parent=parent.address,
        run_id="parent-run",
        tool_call_id="delegate",
        tool_call_index=0,
    )
    loop = build_chat_loop(runtime)
    runtime.chat_loop = loop
    service = TriggerService(loop, manager, runtime, sessions=sessions)
    coordinator = SubAgentCoordinator(runtime, service)
    coordinator.install(manager)
    activity = SessionActivity(
        runs=manager,
        commands=SimpleNamespace(
            add_changed_callback=lambda callback: lambda: None,
            pending_wakes=lambda address: 0,
        ),
        completions=service,
        subagents=coordinator,
    )
    submitted = asyncio.Queue[asyncio.Future[None]]()
    submit = service.submit_completion

    def record_submission(*args: Any, **kwargs: Any) -> asyncio.Future[None]:
        receipt = submit(*args, **kwargs)
        submitted.put_nowait(receipt)
        return receipt

    monkeypatch.setattr(service, "submit_completion", record_submission)
    parent_release = asyncio.Event()

    async def parent_work(run: Run) -> ChatMessage:
        await parent_release.wait()
        run.iteration_count += 1
        return ChatMessage.assistant(model="fixture", content="reviewed")

    parent_run = await manager.start(parent.address, parent_work)
    try:
        run = await manager.start(
            child.address,
            loop.run_executor("Review", parent_agent_input=initial_parent),
            admission=RunAdmission(
                run_kind=RunKind.SUBAGENT if initial_parent else RunKind.USER,
                parent_source=replace(parent_run.capture_continuation_source(), schedule_id="old"),
            ),
        )
        await asyncio.wait_for(adapter.entered.wait(), 5)
        if steered_parent:
            result = await coordinator.spawn(
                ToolContext(
                    agent_id="coder",
                    session_id=parent.id,
                    run_id=parent_run.id,
                    tool_call_id="send",
                    tool_name="subagent",
                    tool_call_index=0,
                    workspace=tmp_path,
                    vbot_root=tmp_path,
                    data_root=tmp_path,
                    permissions=PermissionBoundary.UNRESTRICTED,
                ),
                {
                    "action": "send",
                    "id": "sub-review",
                    "content": "Clarify",
                    "model": "openai/gpt-5.2",
                    "thinking_effort": "high",
                },
            )
            assert result["ok"] and result["data"]["status"] == "steered", result
            [item] = manager.list_queued("coder", "child", project_id=None)
            stored = await runtime.agent_resolver.session_overrides_async(child.address)
            assert (stored.model, stored.thinking_effort) == ("openai/gpt-5.2", "high")
        else:
            item = await manager.enqueue(
                child.address,
                loop.run_executor("Clarify", parent_agent_input=False),
                display_content="Clarify",
                steerable=True,
                admission=RunAdmission(run_kind=RunKind.SUBAGENT),
            )
            assert manager.steer_queued("coder", "child", item.item_id, project_id=None)
        adapter.release.set()
        await adapter.second_entered.wait()
        # MAIN_REPORT already committed to its original recipient. A later
        # user redirect cannot release the Parent's Goal while it is still
        # owed that answer, even before the steered Run ends and forwards it.
        link = await coordinator.parent_link(child.address)
        assert link is not None and link.reply_target == ("parent" if steered_parent else "user")
        assert await coordinator.parent_answer_pending(child.address) is initial_parent
        assert (await activity.pending_wakes(parent.address)).subagents == 1
        adapter.second_release.set()
        assert (await run.wait()).content == "ADDENDUM"
        assert await item.future is run
        assert run.parent_source == (
            parent_run.capture_continuation_source() if steered_parent else None
        )
        receipt = await asyncio.wait_for(submitted.get(), 5)
        assert not receipt.done()
        # A Run ending with Parent replies is read before, and without, delivery;
        # one ending with user replies stays unread for the user.
        summary = sessions.summary(child.address)
        assert summary is not None
        assert summary["has_unread_completion"] is (not steered_parent)
        [state] = await coordinator.subagents(parent.address)
        assert state.activity_file is not None
        activity_path = Path(state.activity_file)
        await coordinator.drain_activity()
        before = activity_path.read_text(encoding="utf-8")
        assert "Pending:" in before and "Delivered:" not in before
        assert await service.deliver_background_completions(parent_run, parent)
        await asyncio.wait_for(receipt, 5)
        assert not await service.deliver_background_completions(parent_run, parent)
        assert not await coordinator.parent_answer_pending(child.address)
        assert (await activity.pending_wakes(parent.address)).subagents == 0
        answers = [m for m in await child.load_run_messages_async(run.id) if m.role == "assistant"]
        assert [m.content for m in answers] == ["MAIN_REPORT", "ADDENDUM"]
        [notice] = [m for m in parent.load() if m.role == "note"]
        body = str(notice.content)
        assert body.count("MAIN_REPORT") == int(initial_parent)
        assert body.count("ADDENDUM") == int(steered_parent)
        if initial_parent and steered_parent:
            assert body.index("MAIN_REPORT") < body.index("ADDENDUM")
        for answer, forwarded in zip(answers, (initial_parent, steered_parent), strict=True):
            assert body.count(answer.id) == int(forwarded)
        await coordinator.drain_activity()
        log = activity_path.read_text(encoding="utf-8")
        assert "Delivered: stored in the Parent Session." in log
        assert all(
            log.count(answer.id) == 2 * int(forwarded)
            for answer, forwarded in zip(answers, (initial_parent, steered_parent), strict=True)
        )

        later = await manager.start(
            child.address,
            loop.run_executor("More", parent_agent_input=True),
            admission=RunAdmission(run_kind=RunKind.SUBAGENT),
        )
        await later.wait()
        receipt = await asyncio.wait_for(submitted.get(), 5)
        assert await service.deliver_background_completions(parent_run, parent)
        await receipt
        bodies = [str(m.content) for m in parent.load() if m.role == "note"]
        assert len(bodies) == 2
        assert "LATER_REPORT" in bodies[1]
        assert "MAIN_REPORT" not in bodies[1] and "ADDENDUM" not in bodies[1]
    finally:
        adapter.release.set()
        adapter.second_release.set()
        parent_release.set()
        await activity.aclose()
        await manager.aclose()
        await service.aclose()
        await coordinator.drain_activity()
        sessions.close()


@pytest.mark.asyncio
async def test_message_parent_resumes_final_answer_delivery_in_a_user_run(tmp_path: Path) -> None:
    from core.subagents import SubAgentCoordinator
    from core.tools import ToolRegistry
    from core.tools.subagent import register_subagent_tools
    from tests.core.subagents.subagents_test_support import RecordingTriggerService

    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_parent",
                        "name": "message_parent",
                        "arguments": {"content": "FOUND_CAUSE"},
                    }
                ],
            },
            {"content": "FIXED_REPORT", "phase": "final_answer"},
        ]
    )
    registry = ToolRegistry()
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=StubAgent(id="coder", model="openai/gpt-5.2"),
        adapter=adapter,
        tools=registry,
    )
    runtime.storage.temporary_files = TemporaryFileManager(tmp_path)
    runtime.terminal_manager = None
    runtime.command_manager = None
    sessions, manager = runtime.chat_sessions, runtime.chat_run_manager
    parent, child = sessions.create("coder"), sessions.create("coder")
    link_session(
        sessions,
        child.address,
        subagent_id="sub_fix",
        parent=parent.address,
        run_id="parent-run",
        tool_call_id=None,
        tool_call_index=None,
    )
    service = RecordingTriggerService()
    coordinator = SubAgentCoordinator(runtime, service)
    register_subagent_tools(registry, coordinator)
    coordinator.install(manager)
    runtime.subagent_reply_to_user = coordinator.subagent_reply_to_user
    loop = build_chat_loop(runtime)
    try:
        run = await loop.start_run("coder", "Here is why that failed", session_id=child.id)
        await run.wait()
        await asyncio.wait_for(service.wait_for_notice(f"subagent:{run.id}"), 5)
        notices = service.to(parent.address)
        assert sum("FOUND_CAUSE" in notice.body for notice in notices) == 1
        assert sum("FIXED_REPORT" in notice.body for notice in notices) == 1
        # The message travels with its display record; a forwarded answer has none.
        [record] = [notice.subagent_message for notice in notices if notice.subagent_message]
        assert record.delivery_id.startswith("subagent-message")
        assert record.to_dict() == {
            "subagent_id": "sub_fix",
            "title": "sub_fix",
            "content": "FOUND_CAUSE",
            "delivery_id": record.delivery_id,
            "agent_id": "coder",
            "session_id": child.id,
        }
        assert sessions.metadata_value(child.address, "subagent_reply_target") == "parent"
        [answer] = [
            m for m in child.load() if m.role == "assistant" and m.content == "FIXED_REPORT"
        ]
        assert answer.subagent_reply_target == "parent"
    finally:
        await manager.aclose()
        await coordinator.drain_activity()
        sessions.close()
