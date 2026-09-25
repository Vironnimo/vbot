"""Channel reply relay and interaction-tap tests."""

from __future__ import annotations

import asyncio
import threading

import core.channels._conversation_content as content_module
from core.channels.adapter import RunButtonBinding, bound_run_callback_data
from core.database import DatabaseUnavailableError
from core.sessions import SessionAddress
from core.utils.timestamps import utc_now_timestamp

from .engine_test_support import (
    ASSISTANT_OUTPUT_EVENT,
    CHANNEL_REPLY_SURFACE,
    SESSION_ID,
    AsyncMock,
    ChatRunManager,
    InteractionButton,
    InteractionEvent,
    Path,
    Run,
    channel_state,
    drain,
    engine_module,
    logging,
    make_cancelled_run,
    make_completed_run,
    make_conversation,
    make_empty_completed_run,
    make_engine,
    make_failed_run,
    make_interrupted_run,
    make_new_only_dispatcher,
    pytest,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["claim", "pointer", "lookup_failure"])
async def test_aborted_bound_tap_restores_unadmitted_state(tmp_path, monkeypatch, stage):
    storage = channel_state(tmp_path)
    engine, sessions, trigger, _transport = make_engine(
        tmp_path, run_button_binding_registry=storage
    )
    sessions.create("assistant", session_id=SESSION_ID)
    sessions.create("assistant", session_id="origin")
    address = SessionAddress(project_id=None, agent_id="assistant", session_id=SESSION_ID)
    previous = {"other": "keep"}
    sessions.set_metadata(address, previous)
    storage.point_conversation("tg-assistant", SESSION_ID, "direct", "prior")
    binding = RunButtonBinding(
        id="cancelled-binding",
        platform_target="12345",
        thread_id=None,
        origin_session_id="origin",
        original_button_data=("run:done",),
        created_at=utc_now_timestamp(),
    )
    storage.save_run_button_binding("tg-assistant", binding)
    data = bound_run_callback_data(binding.id, 0)
    event = InteractionEvent(
        platform="telegram",
        channel_id="tg-assistant",
        chat_id="12345",
        user_id="50",
        message_id="777",
        data=data,
        buttons=((InteractionButton(label="Done", data=data),),),
    )
    entered, release = threading.Event(), threading.Event()
    owner, name = {
        "claim": (storage, "claim_run_button_binding"),
        "pointer": (engine._routing, "_point_conversation_at_session"),
        "lookup_failure": (sessions, "exists"),
    }[stage]
    original = getattr(owner, name)

    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        if stage == "lookup_failure":
            raise OSError("Session lookup failed")
        return result

    monkeypatch.setattr(owner, name, delayed)
    pending = asyncio.create_task(engine.trigger_interaction_reply(make_conversation(), event))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        if stage != "lookup_failure":
            pending.cancel()
        release.set()
        with pytest.raises(OSError if stage == "lookup_failure" else asyncio.CancelledError):
            await pending
        assert sessions.get_metadata(address) == previous
        assert storage.active_session_id("tg-assistant", SESSION_ID) == "prior"
        monkeypatch.setattr(owner, name, original)
        claim = storage.claim_run_button_binding(
            "tg-assistant", binding.id, platform_target="12345", thread_id=None
        )
        assert claim.status == "claimed"
        trigger.assert_not_awaited()
    finally:
        release.set()
        await engine.stop()


@pytest.mark.asyncio
async def test_bound_tap_on_a_closed_session_database_restores_the_binding(tmp_path):
    storage = channel_state(tmp_path)
    engine, sessions, trigger, _transport = make_engine(
        tmp_path, run_button_binding_registry=storage
    )
    sessions.create("assistant", session_id="origin")
    binding = RunButtonBinding(
        id="closed-binding",
        platform_target="12345",
        thread_id=None,
        origin_session_id="origin",
        original_button_data=("run:done",),
        created_at=utc_now_timestamp(),
    )
    storage.save_run_button_binding("tg-assistant", binding)
    data = bound_run_callback_data(binding.id, 0)
    event = InteractionEvent(
        platform="telegram",
        channel_id="tg-assistant",
        chat_id="12345",
        user_id="50",
        message_id="777",
        data=data,
        buttons=((InteractionButton(label="Done", data=data),),),
    )
    sessions.close()
    try:
        with pytest.raises(DatabaseUnavailableError):
            await engine.trigger_interaction_reply(make_conversation(), event)

        # The claim was compensated on the Channel state's own pool.
        claim = storage.claim_run_button_binding(
            "tg-assistant", binding.id, platform_target="12345", thread_id=None
        )
        assert claim.status == "claimed"
        trigger.assert_not_awaited()
    finally:
        await engine.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_waiter", [False, True])
async def test_aborted_tap_cannot_undo_later_admission_to_same_origin(
    tmp_path, monkeypatch, cancel_waiter
):
    storage = channel_state(tmp_path)
    trigger = AsyncMock(return_value=make_completed_run(output_text="done", session_id="origin"))
    engine, sessions, _, _ = make_engine(
        tmp_path, run_button_binding_registry=storage, trigger_run=trigger
    )
    sessions.create("assistant", session_id=SESSION_ID)
    sessions.create("assistant", session_id="origin")
    storage.point_conversation("tg-assistant", SESSION_ID, "direct", "prior")
    events = []
    for name in ("first", "second"):
        binding = RunButtonBinding(
            id=name,
            platform_target="12345",
            thread_id=None,
            origin_session_id="origin",
            original_button_data=("run:done",),
            created_at=utc_now_timestamp(),
        )
        storage.save_run_button_binding("tg-assistant", binding)
        data = bound_run_callback_data(name, 0)
        events.append(
            InteractionEvent(
                platform="telegram",
                channel_id="tg-assistant",
                chat_id="12345",
                user_id="50",
                message_id="777",
                data=data,
                buttons=((InteractionButton(label="Done", data=data),),),
            )
        )
    entered, release = threading.Event(), threading.Event()
    original = engine._routing._point_conversation_at_session
    calls = 0

    def delayed(*args):
        nonlocal calls
        calls += 1
        first = calls == 1
        result = original(*args)
        if first:
            entered.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(engine._routing, "_point_conversation_at_session", delayed)
    first = asyncio.create_task(engine.trigger_interaction_reply(make_conversation(), events[0]))
    pending = [first]
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        first.cancel()
        if cancel_waiter:
            waiter = asyncio.create_task(
                engine.trigger_interaction_reply(make_conversation(), events[1])
            )
            pending.append(waiter)
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
        second = asyncio.create_task(
            engine.trigger_interaction_reply(make_conversation(), events[1])
        )
        pending.append(second)
        completed, _ = await asyncio.wait({second}, timeout=0.05)
        assert not completed
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert await second == "enqueued"
        assert storage.active_session_id("tg-assistant", SESSION_ID) == "origin"
        await drain(engine, "12345")
        assert trigger.await_count == 1
    finally:
        release.set()
        await asyncio.gather(*pending, return_exceptions=True)
        await engine.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_origin", [False, True])
async def test_waiting_bound_tap_keeps_origin_after_anchor_changes(tmp_path, delete_origin):
    storage = channel_state(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()

    async def trigger(agent_id, content, session_id, **kwargs):
        if content == "hold":
            started.set()
            await release.wait()
        return make_completed_run(output_text="done", session_id=session_id)

    trigger_mock = AsyncMock(side_effect=trigger)
    engine, sessions, _, _transport = make_engine(
        tmp_path, trigger_run=trigger_mock, run_button_binding_registry=storage
    )
    sessions.create("assistant", session_id="origin")
    sessions.create("assistant", session_id="other")
    binding = RunButtonBinding(
        id="waiting-binding",
        platform_target="12345",
        thread_id=None,
        origin_session_id="origin",
        original_button_data=("run:done",),
        created_at=utc_now_timestamp(),
    )
    storage.save_run_button_binding("tg-assistant", binding)
    data = bound_run_callback_data(binding.id, 0)
    event = InteractionEvent(
        platform="telegram",
        channel_id="tg-assistant",
        chat_id="12345",
        user_id="50",
        message_id="777",
        data=data,
        buttons=((InteractionButton(label="Done", data=data),),),
    )
    conversation = make_conversation()
    try:
        await engine.handle_inbound_text(conversation, "hold")
        await asyncio.wait_for(started.wait(), 10)
        assert await engine.trigger_interaction_reply(conversation, event) == "enqueued"
        storage.point_conversation("tg-assistant", SESSION_ID, "direct", "other")
        if delete_origin:
            await sessions.archive(
                SessionAddress(project_id=None, agent_id="assistant", session_id="origin")
            )
        release.set()
        await drain(engine, 12345)
        calls = trigger_mock.await_args_list
        assert [call.args[2] for call in calls] == (
            [SESSION_ID] if delete_origin else [SESSION_ID, "origin"]
        )
        assert storage.active_session_id("tg-assistant", SESSION_ID) == "other"
        if delete_origin:
            assert not sessions.exists(
                SessionAddress(project_id=None, agent_id="assistant", session_id="origin")
            )
    finally:
        release.set()
        await engine.stop()


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
async def test_interrupted_run_forwards_preserved_partial_output(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_interrupted_run(output_text="preserved partial"))
    engine, _sessions, _trigger, transport = make_engine(tmp_path, trigger_run=trigger_mock)

    await engine.handle_inbound_text(make_conversation(), "hello")
    await drain(engine, 12345)

    assert transport.sent_texts == ["preserved partial"]
    await engine.stop()


@pytest.mark.asyncio
async def test_recovered_run_forwards_partial_and_continuation_without_added_text(
    tmp_path: Path,
) -> None:
    run = Run(run_id="run-recovered", agent_id="assistant", session_id=SESSION_ID)
    run.emit(
        ASSISTANT_OUTPUT_EVENT,
        {"message": {"content": "preserved partial", "interrupted": True}},
    )
    run.emit(ASSISTANT_OUTPUT_EVENT, {"message": {"content": " continuation"}})
    run.mark_completed("ok")
    trigger_mock = AsyncMock(return_value=run)
    engine, _sessions, _trigger, transport = make_engine(tmp_path, trigger_run=trigger_mock)

    await engine.handle_inbound_text(make_conversation(), "hello")
    await drain(engine, 12345)

    assert transport.sent_texts == ["preserved partial continuation"]
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
    metadata = chat_sessions.get_metadata(
        SessionAddress(project_id=None, agent_id="assistant", session_id=SESSION_ID)
    )
    assert metadata["last_reply_target"] == {
        "channel_id": "tg-assistant",
        "platform_target": "12345",
        "thread_id": "42",
    }

    # A later non-topic message rewrites the reply target without the thread key.
    await engine.handle_inbound_text(make_conversation(kind="group", mentioned_bot=True), "hi")
    await drain(engine, 12345)
    metadata = chat_sessions.get_metadata(
        SessionAddress(project_id=None, agent_id="assistant", session_id=SESSION_ID)
    )
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
    note = content_module._format_interaction_note(
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
    note = content_module._format_interaction_note(
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
    storage = channel_state(tmp_path)
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
        created_at=utc_now_timestamp(),
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
    assert storage.active_session_id("tg-assistant", SESSION_ID) == "origin-session"
    assert transport.sent_texts == ["synced", "deleted"]

    duplicate = await engine.trigger_interaction_reply(conversation, event)
    assert duplicate == "already_handled"
    assert len(trigger_mock.await_args_list) == 2
    await engine.stop()


@pytest.mark.asyncio
async def test_bound_tap_does_not_recreate_missing_origin_session(tmp_path: Path) -> None:
    storage = channel_state(tmp_path)
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
        created_at=utc_now_timestamp(),
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
    assert not sessions.exists(
        SessionAddress(project_id=None, agent_id="assistant", session_id="deleted-session")
    )
    trigger_mock.assert_not_awaited()
    await engine.stop()


@pytest.mark.asyncio
async def test_new_detaches_telegram_after_bound_tap(tmp_path: Path) -> None:
    storage = channel_state(tmp_path)
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
        created_at=utc_now_timestamp(),
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

    detached_session_id = storage.active_session_id("tg-assistant", SESSION_ID)
    assert detached_session_id is not None
    assert detached_session_id not in {SESSION_ID, "origin-session"}
    assert sessions.exists(
        SessionAddress(project_id=None, agent_id="assistant", session_id=detached_session_id)
    )
    await engine.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_pointer", [None, "prior-session"])
@pytest.mark.parametrize("concurrent", [None, "metadata", "navigation"])
async def test_busy_bound_tap_restores_binding_and_previous_conversation_pointer(
    tmp_path: Path,
    monkeypatch,
    prior_pointer: str | None,
    concurrent,
) -> None:
    storage = channel_state(tmp_path)
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
    previous_metadata = {"existing": "preserved"}
    sessions.set_metadata(
        SessionAddress(project_id=None, agent_id="assistant", session_id=SESSION_ID),
        previous_metadata,
    )
    if prior_pointer is not None:
        storage.point_conversation("tg-assistant", SESSION_ID, "direct", prior_pointer)
    binding = RunButtonBinding(
        id="binding-busy",
        platform_target="12345",
        thread_id=None,
        origin_session_id="origin-session",
        original_button_data=("run:done",),
        created_at=utc_now_timestamp(),
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

    expected_metadata = dict(previous_metadata)
    expected_pointer = prior_pointer
    if concurrent is not None:
        original_enqueue = engine._enqueue_chat_work
        if concurrent == "metadata":
            expected_metadata["new_metadata"] = "preserved"
        else:
            expected_pointer = "newer-session"

        def enqueue(*args):
            if concurrent == "metadata":
                sessions.mutate_metadata(
                    SessionAddress(project_id=None, agent_id="assistant", session_id=SESSION_ID),
                    lambda metadata: metadata.update(new_metadata="preserved"),
                )
            else:
                storage.point_conversation("tg-assistant", SESSION_ID, "direct", "newer-session")
            return original_enqueue(*args)

        monkeypatch.setattr(engine, "_enqueue_chat_work", enqueue)

    outcome = await engine.trigger_interaction_reply(make_conversation(), event)

    assert outcome == "busy"
    assert (
        sessions.get_metadata(
            SessionAddress(project_id=None, agent_id="assistant", session_id=SESSION_ID)
        )
        == expected_metadata
    )
    assert storage.active_session_id("tg-assistant", SESSION_ID) == expected_pointer
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
