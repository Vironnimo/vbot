"""Channel command and new-session lifecycle tests."""

from __future__ import annotations

from .engine_test_support import (
    CHANNEL_REPLY_SURFACE,
    SESSION_ID,
    AsyncMock,
    ChatRunManager,
    CommandAction,
    CommandHandled,
    Mock,
    Path,
    ReplyPlanFacts,
    Run,
    asyncio,
    drain,
    engine_module,
    logging,
    make_command_dispatcher,
    make_completed_run,
    make_conversation,
    make_engine,
    make_new_only_dispatcher,
    pytest,
)


@pytest.mark.asyncio
async def test_handled_command_replies_before_trigger(tmp_path: Path) -> None:
    command_dispatcher = make_command_dispatcher(result=CommandHandled(reply="Run cancelled."))
    engine, _sessions, trigger_mock, transport = make_engine(
        tmp_path, command_dispatcher=command_dispatcher
    )

    await engine.handle_inbound_text(make_conversation(), "/stop")
    await drain(engine, 12345)

    command_dispatcher.dispatch.assert_called_once_with("assistant", SESSION_ID, "/stop")
    trigger_mock.assert_not_awaited()
    assert transport.sent == [("12345", "Run cancelled.")]
    await engine.stop()


@pytest.mark.asyncio
async def test_compact_command_action_replies_in_worker(tmp_path: Path) -> None:
    compact_mock = AsyncMock(return_value="Context compacted.")
    command_dispatcher = make_command_dispatcher(result=CommandAction(name="compact"))
    engine, _sessions, trigger_mock, transport = make_engine(
        tmp_path, compact_session=compact_mock, command_dispatcher=command_dispatcher
    )

    await engine.handle_inbound_text(make_conversation(), "/compact")
    await drain(engine, 12345)

    compact_mock.assert_awaited_once_with("assistant", SESSION_ID, None)
    trigger_mock.assert_not_awaited()
    assert transport.sent == [("12345", "Context compacted.")]
    await engine.stop()


@pytest.mark.asyncio
async def test_compact_command_action_forwards_instruction(tmp_path: Path) -> None:
    compact_mock = AsyncMock(return_value="Context compacted.")
    command_dispatcher = make_command_dispatcher(
        result=CommandAction(name="compact", argument="keep the API design")
    )
    engine, _sessions, _trigger, transport = make_engine(
        tmp_path, compact_session=compact_mock, command_dispatcher=command_dispatcher
    )

    await engine.handle_inbound_text(make_conversation(), "/compact keep the API design")
    await drain(engine, 12345)

    compact_mock.assert_awaited_once_with("assistant", SESSION_ID, "keep the API design")
    await engine.stop()


@pytest.mark.asyncio
async def test_new_session_command_starts_fresh_session_and_redirects_followups(
    tmp_path: Path,
) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, chat_sessions, _trigger, transport = make_engine(
        tmp_path, trigger_run=trigger_mock, command_dispatcher=make_new_only_dispatcher()
    )

    await engine.handle_inbound_text(make_conversation(), "/new")
    await drain(engine, 12345)

    new_session_id = chat_sessions.get_metadata("assistant", SESSION_ID)[
        engine_module.ACTIVE_SESSION_METADATA_KEY
    ]
    # A distinct session, anchored to the conversation for grouping, was created.
    assert new_session_id != SESSION_ID
    assert new_session_id.startswith(f"{SESSION_ID}-")
    assert chat_sessions.exists("assistant", new_session_id)
    # The previous (anchor) session is left intact and still loadable, but /new
    # does not invent a model-facing note for either session.
    assert chat_sessions.get("assistant", SESSION_ID).load() == []
    # /new confirms without triggering a run.
    assert transport.sent_texts == [engine_module._NEW_SESSION_STARTED_REPLY]
    trigger_mock.assert_not_awaited()

    # A later message follows the pointer into the new session.
    await engine.handle_inbound_text(make_conversation(), "after new")
    await drain(engine, 12345)

    trigger_mock.assert_awaited_once()
    assert trigger_mock.await_args is not None
    assert trigger_mock.await_args.args[2] == new_session_id
    await engine.stop()


@pytest.mark.asyncio
async def test_message_enqueued_behind_pending_new_routes_to_new_session(tmp_path: Path) -> None:
    """A message arriving before a queued /new is processed follows the moved pointer.

    Routing is resolved at processing time, not enqueue time: both items sit in the
    conversation queue together, /new moves the pointer first, and the message that
    was already enqueued behind it must land in the new session, not the old one.
    """
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, chat_sessions, _trigger, _transport = make_engine(
        tmp_path, trigger_run=trigger_mock, command_dispatcher=make_new_only_dispatcher()
    )

    await engine.handle_inbound_text(make_conversation(), "/new")
    await engine.handle_inbound_text(make_conversation(), "right after new")
    await drain(engine, 12345)

    new_session_id = chat_sessions.get_metadata("assistant", SESSION_ID)[
        engine_module.ACTIVE_SESSION_METADATA_KEY
    ]
    trigger_mock.assert_awaited_once()
    assert trigger_mock.await_args is not None
    assert trigger_mock.await_args.args[2] == new_session_id
    await engine.stop()


@pytest.mark.asyncio
async def test_new_session_tags_fresh_session_with_metadata_but_no_reminder(tmp_path: Path) -> None:
    command_dispatcher = make_command_dispatcher(result=CommandAction(name="new_session"))
    engine, chat_sessions, _trigger, _transport = make_engine(
        tmp_path, command_dispatcher=command_dispatcher
    )

    await engine.handle_inbound_text(make_conversation(), "/new")
    await drain(engine, 12345)

    new_session_id = chat_sessions.get_metadata("assistant", SESSION_ID)[
        engine_module.ACTIVE_SESSION_METADATA_KEY
    ]
    notes = [
        message
        for message in chat_sessions.get("assistant", new_session_id).load()
        if message.role == "note"
    ]
    assert notes == []
    metadata = chat_sessions.get_metadata("assistant", new_session_id)
    assert metadata["source_channel_id"] == "tg-assistant"
    assert metadata["platform"] == "telegram"
    assert metadata["platform_conv_id"] == "12345"
    assert metadata["last_reply_target"] == {
        "channel_id": "tg-assistant",
        "platform_target": "12345",
    }
    # The fresh session is not itself a pointer anchor, and tracks no participant.
    assert engine_module.ACTIVE_SESSION_METADATA_KEY not in metadata
    assert "participants" not in metadata
    await engine.stop()


@pytest.mark.asyncio
async def test_new_session_command_refused_while_run_active(tmp_path: Path) -> None:
    command_dispatcher = make_command_dispatcher(result=CommandAction(name="new_session"))
    engine, chat_sessions, trigger_mock, transport = make_engine(
        tmp_path,
        command_dispatcher=command_dispatcher,
        has_active_run=Mock(return_value=True),
    )

    await engine.handle_inbound_text(make_conversation(), "/new")
    await drain(engine, 12345)

    assert transport.sent_texts == [engine_module._NEW_SESSION_BUSY_REPLY]
    trigger_mock.assert_not_awaited()
    # No new session and no pointer: the anchor is unchanged.
    metadata = chat_sessions.get_metadata("assistant", SESSION_ID)
    assert engine_module.ACTIVE_SESSION_METADATA_KEY not in metadata
    await engine.stop()


@pytest.mark.asyncio
async def test_new_session_in_one_chat_leaves_other_chat_untouched(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, chat_sessions, _trigger, _transport = make_engine(
        tmp_path, trigger_run=trigger_mock, command_dispatcher=make_new_only_dispatcher()
    )

    await engine.handle_inbound_text(make_conversation(chat_id=12345), "/new")
    await drain(engine, 12345)

    await engine.handle_inbound_text(make_conversation(chat_id=67890), "hello B")
    await drain(engine, 67890)

    # Chat B is unaffected: it routes to its own derived anchor and has no pointer.
    trigger_mock.assert_awaited_once_with(
        "assistant",
        "hello B",
        "ch-tg-assistant-67890",
        sender=None,
        reply_surface=CHANNEL_REPLY_SURFACE,
    )
    metadata_b = chat_sessions.get_metadata("assistant", "ch-tg-assistant-67890")
    assert engine_module.ACTIVE_SESSION_METADATA_KEY not in metadata_b
    await engine.stop()


@pytest.mark.asyncio
async def test_continue_command_action_relays_continued_run(tmp_path: Path) -> None:
    continue_mock = AsyncMock(return_value=make_completed_run(output_text="continued reply"))
    command_dispatcher = make_command_dispatcher(result=CommandAction(name="continue"))
    engine, _sessions, trigger_mock, transport = make_engine(
        tmp_path, continue_run=continue_mock, command_dispatcher=command_dispatcher
    )

    await engine.handle_inbound_text(make_conversation(), "/continue")
    await drain(engine, 12345)

    continue_mock.assert_awaited_once_with(
        "assistant", SESSION_ID, reply_surface=CHANNEL_REPLY_SURFACE
    )
    trigger_mock.assert_not_awaited()
    assert transport.sent == [("12345", "continued reply")]
    await engine.stop()


@pytest.mark.asyncio
async def test_unsupported_command_action_reports_channel_limitation(tmp_path: Path) -> None:
    command_dispatcher = make_command_dispatcher(
        result=CommandAction(name="handoff", argument=None)
    )
    engine, _sessions, trigger_mock, transport = make_engine(
        tmp_path, command_dispatcher=command_dispatcher
    )

    await engine.handle_inbound_text(make_conversation(), "/handoff")
    await drain(engine, 12345)

    trigger_mock.assert_not_awaited()
    assert transport.sent == [
        ("12345", "This command is not available from Telegram channels yet.")
    ]
    await engine.stop()


@pytest.mark.asyncio
async def test_compact_action_failure_is_logged_and_replies_generically(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    compact_mock = AsyncMock(side_effect=RuntimeError("compact failed"))
    command_dispatcher = make_command_dispatcher(result=CommandAction(name="compact"))
    engine, _sessions, _trigger, transport = make_engine(
        tmp_path, compact_session=compact_mock, command_dispatcher=command_dispatcher
    )
    caplog.set_level(logging.ERROR, logger="vbot.channels.engine")

    await engine.handle_inbound_text(make_conversation(), "/compact")
    await drain(engine, 12345)

    assert transport.sent_texts == [engine_module._FAILED_REPLY]
    records = [r for r in caplog.records if r.message.startswith("Channel command action failed")]
    assert len(records) == 1
    assert "action=compact" in records[0].message
    assert records[0].exc_info is not None
    await engine.stop()


@pytest.mark.asyncio
async def test_stop_command_eagerly_dispatched_while_worker_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command_dispatcher = make_command_dispatcher(result=CommandHandled(reply="Run cancelled."))
    trigger_mock = AsyncMock(
        return_value=Run(run_id="run-active", agent_id="assistant", session_id=SESSION_ID)
    )
    engine, _sessions, _trigger, transport = make_engine(
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

    await engine.handle_inbound_text(make_conversation(), "/stop")
    await asyncio.sleep(0)

    # Plain text never touches the dispatcher; the command dispatched eagerly while
    # the worker was still blocked relaying the first message's run.
    command_dispatcher.dispatch.assert_called_once_with("assistant", SESSION_ID, "/stop")
    assert trigger_mock.await_count == 1
    assert transport.sent == [("12345", "Run cancelled.")]

    release_relay.set()
    await drain(engine, 12345)
    await engine.stop()


@pytest.mark.asyncio
async def test_stop_command_bypasses_full_waiting_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waiting_work_manager = ChatRunManager(waiting_work_limit=1)
    command_dispatcher = make_command_dispatcher(result=CommandHandled(reply="Run cancelled."))
    trigger_mock = AsyncMock(
        return_value=Run(run_id="run-active", agent_id="assistant", session_id=SESSION_ID)
    )
    engine, _sessions, _trigger, transport = make_engine(
        tmp_path,
        trigger_run=trigger_mock,
        command_dispatcher=command_dispatcher,
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
    await engine.handle_inbound_text(make_conversation(), "queued")
    await engine.handle_inbound_text(make_conversation(), "/stop")

    command_dispatcher.dispatch.assert_called_once_with("assistant", SESSION_ID, "/stop")
    assert transport.sent_texts == ["Run cancelled."]
    release_relay.set()
    await drain(engine, 12345)
    await engine.stop()
