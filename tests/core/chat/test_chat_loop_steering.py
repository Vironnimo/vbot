"""Steering preserves Run identity and ordered Model/Tool boundaries."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast, override

import pytest

from core.attachments import AttachmentStore
from core.chat._queued_input import STEERING_SYSTEM_REMINDER
from core.chat.block_resolver import ContentBlockResolver
from core.chat.content_blocks import MediaBlock
from core.chat.messages import ChatMessage, InputOrigin
from core.chat.wire_shaping import system_reminder_request_message
from core.model_tasks.speech import SpeechError
from core.providers.errors import NetworkError
from core.runs import PROVIDER_REQUEST_STATUS_EVENT, RunCancelledError, RunKind, RunStatus
from core.tools import ToolRegistry, tool_success
from tests.core.chat.chat_loop_compaction_test_support import compaction_runtime
from tests.core.chat.chat_loop_support import (
    PolicyStubAdapter,
    StubAdapter,
    StubAgent,
    StubCompactionService,
    StubRuntime,
    build_chat_loop,
    build_request_messages,
    session_address,
)


def _steering_notes(messages: list[ChatMessage]) -> list[ChatMessage]:
    return [
        message
        for message in messages
        if message.role == "note" and message.content == STEERING_SYSTEM_REMINDER
    ]


class SteeringAdapter(StubAdapter):
    """Select one new steering input while each chosen request is in flight."""

    def __init__(self, responses: list[Any], *, steer_requests: range) -> None:
        super().__init__(responses)
        self.steer_requests = steer_requests
        self.loop: Any = None
        self.runtime: Any = None

    @override
    async def send(
        self, messages: list[dict[str, Any]], *, model_id: str, **kwargs: Any
    ) -> dict[str, Any]:
        index = len(self.requests)
        if index in self.steer_requests:
            item = await self.loop.queue_run("coder", f"Steer {index}", session_id="one")
            self.runtime.chat_run_manager.steer_queued(
                "coder", "one", item.item_id, project_id=None
            )
        return await super().send(messages, model_id=model_id, **kwargs)


class PausedAdapter(PolicyStubAdapter):
    """Hold the first request until released; reasoning replays only within the Run."""

    def __init__(self, responses: list[Any]) -> None:
        super().__init__(responses, policy="current_run")
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
@pytest.mark.parametrize("tool_step", [False, True])
@pytest.mark.parametrize("origin", [None, "speech_transcription", "live_voice"])
async def test_steer_keeps_same_run_and_follows_complete_tool_batch(
    tmp_path: Path, tool_step: bool, origin: InputOrigin | None
) -> None:
    first = {
        "content": "Before",
        "tool_calls": [
            {"id": "a", "name": "probe", "arguments": {}},
            {"id": "b", "name": "probe", "arguments": {}},
        ]
        if tool_step
        else None,
    }
    adapter = PausedAdapter([first, {"content": "After", "tool_calls": None}])
    tools = ToolRegistry()
    tools.register(
        "probe", "Probe", {"type": "object"}, lambda _ctx, _args: tool_success({"done": True})
    )
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["probe"])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)
    runtime.chat_sessions.create("coder", session_id="one")
    loop = build_chat_loop(runtime)
    run = await loop.start_run("coder", "Original", session_id="one")
    await asyncio.wait_for(adapter.entered.wait(), 5)
    ordinary = await loop.queue_run("coder", "Later", session_id="one")
    items = [
        await loop.queue_run("coder", content, session_id="one", input_origin=origin)
        for content in ["Steer one", "Steer two"]
    ]
    for item in reversed(items):
        runtime.chat_run_manager.steer_queued("coder", "one", item.item_id, project_id=None)
    assert all(not item.future.done() for item in items)
    assert runtime.chat_run_manager.remove_queued("coder", "one", ordinary.item_id, project_id=None)
    adapter.release.set()
    result = await asyncio.wait_for(run.wait(), 10)
    assert result.content == "After"
    assert run.iteration_count == 2
    assert all(item.future.result() is run for item in items)
    assert runtime.chat_run_manager.list_queued("coder", "one", project_id=None) == []
    session = runtime.chat_sessions.get(session_address("coder", "one"))
    history = session.load()
    visible = [m for m in history if m.role in {"user", "assistant", "tool"}]
    assert [m.role for m in visible] == (
        ["user", "assistant", "tool", "tool", "user", "user", "assistant"]
        if tool_step
        else ["user", "assistant", "user", "user", "assistant"]
    )
    assert [m.content for m in visible if m.role == "user"] == [
        "Original",
        "Steer one",
        "Steer two",
    ]
    steering_notes = _steering_notes(history)
    assert len(steering_notes) == 2
    for note in steering_notes:
        user = history[history.index(note) + 1]
        assert user.role == "user"
        assert user.input_origin == origin
        assert note.run_id == user.run_id == run.id
    sent = adapter.requests[1]["messages"]
    reminder = system_reminder_request_message(STEERING_SYSTEM_REMINDER)["content"]
    for content in ["Steer one", "Steer two"]:
        index = next(i for i, message in enumerate(sent) if message["content"] == content)
        assert sent[index]["role"] == "user"
        assert sent[index - 1]["role"] == "user"
        assert sent[index - 1]["content"].endswith(reminder)
        assert sent[index - 1]["content"].count("<system-reminder>") == 1 + bool(origin)
    assert not any(
        reminder in str(message.get("content")) for message in adapter.requests[0]["messages"]
    )
    replayed = await build_request_messages(loop, agent, session)
    assert replayed[-5:-1] == sent[-4:]
    assert len([m for m in history if m.role == "run_summary"]) == 1
    assert [
        e.payload.get("queue_item_id")
        for e in await runtime.timelines.events(run)
        if e.type == "user_message_persisted"
    ] == [None, items[0].item_id, items[1].item_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("steering_transcription", ["success", "failure", "empty"])
async def test_voice_input_persists_with_its_transcription(
    tmp_path: Path, steering_transcription: str
) -> None:
    # Speech-to-text runs before a voice message persists, whether it starts the
    # Run or steers it, so history, accessors and every request carry what was said.
    class Transcriber:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def transcribe(self, audio: bytes, *, filename: str, media_type: str) -> object:
            self.calls.append(filename)
            if filename == "b.ogg":
                assert not runtime.chat_sessions.write_lock(
                    session_address("coder", "one")
                ).locked()
                if steering_transcription == "failure":
                    raise SpeechError("transcription unavailable")
                if steering_transcription == "empty":
                    return SimpleNamespace(text="")
            return SimpleNamespace(text=f"words of {filename}")

    store = AttachmentStore(tmp_path)
    voices = [store.store(name, b"OggS\x00\x02voice-payload") for name in ("a.ogg", "b.ogg")]
    blocks = [MediaBlock("media", r.id, r.filename, r.media_type) for r in voices]
    adapter = PausedAdapter(
        [{"content": "Before", "tool_calls": None}, {"content": "After", "tool_calls": None}]
    )
    agent = StubAgent(id="coder", model="openai/gpt-5.2")
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.chat_sessions.create("coder", session_id="one")
    transcriber = Transcriber()
    loop = build_chat_loop(
        runtime, attachment_resolver=ContentBlockResolver(store, transcriber=transcriber)
    )
    run = await loop.start_run("coder", [blocks[0]], session_id="one")
    await asyncio.wait_for(adapter.entered.wait(), 5)
    item = await loop.queue_run("coder", [blocks[1]], session_id="one")
    runtime.chat_run_manager.steer_queued("coder", "one", item.item_id, project_id=None)
    adapter.release.set()
    await asyncio.wait_for(run.wait(), 10)

    session = runtime.chat_sessions.get(session_address("coder", "one"))
    users = [message for message in session.load() if message.role == "user"]
    assert [message.content for message in users] == [
        [replace(blocks[0], transcription="words of a.ogg")],
        [replace(blocks[1], transcription="words of b.ogg")]
        if steering_transcription == "success"
        else [blocks[1]],
    ]
    # Each voice message is transcribed once; requests reuse the persisted text.
    assert transcriber.calls == ["a.ogg", "b.ogg"]
    assert ("words of b.ogg" in str(adapter.requests[1]["messages"])) is (
        steering_transcription == "success"
    )


@pytest.mark.asyncio
async def test_stop_during_speech_to_text_keeps_the_voice_message(tmp_path: Path) -> None:
    # Stop abandons the speech-to-text of the Run's input: the voice message
    # still persists, without a transcription, and the Run ends before a request.
    class StalledTranscriber:
        def __init__(self) -> None:
            self.entered = asyncio.Event()

        async def transcribe(self, audio: bytes, *, filename: str, media_type: str) -> object:
            self.entered.set()
            await asyncio.Event().wait()
            raise AssertionError("speech-to-text never finishes")

    store = AttachmentStore(tmp_path)
    voice = store.store("a.ogg", b"OggS\x00\x02voice-payload")
    block = MediaBlock("media", voice.id, voice.filename, voice.media_type)
    adapter = StubAdapter([])
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=StubAgent(id="coder", model="openai/gpt-5.2"), adapter=adapter
    )
    runtime.chat_sessions.create("coder", session_id="one")
    transcriber = StalledTranscriber()
    loop = build_chat_loop(
        runtime, attachment_resolver=ContentBlockResolver(store, transcriber=transcriber)
    )
    run = await loop.start_run("coder", [block], session_id="one")
    await asyncio.wait_for(transcriber.entered.wait(), 5)
    run.request_cancel(reason="user")
    with pytest.raises(RunCancelledError):
        await asyncio.wait_for(run.wait(), 10)

    assert run.status == RunStatus.CANCELLED
    history = runtime.chat_sessions.get(session_address("coder", "one")).load()
    users = [message for message in history if message.role == "user"]
    assert [message.content for message in users] == [[block]]
    persisted = [
        event.payload["message"]["id"]
        for event in await runtime.timelines.events(run)
        if event.type == "user_message_persisted"
    ]
    assert persisted == [users[0].id]
    assert adapter.requests == []


@pytest.mark.asyncio
async def test_steering_reaches_an_internal_system_run(tmp_path: Path) -> None:
    adapter = PausedAdapter(
        [{"content": "Reviewed", "tool_calls": None}, {"content": "Adjusted", "tool_calls": None}]
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=StubAgent(id="coder", model="openai/gpt-5.2"), adapter=adapter
    )
    runtime.chat_sessions.create("coder", session_id="one")
    loop = build_chat_loop(runtime)
    # Shaped like the automatic delivery of a finished Sub-Agent's result.
    run = await loop.start_run(
        "coder",
        "Sub-Agent finished",
        session_id="one",
        internal=True,
        input_persisted_hook=lambda: None,
        run_kind=RunKind.SYSTEM,
    )
    await asyncio.wait_for(adapter.entered.wait(), 5)
    item = await loop.queue_run("coder", "Also check the tests", session_id="one")
    runtime.chat_run_manager.steer_queued("coder", "one", item.item_id, project_id=None)
    adapter.release.set()
    result = await asyncio.wait_for(run.wait(), 10)
    assert result.content == "Adjusted"
    assert item.future.result() is run
    history = runtime.chat_sessions.get(session_address("coder", "one")).load()
    assert [m.content for m in history if m.role == "user"] == ["Also check the tests"]
    assert len(_steering_notes(history)) == 1
    sent = adapter.requests[1]["messages"]
    assert [m["content"] for m in sent if m["role"] == "user"][-1] == "Also check the tests"


@pytest.mark.asyncio
async def test_input_selected_for_a_cancelled_run_starts_next(tmp_path: Path) -> None:
    adapter = PausedAdapter([{"content": "Queued answer", "tool_calls": None}])
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=StubAgent(id="coder", model="openai/gpt-5.2"), adapter=adapter
    )
    runtime.chat_sessions.create("coder", session_id="one")
    loop = build_chat_loop(runtime)
    run = await loop.start_run("coder", "Original", session_id="one")
    await asyncio.wait_for(adapter.entered.wait(), 5)
    item = await loop.queue_run("coder", "Retained", session_id="one")
    manager = runtime.chat_run_manager
    manager.steer_queued("coder", "one", item.item_id, project_id=None)
    await manager.cancel(run.id)
    adapter.release.set()
    successor = await asyncio.wait_for(item.future, 5)
    assert successor.id != run.id
    await asyncio.wait_for(successor.wait(), 10)
    history = runtime.chat_sessions.get(session_address("coder", "one")).load()
    assert [m.content for m in history if m.role == "user"] == ["Original", "Retained"]
    assert not _steering_notes(history)


@pytest.mark.asyncio
async def test_each_steered_step_gets_a_fresh_recovery_budget(
    tmp_path: Path, recovery_waits: list[float]
) -> None:
    failures: list[Any] = [NetworkError("temporarily unavailable") for _ in range(8)]
    answers = [{"content": f"Answer {index}", "tool_calls": None} for index in range(10)]
    # The first answer needs the ninth attempt; nine steered answers follow it.
    adapter = SteeringAdapter([*failures, *answers], steer_requests=range(8, 17))
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=StubAgent(id="coder", model="openai/gpt-5.2"), adapter=adapter
    )
    runtime.chat_sessions.create("coder", session_id="one")
    loop = build_chat_loop(runtime)
    adapter.loop, adapter.runtime = loop, runtime
    run = await loop.start_run("coder", "Original", session_id="one")
    result = await asyncio.wait_for(run.wait(), 20)
    assert run.status == RunStatus.COMPLETED
    assert result.content == "Answer 9"
    assert len(adapter.requests) == 18
    retries = [
        event.payload["attempt"]
        for event in await runtime.timelines.events(run)
        if event.type == PROVIDER_REQUEST_STATUS_EVENT
        and event.payload.get("state") == "retrying"
        and "attempt" in event.payload
    ]
    # Only the initial step failed; no steered request waits for a backoff.
    assert retries == list(range(2, 10))
    assert len(recovery_waits) == 8
    history = runtime.chat_sessions.get(session_address("coder", "one")).load()
    assert len([m for m in history if m.role == "user"]) == 10


@pytest.mark.asyncio
async def test_withdrawn_steering_input_keeps_the_final_answer(tmp_path: Path) -> None:
    address = session_address("coder", "one")

    class WithdrawingAdapter(StubAdapter):
        @override
        async def send(
            self, messages: list[dict[str, Any]], *, model_id: str, **kwargs: Any
        ) -> dict[str, Any]:
            if not self.requests:
                item = await loop.queue_run("coder", "Withdrawn", session_id="one")
                manager.steer_queued("coder", "one", item.item_id, project_id=None)
                withdrawals.append(asyncio.create_task(withdraw(item.item_id)))
            return await super().send(messages, model_id=model_id, **kwargs)

    async def withdraw(item_id: str) -> None:
        # Wait on the Session lock while the final answer is persisted, so the
        # removal lands between the pending-steering check and its delivery.
        lock = runtime.chat_sessions.write_lock(address)
        while not lock.locked():
            await asyncio.sleep(0)
        async with lock:
            assert manager.remove_queued("coder", "one", item_id, project_id=None)

    withdrawals: list[asyncio.Task[None]] = []
    adapter = WithdrawingAdapter([{"content": "Final", "tool_calls": None}])
    runtime = compaction_runtime(tmp_path, adapter=adapter, context_window=1_000_000)
    runtime.chat_sessions.create("coder", session_id="one")
    manager = runtime.chat_run_manager
    service = StubCompactionService(should_auto=False)
    loop = build_chat_loop(runtime, compaction_service=cast(Any, service))
    run = await loop.start_run("coder", "Original", session_id="one")
    result = await asyncio.wait_for(run.wait(), 10)
    await asyncio.gather(*withdrawals)
    assert run.status == RunStatus.COMPLETED
    assert result.content == "Final"
    assert len(adapter.requests) == 1
    # Only the first request evaluates Compaction. The withdrawn input needs
    # neither another Agent request nor an automatic Compaction attempt.
    assert len(service.should_auto_calls) == 1
    history = runtime.chat_sessions.get(address).load()
    assert [m.content for m in history if m.role == "user"] == ["Original"]
    assert not _steering_notes(history)


@pytest.mark.asyncio
@pytest.mark.parametrize("steer", [False, True])
async def test_steering_keeps_current_run_reasoning(tmp_path: Path, steer: bool) -> None:
    first = {
        "content": "Before",
        "reasoning": "Plan the probe",
        "reasoning_meta": {"signature": "sig-1"},
        "tool_calls": [{"id": "a", "name": "probe", "arguments": {}}],
        "terminal_outcome": "tool_calls",
    }
    adapter = PausedAdapter([first, {"content": "After", "tool_calls": None}])
    tools = ToolRegistry()
    tools.register(
        "probe", "Probe", {"type": "object"}, lambda _ctx, _args: tool_success({"done": True})
    )
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["probe"])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)
    runtime.chat_sessions.create("coder", session_id="one")
    loop = build_chat_loop(runtime)
    run = await loop.start_run("coder", "Original", session_id="one")
    await asyncio.wait_for(adapter.entered.wait(), 5)
    if steer:
        item = await loop.queue_run("coder", "Steer", session_id="one")
        runtime.chat_run_manager.steer_queued("coder", "one", item.item_id, project_id=None)
    adapter.release.set()
    await asyncio.wait_for(run.wait(), 10)
    sent = adapter.requests[1]["messages"]
    tool_turn = next(m for m in sent if m["role"] == "assistant" and m.get("tool_calls"))
    assert tool_turn["reasoning"] == "Plan the probe"
    assert tool_turn["reasoning_meta"] == {"signature": "sig-1"}
    assert [m["content"] for m in sent if m["role"] == "user"][-1] == (
        "Steer" if steer else "Original"
    )
