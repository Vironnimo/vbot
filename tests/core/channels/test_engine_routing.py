"""Channel session derivation, metadata, and routing tests."""

from __future__ import annotations

from .engine_test_support import (
    CHANNEL_REPLY_SURFACE,
    SESSION_ID,
    AsyncMock,
    Path,
    RouteFacts,
    RunKind,
    drain,
    engine_module,
    logging,
    make_completed_run,
    make_conversation,
    make_engine,
    make_new_only_dispatcher,
    pytest,
)


@pytest.mark.parametrize(
    ("dm_scope", "kind", "chat_id", "user_id", "expected"),
    [
        ("per_conversation", "direct", 12345, 987, "ch-tg-assistant-12345"),
        ("main", "direct", 12345, 987, "ch-tg-assistant-main"),
        ("per_peer", "direct", 12345, 987, "ch-tg-assistant-u987"),
        ("per_account_channel_peer", "direct", 12345, 987, "ch-tg-assistant-12345-u987"),
        ("main", "group", -10001, 987, "ch-tg-assistant--10001"),
    ],
)
def test_derive_session_id(
    tmp_path: Path,
    dm_scope: str,
    kind: str,
    chat_id: int,
    user_id: int,
    expected: str,
) -> None:
    engine, _sessions, _trigger, _transport = make_engine(tmp_path, dm_scope=dm_scope)

    session_id = engine._derive_session_id(
        make_conversation(chat_id=chat_id, user_id=user_id, kind=kind)
    )

    assert session_id == expected


@pytest.mark.asyncio
async def test_session_creation_writes_metadata_without_reply_surface_note(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(
        side_effect=[
            make_completed_run(output_text="first"),
            make_completed_run(output_text="second"),
        ]
    )
    engine, chat_sessions, _trigger, _transport = make_engine(tmp_path, trigger_run=trigger_mock)

    await engine.handle_inbound_text(make_conversation(), "hello")
    await drain(engine, 12345)
    await engine.handle_inbound_text(make_conversation(), "hello")
    await drain(engine, 12345)

    session = chat_sessions.get("assistant", SESSION_ID)
    notes = [message for message in session.load() if message.role == "note"]
    metadata = chat_sessions.get_metadata("assistant", SESSION_ID)

    assert notes == []
    assert metadata["source_channel_id"] == "tg-assistant"
    assert metadata["platform"] == "telegram"
    assert metadata["platform_conv_id"] == "12345"
    assert metadata["last_reply_target"] == {
        "channel_id": "tg-assistant",
        "platform_target": "12345",
    }
    await engine.stop()


@pytest.mark.asyncio
async def test_ensure_channel_session_reuses_session_without_writing_notes(
    tmp_path: Path,
) -> None:
    engine, chat_sessions, _trigger, _transport = make_engine(tmp_path)

    route = engine.ensure_channel_session(make_conversation())
    engine.ensure_channel_session(make_conversation())

    assert route == RouteFacts(agent_id="assistant", session_id=SESSION_ID)
    session = chat_sessions.get("assistant", SESSION_ID)
    notes = [message for message in session.load() if message.role == "note"]
    assert notes == []
    await engine.stop()


@pytest.mark.asyncio
async def test_inbound_message_logs_routed_line(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="final reply"))
    engine, _sessions, _trigger, _transport = make_engine(tmp_path, trigger_run=trigger_mock)

    with caplog.at_level(logging.INFO, logger="vbot.channels.engine"):
        await engine.handle_inbound_text(make_conversation(), "hello")
        await drain(engine, 12345)

    routed_line = next(
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("Channel message routed")
    )
    assert "target=12345" in routed_line
    assert f"session={SESSION_ID}" in routed_line
    assert "internal" not in routed_line
    await engine.stop()


@pytest.mark.asyncio
async def test_channel_without_new_routes_to_derived_anchor(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, chat_sessions, _trigger, _transport = make_engine(tmp_path, trigger_run=trigger_mock)

    await engine.handle_inbound_text(make_conversation(), "hello")
    await drain(engine, 12345)

    # Byte-for-byte the pre-pointer behavior: route straight to the derived id.
    trigger_mock.assert_awaited_once_with(
        "assistant",
        "hello",
        SESSION_ID,
        sender=None,
        reply_surface=CHANNEL_REPLY_SURFACE,
        run_kind=RunKind.CHANNEL,
    )
    metadata = chat_sessions.get_metadata("assistant", SESSION_ID)
    assert engine_module.ACTIVE_SESSION_METADATA_KEY not in metadata
    await engine.stop()


@pytest.mark.asyncio
async def test_ensure_channel_session_follows_pointer_after_new(tmp_path: Path) -> None:
    command_dispatcher = make_new_only_dispatcher()
    engine, chat_sessions, _trigger, _transport = make_engine(
        tmp_path, command_dispatcher=command_dispatcher
    )

    await engine.handle_inbound_text(make_conversation(), "/new")
    await drain(engine, 12345)

    new_session_id = chat_sessions.get_metadata("assistant", SESSION_ID)[
        engine_module.ACTIVE_SESSION_METADATA_KEY
    ]
    # Proactive channel_send resolves to the active (pointer) session, not the anchor.
    route = engine.ensure_channel_session(make_conversation())
    assert route == RouteFacts(agent_id="assistant", session_id=new_session_id)
    await engine.stop()
