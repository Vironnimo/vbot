"""Durable Accessor input through the public ChatLoop and real Sessions/Runs."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, override

import pytest

from core.chat.messages import ReplySurface
from core.runs import ChatRunManager, RunCancelledError
from core.sessions.errors import AccessorInputConflictError
from tests.core.chat.chat_loop_support import StubAdapter, StubAgent, StubRuntime, build_chat_loop
from tests.core.runs.runs_test_support import RunTimelines


class HeldFirstAdapter(StubAdapter):
    def __init__(self, responses=None):
        super().__init__(responses or [{"content": "first answer"}, {"content": "next answer"}])
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    @override
    async def send(self, messages, *, model_id, **kwargs):
        if not self.requests:
            self.entered.set()
            await self.release.wait()
        return await super().send(messages, model_id=model_id, **kwargs)


@asynccontextmanager
async def _harness(data_dir, adapter):
    runtime: Any = StubRuntime(
        data_dir=data_dir, agent=StubAgent(id="coder", model="openai/gpt-5.2"), adapter=adapter
    )
    # Production Runtime supplies the same withdrawal hook. Keep the standard
    # lightweight resolver/provider fixtures with real persistence and scheduling.
    runtime.chat_runs = ChatRunManager(
        persistence=runtime.chat_sessions,
        input_withdrawer=runtime.chat_sessions.remove_accessor_input_async,
    )
    runtime.chat_run_manager = runtime.chat_runs
    runtime.timelines = RunTimelines(runtime.chat_runs)
    try:
        yield runtime, build_chat_loop(runtime)
    finally:
        await runtime.chat_runs.aclose()
        runtime.chat_sessions.close()


async def _submit(loop, input_id, content, *, session_id=None, new_session=False):
    return await loop.submit_accessor_input(
        "coder",
        content,
        input_id=input_id,
        request_hash=f"original-{input_id}",
        session_id=session_id,
        new_session=new_session,
        reply_surface=ReplySurface.webui(),
    )


@pytest.mark.asyncio
async def test_retried_new_session_input_reuses_the_session_and_run(tmp_path):
    adapter = HeldFirstAdapter([{"content": "done"}])
    async with _harness(tmp_path, adapter) as (runtime, loop):
        first = await _submit(loop, "request", "original", new_session=True)
        await asyncio.wait_for(adapter.entered.wait(), 3)
        retry = await _submit(loop, "request", "original", new_session=True)
        assert first.run is not None and retry.run is first.run
        assert retry.record.address == first.record.address
        assert len(runtime.chat_sessions.list("coder")) == 1
        with pytest.raises(AccessorInputConflictError):
            await loop.submit_accessor_input(
                "coder", "changed", input_id="request", request_hash="conflict", new_session=True
            )
        assert len(runtime.chat_sessions.list("coder")) == 1
        adapter.release.set()
        await first.run.wait()
        completed = await loop.lookup_accessor_input("request", request_hash="original-request")
        assert completed.record.run_status == "completed"
        history = runtime.chat_sessions.get(first.record.address).load()
        assert [message.content for message in history if message.role == "user"] == ["original"]
        assert len(adapter.requests) == 1


@pytest.mark.asyncio
async def test_queue_restart_preserves_edit_removal_and_steering_priority(tmp_path):
    adapter = HeldFirstAdapter()
    async with _harness(tmp_path, adapter) as (runtime, loop):
        active = await _submit(loop, "active", "active input", new_session=True)
        await asyncio.wait_for(adapter.entered.wait(), 3)
        address = active.record.address
        edited = await _submit(loop, "edit", "before", session_id=address.session_id)
        removed = await _submit(loop, "remove", "removed input", session_id=address.session_id)
        selected = await _submit(loop, "selected", "selected input", session_id=address.session_id)
        assert await loop.update_accessor_queued(address, edited.record.queue_id, "after")
        assert await loop.remove_accessor_queued(address, removed.record.queue_id)
        await loop.steer_accessor_queued(address, selected.record.queue_id)
        stable_ids = {edited.record.queue_id, selected.record.queue_id}
        assert {item.item_id for _, item in runtime.chat_runs.all_queued()} == stable_ids

    resumed = StubAdapter([{"content": "selected answer"}, {"content": "edited answer"}])
    async with _harness(tmp_path, resumed) as (runtime, loop):
        started = asyncio.Queue()
        runtime.chat_runs.add_run_started_callback(started.put_nowait)
        await loop.restore_accessor_inputs()
        first = await asyncio.wait_for(started.get(), 3)
        await first.wait()
        first_input = await runtime.chat_sessions.lookup_accessor_input_async("selected")
        # Selection has the same priority as an ordinary Queue drain after an active Run ends.
        assert first_input.state == "started" and first_input.run_id == first.id
        second = await asyncio.wait_for(started.get(), 3)
        await second.wait()
        records = [
            await runtime.chat_sessions.lookup_accessor_input_async(input_id)
            for input_id in ("active", "edit", "remove", "selected")
        ]
        assert [record.state for record in records] == ["started", "started", "removed", "started"]
        assert {
            record.queue_id for record in records if record.input_id in {"edit", "selected"}
        } == stable_ids
        assert records[0].run_status == "cancelled"
        assert len(resumed.requests) == 2
        history = runtime.chat_sessions.get(records[0].address).load()
        assert [message.content for message in history if message.role == "user"] == [
            "active input",
            "selected input",
            "after",
        ]


@pytest.mark.asyncio
async def test_cancelled_submission_keeps_committed_queue_work_and_retry_identity(
    tmp_path, monkeypatch
):
    adapter = HeldFirstAdapter()
    async with _harness(tmp_path, adapter) as (runtime, loop):
        active = await _submit(loop, "active", "original", new_session=True)
        await asyncio.wait_for(adapter.entered.wait(), 3)
        committed = asyncio.Event()
        release_acceptance = asyncio.Event()
        accept = runtime.chat_sessions.accept_accessor_input_async

        async def paused_accept(*args, **kwargs):
            result = await accept(*args, **kwargs)
            committed.set()
            await release_acceptance.wait()
            return result

        monkeypatch.setattr(runtime.chat_sessions, "accept_accessor_input_async", paused_accept)
        submission = asyncio.create_task(
            _submit(loop, "queued", "kept", session_id=active.record.address.session_id)
        )
        await asyncio.wait_for(committed.wait(), 3)
        submission.cancel()
        release_acceptance.set()
        with pytest.raises(asyncio.CancelledError):
            await submission
        retry = await _submit(loop, "queued", "kept", session_id=active.record.address.session_id)
        assert retry.queued_item is not None
        assert [item.item_id for _, item in runtime.chat_runs.all_queued()] == [
            retry.record.queue_id
        ]
        adapter.release.set()
        await active.run.wait()
        queued_run = await retry.queued_item.future
        await queued_run.wait()
        assert len(adapter.requests) == 2
        history = runtime.chat_sessions.get(retry.record.address).load()
        assert [message.content for message in history if message.role == "user"] == [
            "original",
            "kept",
        ]


@pytest.mark.asyncio
async def test_selected_input_steers_once_and_never_returns_on_restart(tmp_path):
    adapter = HeldFirstAdapter()
    async with _harness(tmp_path, adapter) as (runtime, loop):
        active = await _submit(loop, "active", "original", new_session=True)
        await asyncio.wait_for(adapter.entered.wait(), 3)
        selected = await _submit(
            loop, "selected", "correction", session_id=active.record.address.session_id
        )
        await loop.steer_accessor_queued(active.record.address, selected.record.queue_id)
        adapter.release.set()
        await active.run.wait()
        retry = await loop.lookup_accessor_input("selected", request_hash="original-selected")
        assert retry.record.state == "steered" and retry.run is active.run
        assert len(adapter.requests) == 2

    unused = StubAdapter([])
    async with _harness(tmp_path, unused) as (runtime, loop):
        await loop.restore_accessor_inputs()
        retry = await loop.lookup_accessor_input("selected", request_hash="original-selected")
        assert retry.record.state == "steered" and retry.run is None
        assert unused.requests == [] and runtime.chat_runs.active_runs() == []


@pytest.mark.asyncio
async def test_stop_all_withdraws_durable_queue_before_restart(tmp_path):
    adapter = HeldFirstAdapter()
    async with _harness(tmp_path, adapter) as (runtime, loop):
        active = await _submit(loop, "active", "original", new_session=True)
        await asyncio.wait_for(adapter.entered.wait(), 3)
        address = active.record.address
        ordinary = await _submit(loop, "ordinary", "later", session_id=address.session_id)
        selected = await _submit(loop, "selected", "steering", session_id=address.session_id)
        await loop.steer_accessor_queued(address, selected.record.queue_id)
        assert (
            await runtime.chat_runs.clear_queued_async(
                address.agent_id, address.session_id, project_id=address.project_id
            )
            == 2
        )
        await runtime.chat_runs.cancel(active.run.id, reason="user")
        with pytest.raises(RunCancelledError):
            await active.run.wait()
        assert ordinary.queued_item.future.cancelled()
        assert selected.queued_item.future.cancelled()
        assert runtime.chat_runs.all_queued() == []

    unused = StubAdapter([])
    async with _harness(tmp_path, unused) as (runtime, loop):
        await loop.restore_accessor_inputs()
        for input_id, content in (("ordinary", "later"), ("selected", "steering")):
            retry = await _submit(loop, input_id, content, session_id=address.session_id)
            assert retry.record.state == "removed" and retry.queued_item is None
        assert unused.requests == [] and runtime.chat_runs.active_runs() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["waiting", "committed"])
async def test_queue_removal_cancellation_respects_mutation_admission(tmp_path, monkeypatch, phase):
    adapter = HeldFirstAdapter()
    async with _harness(tmp_path, adapter) as (runtime, loop):
        active = await _submit(loop, "active", "original", new_session=True)
        await asyncio.wait_for(adapter.entered.wait(), 3)
        address = active.record.address
        queued = await _submit(loop, "queued", "later", session_id=address.session_id)
        if phase == "waiting":
            attempting = asyncio.Event()
            mutation_boundary = runtime.chat_runs.queued_input_mutation

            @asynccontextmanager
            async def observed_boundary():
                attempting.set()
                async with mutation_boundary():
                    yield

            monkeypatch.setattr(runtime.chat_runs, "queued_input_mutation", observed_boundary)
            async with mutation_boundary():
                removal = asyncio.create_task(
                    loop.remove_accessor_queued(address, queued.record.queue_id)
                )
                await asyncio.wait_for(attempting.wait(), 3)
                removal.cancel()
                done, _ = await asyncio.wait((removal,), timeout=1)
            with pytest.raises(asyncio.CancelledError):
                await removal
            assert removal in done
            assert (
                await runtime.chat_sessions.lookup_accessor_input_async("queued")
            ).state == "queued"
            assert [item.item_id for _, item in runtime.chat_runs.all_queued()] == [
                queued.record.queue_id
            ]
        else:
            committed = asyncio.Event()
            release = asyncio.Event()
            remove = runtime.chat_sessions.remove_accessor_input_async

            async def paused_remove(*args, **kwargs):
                result = await remove(*args, **kwargs)
                committed.set()
                await release.wait()
                return result

            monkeypatch.setattr(runtime.chat_sessions, "remove_accessor_input_async", paused_remove)
            removal = asyncio.create_task(
                loop.remove_accessor_queued(address, queued.record.queue_id)
            )
            await asyncio.wait_for(committed.wait(), 3)
            removal.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await removal
            assert (
                await runtime.chat_sessions.lookup_accessor_input_async("queued")
            ).state == "removed"
            assert runtime.chat_runs.all_queued() == []
