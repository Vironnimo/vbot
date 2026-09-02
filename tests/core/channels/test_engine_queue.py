"""Channel ingress queue, overflow, and ordering tests."""

from __future__ import annotations

from core.sessions import SessionAddress

from .engine_test_support import (
    SESSION_ID,
    AsyncMock,
    ChatRunManager,
    FakeTransport,
    Path,
    ReplyPlanFacts,
    Run,
    SimpleNamespace,
    asyncio,
    drain,
    engine_module,
    make_command_dispatcher,
    make_conversation,
    make_engine,
    pytest,
)

_ASYNC_COORDINATION_TIMEOUT_SECONDS = 10.0


@pytest.mark.asyncio
async def test_non_command_text_queues_behind_blocked_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command_dispatcher = make_command_dispatcher()
    trigger_mock = AsyncMock(
        return_value=Run(run_id="run-active", agent_id="assistant", session_id=SESSION_ID)
    )
    engine, _sessions, _trigger, _transport = make_engine(
        tmp_path, trigger_run=trigger_mock, command_dispatcher=command_dispatcher
    )

    relay_started = asyncio.Event()
    release_relay = asyncio.Event()

    async def block_relay(_run: Run, _platform_target: str) -> None:
        relay_started.set()
        await release_relay.wait()

    monkeypatch.setattr(engine, "_relay_run_events", AsyncMock(side_effect=block_relay))

    await engine.handle_inbound_text(make_conversation(), "hello")
    await asyncio.wait_for(relay_started.wait(), timeout=1)

    await engine.handle_inbound_text(make_conversation(), "still queued")
    await asyncio.sleep(0)

    assert trigger_mock.await_count == 1
    queue = engine._chat_queues.get("12345")
    assert queue is not None
    assert queue.qsize() == 1

    release_relay.set()
    await drain(engine, 12345)
    await engine.stop()


@pytest.mark.asyncio
async def test_chat_waiting_limit_rejects_ninth_followup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    waiting_work_manager = ChatRunManager(waiting_work_limit=16)
    trigger_mock = AsyncMock(
        return_value=Run(run_id="run-active", agent_id="assistant", session_id=SESSION_ID)
    )
    engine, _sessions, _trigger, transport = make_engine(
        tmp_path,
        trigger_run=trigger_mock,
        waiting_work_manager=waiting_work_manager,
    )
    relay_started = asyncio.Event()
    release_relay = asyncio.Event()
    first_relay = True

    async def block_first_relay(_run: Run, _reply_plan: ReplyPlanFacts) -> None:
        nonlocal first_relay
        if first_relay:
            first_relay = False
            relay_started.set()
            await release_relay.wait()

    monkeypatch.setattr(engine, "_relay_run_events", block_first_relay)

    await engine.handle_inbound_text(make_conversation(), "running")
    await asyncio.wait_for(relay_started.wait(), timeout=1)
    for index in range(engine_module.CHANNEL_WAITING_WORK_LIMIT):
        await engine.handle_inbound_text(make_conversation(), f"queued {index}")

    assert waiting_work_manager.waiting_work_count() == engine_module.CHANNEL_WAITING_WORK_LIMIT
    await engine.handle_inbound_text(make_conversation(), "overflow")

    queue = engine._chat_queues["12345"]
    assert queue.qsize() == engine_module.CHANNEL_WAITING_WORK_LIMIT
    assert transport.sent_texts == [engine_module._BUSY_REPLY]
    release_relay.set()
    await drain(engine, 12345)
    await engine.stop()


@pytest.mark.asyncio
async def test_global_waiting_limit_rejects_followup_from_another_chat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waiting_work_manager = ChatRunManager(waiting_work_limit=2)
    trigger_mock = AsyncMock(
        return_value=Run(run_id="run-active", agent_id="assistant", session_id=SESSION_ID)
    )
    engine, _sessions, _trigger, transport = make_engine(
        tmp_path,
        trigger_run=trigger_mock,
        waiting_work_manager=waiting_work_manager,
    )
    first_relays_started = {"12345": asyncio.Event(), "67890": asyncio.Event()}
    release_relays = asyncio.Event()

    async def block_first_relay(_run: Run, reply_plan: ReplyPlanFacts) -> None:
        started = first_relays_started[reply_plan.platform_target]
        if not started.is_set():
            started.set()
            await release_relays.wait()

    monkeypatch.setattr(engine, "_relay_run_events", block_first_relay)

    await engine.handle_inbound_text(make_conversation(chat_id=12345), "running one")
    await asyncio.wait_for(first_relays_started["12345"].wait(), timeout=1)
    await engine.handle_inbound_text(make_conversation(chat_id=12345), "queued one")
    await engine.handle_inbound_text(make_conversation(chat_id=67890), "running two")
    await asyncio.wait_for(first_relays_started["67890"].wait(), timeout=1)
    await engine.handle_inbound_text(make_conversation(chat_id=67890), "queued two")

    assert waiting_work_manager.waiting_work_count() == 2
    await engine.handle_inbound_text(make_conversation(chat_id=12345), "global overflow")

    assert transport.sent_texts == [engine_module._BUSY_REPLY]
    assert engine._chat_queues["12345"].qsize() == 1
    release_relays.set()
    await drain(engine, 12345)
    await drain(engine, 67890)
    await engine.stop()


@pytest.mark.asyncio
async def test_channel_fifo_preserves_arrival_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trigger_mock = AsyncMock(
        return_value=Run(run_id="run-active", agent_id="assistant", session_id=SESSION_ID)
    )
    engine, _sessions, _trigger, _transport = make_engine(tmp_path, trigger_run=trigger_mock)
    relay_started = asyncio.Event()
    release_relay = asyncio.Event()
    first_relay = True

    async def block_first_relay(_run: Run, _reply_plan: ReplyPlanFacts) -> None:
        nonlocal first_relay
        if first_relay:
            first_relay = False
            relay_started.set()
            await release_relay.wait()

    monkeypatch.setattr(engine, "_relay_run_events", block_first_relay)

    for text in ("first", "second", "third"):
        await engine.handle_inbound_text(make_conversation(), text)
        if text == "first":
            await asyncio.wait_for(
                relay_started.wait(), timeout=_ASYNC_COORDINATION_TIMEOUT_SECONDS
            )

    release_relay.set()
    await drain(engine, 12345)

    assert [call.args[1] for call in trigger_mock.await_args_list] == ["first", "second", "third"]
    await engine.stop()


@pytest.mark.asyncio
async def test_overflow_busy_reply_is_throttled_per_chat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waiting_work_manager = ChatRunManager(waiting_work_limit=16)
    trigger_mock = AsyncMock(
        return_value=Run(run_id="run-active", agent_id="assistant", session_id=SESSION_ID)
    )
    engine, _sessions, _trigger, transport = make_engine(
        tmp_path,
        trigger_run=trigger_mock,
        waiting_work_manager=waiting_work_manager,
    )
    relay_started = asyncio.Event()
    release_relay = asyncio.Event()

    async def block_relay(_run: Run, _reply_plan: ReplyPlanFacts) -> None:
        relay_started.set()
        await release_relay.wait()

    monkeypatch.setattr(engine, "_relay_run_events", block_relay)

    await engine.handle_inbound_text(make_conversation(), "running")
    await asyncio.wait_for(relay_started.wait(), timeout=1)
    for index in range(engine_module.CHANNEL_WAITING_WORK_LIMIT):
        await engine.handle_inbound_text(make_conversation(), f"queued {index}")
    await engine.handle_inbound_text(make_conversation(), "overflow one")
    await engine.handle_inbound_text(make_conversation(), "overflow two")

    assert transport.sent_texts == [engine_module._BUSY_REPLY]
    release_relay.set()
    await drain(engine, 12345)
    await engine.stop()


@pytest.mark.asyncio
async def test_overflow_rejects_media_before_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waiting_work_manager = ChatRunManager(waiting_work_limit=16)
    media_builder = AsyncMock(return_value=[])
    transport = FakeTransport(media_builder=media_builder)
    trigger_mock = AsyncMock(
        return_value=Run(run_id="run-active", agent_id="assistant", session_id=SESSION_ID)
    )
    engine, _sessions, _trigger, _transport = make_engine(
        tmp_path,
        trigger_run=trigger_mock,
        transport=transport,
        waiting_work_manager=waiting_work_manager,
    )
    relay_started = asyncio.Event()
    release_relay = asyncio.Event()

    async def block_relay(_run: Run, _reply_plan: ReplyPlanFacts) -> None:
        relay_started.set()
        await release_relay.wait()

    monkeypatch.setattr(engine, "_relay_run_events", block_relay)

    await engine.handle_inbound_text(make_conversation(), "running")
    await asyncio.wait_for(relay_started.wait(), timeout=1)
    for index in range(engine_module.CHANNEL_WAITING_WORK_LIMIT):
        await engine.handle_inbound_text(make_conversation(), f"queued {index}")
    await engine.handle_inbound_media(make_conversation(), (SimpleNamespace(caption="photo"),))

    media_builder.assert_not_awaited()
    assert transport.sent_texts == [engine_module._BUSY_REPLY]
    release_relay.set()
    await drain(engine, 12345)
    await engine.stop()


@pytest.mark.asyncio
async def test_observed_message_waits_behind_active_channel_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trigger_mock = AsyncMock(
        return_value=Run(run_id="run-active", agent_id="assistant", session_id=SESSION_ID)
    )
    engine, chat_sessions, _trigger, _transport = make_engine(
        tmp_path,
        trigger_run=trigger_mock,
        observe_unaddressed=True,
    )
    relay_started = asyncio.Event()
    release_relay = asyncio.Event()

    async def block_relay(_run: Run, _reply_plan: ReplyPlanFacts) -> None:
        relay_started.set()
        await release_relay.wait()

    monkeypatch.setattr(engine, "_relay_run_events", AsyncMock(side_effect=block_relay))

    await engine.handle_inbound_text(
        make_conversation(kind="group", mentioned_bot=True),
        "hello bot",
    )
    await asyncio.wait_for(relay_started.wait(), timeout=1)

    await engine.handle_inbound_text(
        make_conversation(kind="group", user_display_name="Alice"),
        "side conversation",
    )
    await asyncio.sleep(0)

    notes_before_release = [
        message.content
        for message in chat_sessions.get(
            SessionAddress(project_id=None, agent_id="assistant", session_id=SESSION_ID)
        ).load()
        if message.role == "note"
    ]
    assert not any(
        isinstance(content, str) and content.startswith("[channel-message] ")
        for content in notes_before_release
    )
    queue = engine._chat_queues.get("12345")
    assert queue is not None
    assert queue.qsize() == 1

    release_relay.set()
    await drain(engine, 12345)

    notes_after_release = [
        message.content
        for message in chat_sessions.get(
            SessionAddress(project_id=None, agent_id="assistant", session_id=SESSION_ID)
        ).load()
        if message.role == "note"
    ]
    assert notes_after_release[-1] == "[channel-message] [Alice|50|member]: side conversation"
    await engine.stop()
