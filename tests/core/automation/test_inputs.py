"""Concrete program tasks through durable Chat acceptance and the shared Queue."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any, override

import pytest

from core.automation.inputs import DirectInputError, DirectInputNotFoundError, DirectInputService
from core.runs import ChatRunManager
from core.sessions.errors import AccessorInputConflictError
from tests.core.automation.cron_test_support import secret_store
from tests.core.chat.chat_loop_support import StubAdapter, StubAgent, StubRuntime, build_chat_loop


class HeldAdapter(StubAdapter):
    def __init__(self) -> None:
        super().__init__([{"content": "first"}, {"content": "second"}, {"content": "third"}])
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    @override
    async def send(self, messages: Any, *, model_id: str, **kwargs: Any) -> Any:
        if not self.requests:
            self.entered.set()
            await self.release.wait()
        return await super().send(messages, model_id=model_id, **kwargs)


@asynccontextmanager
async def harness(tmp_path, adapter=None):
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=StubAgent(id="coder", model="openai/base"),
        adapter=adapter or StubAdapter([{"content": "answer"}]),
    )
    runtime.chat_runs = ChatRunManager(
        persistence=runtime.chat_sessions,
        input_withdrawer=runtime.chat_sessions.remove_accessor_input_async,
    )
    runtime.chat_run_manager = runtime.chat_runs
    loop = build_chat_loop(runtime)
    store = secret_store(tmp_path)
    service = DirectInputService(
        tmp_path,
        chat=loop,
        sessions=runtime.chat_sessions,
        resolver=runtime.agent_resolver,
        secret_store=store,
    )
    try:
        yield runtime, service, store, loop
    finally:
        await runtime.chat_runs.aclose()
        runtime.chat_sessions.close()


@pytest.mark.asyncio
async def test_tasks_queue_continue_and_use_run_overrides_without_changing_defaults(
    tmp_path, monkeypatch
):
    adapter = HeldAdapter()
    async with harness(tmp_path, adapter) as (runtime, inputs, _store, _loop):
        inlet = await inputs.configure(
            None,
            {
                "name": "Build",
                "agent_id": "coder",
                "run_overrides": {"model": "openai/input", "thinking_effort": "low"},
            },
        )
        identity = inlet["id"]
        first = await inputs.submit(
            identity,
            {
                "prompt": "First",
                "run_overrides": {"model": "openai/request", "thinking_effort": "high"},
            },
            request_id="one",
        )
        await asyncio.wait_for(adapter.entered.wait(), 2)
        second_body = {"prompt": "Follow up", "session_id": first["session_id"]}
        second = await inputs.submit(identity, second_body, request_id="two")
        assert second["session_id"] == first["session_id"]
        assert second["status"] == "queued"
        # Waiting for the HTTP result must not cancel admitted work.
        deadline = await inputs.wait(identity, "one", 0)
        assert deadline["status"] == "running" and "content" not in deadline
        active_run = next(
            run for run in runtime.chat_runs.active_runs() if run.session_id == first["session_id"]
        )
        entered_wait = asyncio.Event()
        original_wait = active_run.wait

        async def observed_wait():
            entered_wait.set()
            return await original_wait()

        monkeypatch.setattr(active_run, "wait", observed_wait)
        waiting = asyncio.create_task(inputs.wait(identity, "one", 30))
        await asyncio.wait_for(entered_wait.wait(), 2)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        adapter.release.set()
        result = await inputs.wait(identity, "two", 2)
        assert result["status"] == "completed" and result["content"] == "second"
        assert (await inputs.result(identity, "one"))["content"] == "first"
        assert [request["model_id"] for request in adapter.requests] == ["request", "input"]
        assert [request["kwargs"]["thinking_effort"] for request in adapter.requests] == [
            "high",
            "low",
        ]
        metadata = await runtime.chat_sessions.get_metadata_async(
            (await runtime.chat_sessions.lookup_accessor_input_async(f"{identity}:one")).address
        )
        assert metadata["agent_overrides"]["model"] == "openai/base"
        third = await inputs.submit(identity, {"prompt": "Independent"}, request_id="three")
        assert third["session_id"] != first["session_id"]
        await inputs.wait(identity, "three", 2)


@pytest.mark.asyncio
async def test_receipts_are_scoped_retryable_and_survive_a_service_restart(tmp_path):
    async with harness(tmp_path) as (runtime, inputs, store, loop):
        first = await inputs.configure(None, {"name": "First", "agent_id": "coder"})
        second = await inputs.configure(None, {"name": "Second", "agent_id": "coder"})
        body = {"prompt": "Do this"}
        receipt = await inputs.submit(first["id"], body, request_id="retry")
        await inputs.wait(first["id"], "retry", 2)
        reopened = DirectInputService(
            tmp_path,
            chat=loop,
            sessions=runtime.chat_sessions,
            resolver=runtime.agent_resolver,
            secret_store=store,
        )
        replay = await reopened.submit(first["id"], {**body, "wait": True}, request_id="retry")
        assert replay["run_id"] == (await inputs.result(first["id"], "retry"))["run_id"]
        assert replay["session_id"] == receipt["session_id"]
        with pytest.raises(AccessorInputConflictError):
            await reopened.submit(first["id"], {"prompt": "Different"}, request_id="retry")
        with pytest.raises(DirectInputNotFoundError):
            await reopened.result(second["id"], "retry")
        with pytest.raises(DirectInputNotFoundError):
            await reopened.submit(
                second["id"],
                {"prompt": "Continue", "session_id": receipt["session_id"]},
                request_id="other",
            )


@pytest.mark.asyncio
async def test_configuration_preserves_future_fields_and_revokes_secrets(tmp_path):
    async with harness(tmp_path) as (_runtime, inputs, _store, _loop):
        row = await inputs.configure(None, {"name": "Tasks", "agent_id": "coder"})
        identity = row["id"]
        original = await inputs.secret(identity)
        assert await inputs.authenticate(identity, f"Bearer {original}", submitting=True)
        replacement = await inputs.rotate_secret(identity)
        assert not await inputs.authenticate(identity, f"Bearer {original}", submitting=True)
        assert await inputs.authenticate(identity, f"Bearer {replacement}", submitting=True)
        path = tmp_path / "automation" / "inputs.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["future"] = {"keep": True}
        document["inputs"][0]["future"] = 42
        path.write_text(json.dumps(document), encoding="utf-8")
        await inputs.configure(identity, {"enabled": False})
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["future"] == {"keep": True} and saved["inputs"][0]["future"] == 42
        assert not await inputs.authenticate(identity, f"Bearer {replacement}", submitting=True)
        assert await inputs.authenticate(identity, f"Bearer {replacement}", submitting=False)
        with pytest.raises(DirectInputError):
            await inputs.configure(identity, {"run_overrides": {"permission_ceiling": {}}})
        await inputs.delete(identity)
        assert not await inputs.authenticate(identity, f"Bearer {replacement}", submitting=False)
