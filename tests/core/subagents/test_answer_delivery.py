"""Complete answers survive steering from real Chat Runs to the Parent Session."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, override

import pytest

from core.automation import TriggerService
from core.chat import ChatMessage
from core.runs import Run, RunAdmission, RunKind
from core.storage import TemporaryFileManager
from core.subagents.forwarding import SubAgentActivities, SubAgentForwarding
from core.subagents.links import link_session
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

    @override
    async def send(
        self, messages: list[dict[str, Any]], *, model_id: str, **kwargs: Any
    ) -> dict[str, Any]:
        if not self.requests:
            self.entered.set()
            await self.release.wait()
        return await super().send(messages, model_id=model_id, **kwargs)


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
    service = TriggerService(loop, manager, runtime, sessions=sessions)
    activities = SubAgentActivities(runtime)
    forwarding = SubAgentForwarding(runtime, service, activities)
    forwarding.install(manager)
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
            admission=RunAdmission(run_kind=RunKind.SUBAGENT if initial_parent else RunKind.USER),
        )
        await asyncio.wait_for(adapter.entered.wait(), 5)
        item = await manager.enqueue(
            child.address,
            loop.run_executor("Clarify", parent_agent_input=steered_parent),
            display_content="Clarify",
            steerable=True,
            admission=RunAdmission(run_kind=RunKind.SUBAGENT),
        )
        assert manager.steer_queued("coder", "child", item.item_id, project_id=None)
        adapter.release.set()
        assert (await run.wait()).content == "ADDENDUM"
        assert await item.future is run
        receipt = await asyncio.wait_for(submitted.get(), 5)
        assert not receipt.done()
        activity = await activities.ensure(child.address)
        assert activity is not None
        await activities.drain()
        before = activity.path.read_text(encoding="utf-8")
        assert "Pending:" in before and "Delivered:" not in before
        assert service.deliver_background_completions(parent_run, parent)
        await asyncio.wait_for(receipt, 5)
        assert not service.deliver_background_completions(parent_run, parent)
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
        summary = sessions.summary(child.address)
        assert summary is not None
        assert summary["has_unread_completion"] is (not steered_parent)
        await activities.drain()
        log = activity.path.read_text(encoding="utf-8")
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
        assert service.deliver_background_completions(parent_run, parent)
        await receipt
        bodies = [str(m.content) for m in parent.load() if m.role == "note"]
        assert len(bodies) == 2
        assert "LATER_REPORT" in bodies[1]
        assert "MAIN_REPORT" not in bodies[1] and "ADDENDUM" not in bodies[1]
    finally:
        adapter.release.set()
        parent_release.set()
        await manager.aclose()
        await service.aclose()
        await activities.drain()
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
        assert sessions.metadata_value(child.address, "subagent_reply_target") == "parent"
        [answer] = [
            m for m in child.load() if m.role == "assistant" and m.content == "FIXED_REPORT"
        ]
        assert answer.subagent_reply_target == "parent"
    finally:
        await manager.aclose()
        await coordinator.drain_activity()
        sessions.close()
