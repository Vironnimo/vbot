"""Channel group addressing, participants, and permission tests."""

from __future__ import annotations

from .engine_test_support import (
    CHANNEL_REPLY_SURFACE,
    SESSION_ID,
    AsyncMock,
    MessageSender,
    Path,
    command_outcome,
    drain,
    make_command_dispatcher,
    make_completed_run,
    make_conversation,
    make_engine,
    pytest,
)


@pytest.mark.asyncio
async def test_group_message_triggers_run_with_sender(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, _sessions, _trigger, _transport = make_engine(
        tmp_path, trigger_run=trigger_mock, response_mode="all"
    )

    await engine.handle_inbound_text(
        make_conversation(kind="group", user_display_name="Alice"),
        "hello",
    )
    await drain(engine, 12345)

    trigger_mock.assert_awaited_once_with(
        "assistant",
        "hello",
        SESSION_ID,
        sender=MessageSender(id="50", display_name="Alice"),
        reply_surface=CHANNEL_REPLY_SURFACE,
    )
    await engine.stop()


@pytest.mark.asyncio
async def test_direct_message_triggers_run_without_sender(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, _sessions, _trigger, _transport = make_engine(tmp_path, trigger_run=trigger_mock)

    await engine.handle_inbound_text(
        make_conversation(kind="direct", user_display_name="Alice"),
        "hello",
    )
    await drain(engine, 12345)

    trigger_mock.assert_awaited_once_with(
        "assistant",
        "hello",
        SESSION_ID,
        sender=None,
        reply_surface=CHANNEL_REPLY_SURFACE,
    )
    await engine.stop()


@pytest.mark.asyncio
async def test_group_sender_display_name_falls_back_to_user_id(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, _sessions, _trigger, _transport = make_engine(
        tmp_path, trigger_run=trigger_mock, response_mode="all"
    )

    await engine.handle_inbound_text(make_conversation(kind="group"), "hello")
    await drain(engine, 12345)

    trigger_mock.assert_awaited_once_with(
        "assistant",
        "hello",
        SESSION_ID,
        sender=MessageSender(id="50", display_name="50"),
        reply_surface=CHANNEL_REPLY_SURFACE,
    )
    await engine.stop()


@pytest.mark.asyncio
async def test_participants_metadata_written_for_groups_only(tmp_path: Path) -> None:
    engine, chat_sessions, _trigger, _transport = make_engine(tmp_path)

    engine.prepare_inbound_route(make_conversation(kind="direct", user_display_name="Alice"))
    direct_metadata = chat_sessions.get_metadata("assistant", SESSION_ID)
    assert "participants" not in direct_metadata

    engine.prepare_inbound_route(make_conversation(kind="group", user_display_name="Alice"))
    group_metadata = chat_sessions.get_metadata("assistant", SESSION_ID)
    participants = group_metadata["participants"]
    assert set(participants) == {"50"}
    assert participants["50"]["display_name"] == "Alice"
    assert participants["50"]["last_seen_at"].endswith("+00:00")
    await engine.stop()


@pytest.mark.asyncio
async def test_participants_metadata_updated_on_repeat_messages(tmp_path: Path) -> None:
    engine, chat_sessions, _trigger, _transport = make_engine(tmp_path)

    engine.prepare_inbound_route(
        make_conversation(kind="group", user_id=50, user_display_name="Alice")
    )
    engine.prepare_inbound_route(
        make_conversation(kind="group", user_id=51, user_display_name="Bob")
    )
    engine.prepare_inbound_route(
        make_conversation(kind="group", user_id=50, user_display_name="Alice Renamed")
    )

    participants = chat_sessions.get_metadata("assistant", SESSION_ID)["participants"]
    assert set(participants) == {"50", "51"}
    assert participants["50"]["display_name"] == "Alice Renamed"
    assert participants["51"]["display_name"] == "Bob"
    await engine.stop()


@pytest.mark.asyncio
async def test_group_unaddressed_text_is_dropped_in_mention_mode(tmp_path: Path) -> None:
    command_dispatcher = make_command_dispatcher()
    engine, chat_sessions, trigger_mock, transport = make_engine(
        tmp_path, command_dispatcher=command_dispatcher
    )

    await engine.handle_inbound_text(make_conversation(kind="group"), "hello everyone")
    await drain(engine, 12345)

    trigger_mock.assert_not_awaited()
    command_dispatcher.execute.assert_not_awaited()
    assert transport.sent == []
    # Dropped messages must not create a Session either.
    assert not chat_sessions.exists("assistant", SESSION_ID)
    await engine.stop()


@pytest.mark.asyncio
async def test_group_unaddressed_text_is_observed_as_note(tmp_path: Path) -> None:
    command_dispatcher = make_command_dispatcher()
    engine, chat_sessions, trigger_mock, transport = make_engine(
        tmp_path,
        command_dispatcher=command_dispatcher,
        observe_unaddressed=True,
    )

    await engine.handle_inbound_text(
        make_conversation(
            kind="group",
            user_id="|50]\r",
            user_display_name="[Alice]\n|",
        ),
        "hello\nworld",
    )
    await drain(engine, 12345)

    notes = [
        message.content
        for message in chat_sessions.get("assistant", SESSION_ID).load()
        if message.role == "note"
    ]
    assert notes == ["[channel-message] Alice (50): hello\nworld"]
    trigger_mock.assert_not_awaited()
    command_dispatcher.execute.assert_not_awaited()
    assert transport.sent == []
    await engine.stop()


@pytest.mark.asyncio
async def test_observed_group_message_updates_metadata_and_participant(tmp_path: Path) -> None:
    engine, chat_sessions, _trigger, _transport = make_engine(
        tmp_path,
        observe_unaddressed=True,
    )

    await engine.handle_inbound_text(
        make_conversation(kind="group", user_display_name="Alice"),
        "hello everyone",
    )
    await drain(engine, 12345)

    metadata = chat_sessions.get_metadata("assistant", SESSION_ID)
    assert metadata["last_reply_target"] == {
        "channel_id": "tg-assistant",
        "platform_target": "12345",
    }
    assert metadata["participants"]["50"]["display_name"] == "Alice"
    assert metadata["participants"]["50"]["last_seen_at"].endswith("+00:00")
    await engine.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mentioned_bot", "is_reply_to_bot"),
    [(True, False), (False, True)],
)
async def test_group_addressed_text_triggers_in_mention_mode(
    tmp_path: Path,
    mentioned_bot: bool,
    is_reply_to_bot: bool,
) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, chat_sessions, _trigger, _transport = make_engine(
        tmp_path,
        trigger_run=trigger_mock,
        observe_unaddressed=True,
    )

    await engine.handle_inbound_text(
        make_conversation(
            kind="group", mentioned_bot=mentioned_bot, is_reply_to_bot=is_reply_to_bot
        ),
        "hello bot",
    )
    await drain(engine, 12345)

    trigger_mock.assert_awaited_once()
    notes = chat_sessions.get("assistant", SESSION_ID).load()
    assert not any(
        message.role == "note"
        and isinstance(message.content, str)
        and message.content.startswith("[channel-message] ")
        for message in notes
    )
    await engine.stop()


@pytest.mark.asyncio
async def test_group_wake_word_pattern_matches_case_insensitively(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, chat_sessions, _trigger, _transport = make_engine(
        tmp_path,
        trigger_run=trigger_mock,
        mention_patterns=[r"\bvbot\b"],
        observe_unaddressed=True,
    )

    await engine.handle_inbound_text(make_conversation(kind="group"), "Hey VBOT, status?")
    await drain(engine, 12345)

    trigger_mock.assert_awaited_once()
    notes = chat_sessions.get("assistant", SESSION_ID).load()
    assert not any(
        message.role == "note"
        and isinstance(message.content, str)
        and message.content.startswith("[channel-message] ")
        for message in notes
    )
    await engine.stop()


@pytest.mark.asyncio
async def test_direct_message_always_triggers_in_mention_mode(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, chat_sessions, _trigger, _transport = make_engine(
        tmp_path,
        trigger_run=trigger_mock,
        observe_unaddressed=True,
    )

    await engine.handle_inbound_text(make_conversation(kind="direct"), "hello")
    await drain(engine, 12345)

    trigger_mock.assert_awaited_once()
    note_contents = [
        message.content
        for message in chat_sessions.get("assistant", SESSION_ID).load()
        if message.role == "note"
    ]
    assert not any(
        isinstance(content, str) and content.startswith("[channel-message] ")
        for content in note_contents
    )
    await engine.stop()


@pytest.mark.asyncio
async def test_group_command_from_owner_is_dispatched(tmp_path: Path) -> None:
    command_dispatcher = make_command_dispatcher(result=command_outcome("stop", "Run cancelled."))
    engine, _sessions, trigger_mock, transport = make_engine(
        tmp_path, command_dispatcher=command_dispatcher, owner_user_ids=["50"]
    )

    await engine.handle_inbound_text(make_conversation(kind="group"), "/stop")
    await drain(engine, 12345)

    command_dispatcher.execute.assert_awaited_once()
    trigger_mock.assert_not_awaited()
    assert transport.sent_texts == ["Run cancelled."]
    await engine.stop()


@pytest.mark.asyncio
async def test_group_command_from_non_owner_is_denied_without_dispatch(tmp_path: Path) -> None:
    command_dispatcher = make_command_dispatcher(result=command_outcome("stop", "Run cancelled."))
    engine, chat_sessions, trigger_mock, transport = make_engine(
        tmp_path,
        command_dispatcher=command_dispatcher,
        owner_user_ids=["99"],
        observe_unaddressed=True,
    )

    await engine.handle_inbound_text(make_conversation(kind="group", user_id=50), "/stop")
    await drain(engine, 12345)

    command_dispatcher.execute.assert_not_awaited()
    trigger_mock.assert_not_awaited()
    assert transport.sent == []
    assert not chat_sessions.exists("assistant", SESSION_ID)
    await engine.stop()


@pytest.mark.asyncio
async def test_group_mention_from_non_owner_starts_a_normal_run(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="research complete"))
    engine, _sessions, _trigger, transport = make_engine(
        tmp_path,
        trigger_run=trigger_mock,
        owner_user_ids=["99"],
    )

    await engine.handle_inbound_text(
        make_conversation(kind="group", user_id=50, mentioned_bot=True),
        "Please research the topic.",
    )
    await drain(engine, 12345)

    trigger_mock.assert_awaited_once()
    assert transport.sent_texts == ["research complete"]
    await engine.stop()


@pytest.mark.asyncio
async def test_group_command_denied_for_everyone_when_owner_list_is_empty(
    tmp_path: Path,
) -> None:
    command_dispatcher = make_command_dispatcher(result=command_outcome("stop", "Run cancelled."))
    engine, _sessions, _trigger, transport = make_engine(
        tmp_path, command_dispatcher=command_dispatcher
    )

    await engine.handle_inbound_text(make_conversation(kind="group"), "/stop")
    await drain(engine, 12345)

    command_dispatcher.execute.assert_not_awaited()
    assert transport.sent == []
    await engine.stop()


@pytest.mark.asyncio
async def test_group_command_auth_applies_in_all_response_mode(tmp_path: Path) -> None:
    command_dispatcher = make_command_dispatcher(result=command_outcome("stop", "Run cancelled."))
    engine, _sessions, _trigger, transport = make_engine(
        tmp_path, command_dispatcher=command_dispatcher, response_mode="all"
    )

    await engine.handle_inbound_text(make_conversation(kind="group"), "/stop")
    await drain(engine, 12345)

    command_dispatcher.execute.assert_not_awaited()
    assert transport.sent == []
    await engine.stop()


@pytest.mark.asyncio
async def test_dm_command_is_authorized_without_owner_list(tmp_path: Path) -> None:
    command_dispatcher = make_command_dispatcher(result=command_outcome("stop", "Run cancelled."))
    engine, _sessions, _trigger, transport = make_engine(
        tmp_path, command_dispatcher=command_dispatcher
    )

    await engine.handle_inbound_text(make_conversation(kind="direct"), "/stop")
    await drain(engine, 12345)

    command_dispatcher.execute.assert_awaited_once()
    assert transport.sent_texts == ["Run cancelled."]
    await engine.stop()
