"""Channel reply relay and interaction-tap tests."""

from __future__ import annotations

from datetime import UTC, datetime

from core.channels.adapter import RunButtonBinding, bound_run_callback_data
from core.channels.channels import ChannelStorage

from .engine_test_support import (
    CHANNEL_REPLY_SURFACE,
    SESSION_ID,
    AsyncMock,
    ChatRunManager,
    InteractionButton,
    InteractionEvent,
    Path,
    drain,
    engine_module,
    logging,
    make_cancelled_run,
    make_completed_run,
    make_conversation,
    make_empty_completed_run,
    make_engine,
    make_failed_run,
    make_new_only_dispatcher,
    pytest,
)


def _interaction_event(
    *, data: str = "run:done", user_display_name: str | None = "Alice"
) -> InteractionEvent:
    return InteractionEvent(
        platform="telegram",
        channel_id="tg-assistant",
        chat_id="12345",
        user_id="50",
        message_id="777",
        data=data,
        buttons=(
            (InteractionButton(label="✅ Milk", data="chk:milk"),),
            (InteractionButton(label="⬜ Bread", data="chk:bread"),),
            (InteractionButton(label="Fertig ✅", data="run:done"),),
        ),
        user_display_name=user_display_name,
    )


@pytest.mark.asyncio
async def test_completed_run_forwards_final_assistant_output(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="final reply"))
    engine, _sessions, _trigger, transport = make_engine(tmp_path, trigger_run=trigger_mock)

    await engine.handle_inbound_text(make_conversation(), "hello")
    await drain(engine, 12345)

    assert transport.sent == [("12345", "final reply")]
    assert transport.activity_targets == ["12345"]
    await engine.stop()


@pytest.mark.asyncio
async def test_completed_run_without_output_sends_empty_reply(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_empty_completed_run())
    engine, _sessions, _trigger, transport = make_engine(tmp_path, trigger_run=trigger_mock)

    await engine.handle_inbound_text(make_conversation(), "hello")
    await drain(engine, 12345)

    assert transport.sent_texts == [engine_module._EMPTY_ASSISTANT_REPLY]
    await engine.stop()


@pytest.mark.asyncio
async def test_failed_run_sends_generic_failure_reply(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_failed_run(message="boom"))
    engine, _sessions, _trigger, transport = make_engine(tmp_path, trigger_run=trigger_mock)

    await engine.handle_inbound_text(make_conversation(), "hello")
    await drain(engine, 12345)

    assert transport.sent_texts == [engine_module._FAILED_REPLY]
    assert "boom" not in transport.sent_texts[0]
    await engine.stop()


@pytest.mark.asyncio
async def test_cancelled_run_sends_cancellation_reply(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_cancelled_run())
    engine, _sessions, _trigger, transport = make_engine(tmp_path, trigger_run=trigger_mock)

    await engine.handle_inbound_text(make_conversation(), "hello")
    await drain(engine, 12345)

    assert transport.sent_texts == [engine_module._CANCELLED_REPLY]
    await engine.stop()


@pytest.mark.asyncio
async def test_trigger_exception_sends_failure_without_leaking_internals(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    trigger_mock = AsyncMock(side_effect=RuntimeError("internal stack trace"))
    engine, _sessions, _trigger, transport = make_engine(tmp_path, trigger_run=trigger_mock)
    caplog.set_level(logging.ERROR, logger="vbot.channels.engine")

    await engine.handle_inbound_text(make_conversation(), "hello")
    await drain(engine, 12345)

    assert transport.sent_texts == [engine_module._FAILED_REPLY]
    records = [r for r in caplog.records if r.message.startswith("Channel trigger run failed")]
    assert len(records) == 1
    assert records[0].exc_info is not None
    assert "internal stack trace" not in transport.sent_texts[0]
    await engine.stop()


@pytest.mark.asyncio
async def test_group_reply_references_triggering_message(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, _sessions, _trigger, transport = make_engine(
        tmp_path, trigger_run=trigger_mock, response_mode="all"
    )

    await engine.handle_inbound_text(
        make_conversation(kind="group", message_id="777"),
        "hello",
    )
    await drain(engine, 12345)

    assert transport.sent == [("12345", "ok")]
    assert transport.sent_reply_targets == ["777"]
    await engine.stop()


@pytest.mark.asyncio
async def test_topic_message_reply_carries_thread_everywhere(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, chat_sessions, _trigger, transport = make_engine(
        tmp_path, trigger_run=trigger_mock, response_mode="all"
    )

    await engine.handle_inbound_text(
        make_conversation(kind="group", message_id="777", thread_id="42"),
        "hello",
    )
    await drain(engine, 12345)

    # Reply text, activity indicator, and the reply-target metadata all carry the topic.
    assert transport.sent == [("12345", "ok")]
    assert transport.sent_thread_ids == ["42"]
    assert transport.activity_thread_ids == ["42"]
    metadata = chat_sessions.get_metadata("assistant", SESSION_ID)
    assert metadata["last_reply_target"] == {
        "channel_id": "tg-assistant",
        "platform_target": "12345",
        "thread_id": "42",
    }

    # A later non-topic message rewrites the reply target without the thread key.
    await engine.handle_inbound_text(make_conversation(kind="group", mentioned_bot=True), "hi")
    await drain(engine, 12345)
    metadata = chat_sessions.get_metadata("assistant", SESSION_ID)
    assert "thread_id" not in metadata["last_reply_target"]
    await engine.stop()


@pytest.mark.asyncio
async def test_direct_reply_does_not_reference_message(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, _sessions, _trigger, transport = make_engine(tmp_path, trigger_run=trigger_mock)

    await engine.handle_inbound_text(
        make_conversation(kind="direct", message_id="777"),
        "hello",
    )
    await drain(engine, 12345)

    assert transport.sent == [("12345", "ok")]
    assert transport.sent_reply_targets == [None]
    await engine.stop()


def test_format_interaction_note_lists_tapped_button_and_full_keyboard() -> None:
    note = engine_module._format_interaction_note(
        make_conversation(kind="group", user_id=50, user_display_name="Alice"),
        _interaction_event(),
    )

    assert 'Tapped button: "Fertig ✅" (run:done)' in note
    assert '- "✅ Milk" (chk:milk)' in note
    assert '- "⬜ Bread" (chk:bread)' in note
    assert '- "Fertig ✅" (run:done)' in note
    # Group taps name the tapper so the agent knows who acted on the shared session.
    assert "Tapped by: [Alice|50|member]" in note


def test_format_interaction_note_omits_tapper_in_dm() -> None:
    note = engine_module._format_interaction_note(
        make_conversation(kind="direct", user_id=50, user_display_name="Alice"),
        _interaction_event(),
    )

    assert "Tapped by:" not in note


@pytest.mark.asyncio
async def test_interaction_tap_enqueues_internal_run_with_state(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="synced"))
    engine, _sessions, _trigger, _transport = make_engine(tmp_path, trigger_run=trigger_mock)

    outcome = await engine.trigger_interaction_reply(
        make_conversation(kind="direct", user_id=50), _interaction_event()
    )
    await drain(engine, 12345)

    assert outcome == "enqueued"
    trigger_mock.assert_awaited_once()
    await_args = trigger_mock.await_args
    assert await_args is not None
    # An internal note-driven run: no visible user message, content is the note.
    assert await_args.kwargs.get("internal") is True
    assert await_args.kwargs.get("reply_surface") == CHANNEL_REPLY_SURFACE
    note = await_args.args[1]
    assert "chk:milk" in note and "chk:bread" in note
    await engine.stop()


@pytest.mark.asyncio
async def test_group_owner_interaction_tap_enqueues(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="synced"))
    engine, _sessions, _trigger, _transport = make_engine(
        tmp_path, admin_user_ids=["50"], trigger_run=trigger_mock
    )

    outcome = await engine.trigger_interaction_reply(
        make_conversation(kind="group", user_id=50, message_id="777"), _interaction_event()
    )
    await drain(engine, 12345)

    assert outcome == "enqueued"
    trigger_mock.assert_awaited_once()
    await engine.stop()


@pytest.mark.asyncio
async def test_group_member_interaction_tap_is_dropped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="synced"))
    engine, _sessions, _trigger, _transport = make_engine(
        tmp_path, admin_user_ids=["50"], trigger_run=trigger_mock
    )

    caplog.set_level(logging.INFO, logger="vbot.channels.engine")
    outcome = await engine.trigger_interaction_reply(
        make_conversation(kind="group", user_id=99, message_id="777"), _interaction_event()
    )
    await drain(engine, 12345)

    assert outcome == "denied"
    trigger_mock.assert_not_awaited()
    assert any("denied for member" in record.getMessage() for record in caplog.records)
    await engine.stop()


@pytest.mark.asyncio
async def test_bound_tap_repoints_conversation_and_orders_followup_in_origin_session(
    tmp_path: Path,
) -> None:
    storage = ChannelStorage(tmp_path)
    trigger_mock = AsyncMock(
        side_effect=[
            make_completed_run(output_text="synced", session_id="origin-session"),
            make_completed_run(output_text="deleted", session_id="origin-session"),
        ]
    )
    engine, sessions, _trigger, transport = make_engine(
        tmp_path,
        trigger_run=trigger_mock,
        run_button_binding_registry=storage,
    )
    sessions.create("assistant", session_id="origin-session")
    binding = RunButtonBinding(
        id="binding-1",
        platform_target="12345",
        thread_id=None,
        origin_session_id="origin-session",
        original_button_data=("run:done",),
        created_at=datetime.now(UTC).isoformat(),
    )
    storage.save_run_button_binding("tg-assistant", binding)
    internal_data = bound_run_callback_data(binding.id, 0)
    event = InteractionEvent(
        platform="telegram",
        channel_id="tg-assistant",
        chat_id="12345",
        user_id="50",
        message_id="777",
        data=internal_data,
        buttons=(
            (InteractionButton(label="✅ Milk", data="chk:milk"),),
            (InteractionButton(label="Fertig", data=internal_data),),
        ),
    )
    conversation = make_conversation(kind="direct", user_id=50)

    outcome = await engine.trigger_interaction_reply(conversation, event)
    await engine.handle_inbound_text(conversation, "den rest kannst du löschen")
    await drain(engine, 12345)

    assert outcome == "enqueued"
    assert len(trigger_mock.await_args_list) == 2
    first_call, second_call = trigger_mock.await_args_list
    assert first_call.args[2] == "origin-session"
    assert "run:done" in first_call.args[1]
    assert internal_data not in first_call.args[1]
    assert second_call.args[:3] == (
        "assistant",
        "den rest kannst du löschen",
        "origin-session",
    )
    anchor_metadata = sessions.get_metadata("assistant", SESSION_ID)
    assert anchor_metadata[engine_module.ACTIVE_SESSION_METADATA_KEY] == "origin-session"
    assert transport.sent_texts == ["synced", "deleted"]

    duplicate = await engine.trigger_interaction_reply(conversation, event)
    assert duplicate == "already_handled"
    assert len(trigger_mock.await_args_list) == 2
    await engine.stop()


@pytest.mark.asyncio
async def test_bound_tap_does_not_recreate_missing_origin_session(tmp_path: Path) -> None:
    storage = ChannelStorage(tmp_path)
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="unexpected"))
    engine, sessions, _trigger, _transport = make_engine(
        tmp_path,
        trigger_run=trigger_mock,
        run_button_binding_registry=storage,
    )
    binding = RunButtonBinding(
        id="binding-missing",
        platform_target="12345",
        thread_id=None,
        origin_session_id="deleted-session",
        original_button_data=("run:done",),
        created_at=datetime.now(UTC).isoformat(),
    )
    storage.save_run_button_binding("tg-assistant", binding)
    internal_data = bound_run_callback_data(binding.id, 0)
    event = InteractionEvent(
        platform="telegram",
        channel_id="tg-assistant",
        chat_id="12345",
        user_id="50",
        message_id="777",
        data=internal_data,
        buttons=((InteractionButton(label="Fertig", data=internal_data),),),
    )

    outcome = await engine.trigger_interaction_reply(make_conversation(), event)

    assert outcome == "unavailable"
    assert not sessions.exists("assistant", "deleted-session")
    trigger_mock.assert_not_awaited()
    await engine.stop()


@pytest.mark.asyncio
async def test_new_detaches_telegram_after_bound_tap(tmp_path: Path) -> None:
    storage = ChannelStorage(tmp_path)
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="synced"))
    engine, sessions, _trigger, _transport = make_engine(
        tmp_path,
        trigger_run=trigger_mock,
        command_dispatcher=make_new_only_dispatcher(),
        run_button_binding_registry=storage,
    )
    sessions.create("assistant", session_id="origin-session")
    binding = RunButtonBinding(
        id="binding-new",
        platform_target="12345",
        thread_id=None,
        origin_session_id="origin-session",
        original_button_data=("run:done",),
        created_at=datetime.now(UTC).isoformat(),
    )
    storage.save_run_button_binding("tg-assistant", binding)
    internal_data = bound_run_callback_data(binding.id, 0)
    event = InteractionEvent(
        platform="telegram",
        channel_id="tg-assistant",
        chat_id="12345",
        user_id="50",
        message_id="777",
        data=internal_data,
        buttons=((InteractionButton(label="Fertig", data=internal_data),),),
    )
    conversation = make_conversation()

    assert await engine.trigger_interaction_reply(conversation, event) == "enqueued"
    await drain(engine, 12345)
    await engine.handle_inbound_text(conversation, "/new")
    await drain(engine, 12345)

    detached_session_id = sessions.get_metadata("assistant", SESSION_ID)[
        engine_module.ACTIVE_SESSION_METADATA_KEY
    ]
    assert detached_session_id not in {SESSION_ID, "origin-session"}
    assert sessions.exists("assistant", detached_session_id)
    await engine.stop()


@pytest.mark.asyncio
async def test_busy_bound_tap_restores_binding_and_previous_conversation_pointer(
    tmp_path: Path,
) -> None:
    storage = ChannelStorage(tmp_path)
    waiting_work = ChatRunManager(waiting_work_limit=1)
    held_admission = waiting_work.reserve_waiting_work(scope="already-busy", scope_limit=1)
    engine, sessions, trigger_mock, transport = make_engine(
        tmp_path,
        waiting_work_manager=waiting_work,
        run_button_binding_registry=storage,
    )
    sessions.create("assistant", session_id=SESSION_ID)
    sessions.create("assistant", session_id="prior-session")
    sessions.create("assistant", session_id="origin-session")
    previous_metadata = {
        "existing": "preserved",
        engine_module.ACTIVE_SESSION_METADATA_KEY: "prior-session",
    }
    sessions.set_metadata("assistant", SESSION_ID, previous_metadata)
    binding = RunButtonBinding(
        id="binding-busy",
        platform_target="12345",
        thread_id=None,
        origin_session_id="origin-session",
        original_button_data=("run:done",),
        created_at=datetime.now(UTC).isoformat(),
    )
    storage.save_run_button_binding("tg-assistant", binding)
    internal_data = bound_run_callback_data(binding.id, 0)
    event = InteractionEvent(
        platform="telegram",
        channel_id="tg-assistant",
        chat_id="12345",
        user_id="50",
        message_id="777",
        data=internal_data,
        buttons=((InteractionButton(label="Fertig", data=internal_data),),),
    )

    outcome = await engine.trigger_interaction_reply(make_conversation(), event)

    assert outcome == "busy"
    assert sessions.get_metadata("assistant", SESSION_ID) == previous_metadata
    retry_claim = storage.claim_run_button_binding(
        "tg-assistant",
        binding.id,
        platform_target="12345",
        thread_id=None,
    )
    assert retry_claim.status == "claimed"
    trigger_mock.assert_not_awaited()
    assert transport.sent_texts == [engine_module._BUSY_REPLY]
    waiting_work.release_waiting_work(held_admission)
    await engine.stop()
