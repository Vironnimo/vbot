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
async def test_steered_answers_arrive_once_in_order_and_later_runs_do_not_replay_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
            loop.run_executor("Review", parent_agent_input=True),
            admission=RunAdmission(run_kind=RunKind.SUBAGENT),
        )
        await asyncio.wait_for(adapter.entered.wait(), 5)
        item = await manager.enqueue(
            child.address,
            loop.run_executor("Clarify", parent_agent_input=True),
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
        assert body.count("MAIN_REPORT") == body.count("ADDENDUM") == 1
        assert body.index("MAIN_REPORT") < body.index("ADDENDUM")
        for answer in answers:
            assert body.count(answer.id) == 1
        await activities.drain()
        log = activity.path.read_text(encoding="utf-8")
        assert "Delivered: stored in the Parent Session." in log
        assert all(log.count(answer.id) == 2 for answer in answers)

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
