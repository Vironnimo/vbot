"""Channel media ingestion and failure-isolation tests."""

from __future__ import annotations

from .engine_test_support import (
    CHANNEL_REPLY_SURFACE,
    SESSION_ID,
    Any,
    AsyncMock,
    AttachmentTooLargeError,
    AttachmentTypeNotAllowedError,
    ContentBlock,
    FakeTransport,
    MediaBlock,
    MessageFacts,
    MessageSender,
    Path,
    QuotedMessageFacts,
    RunKind,
    SimpleNamespace,
    TextBlock,
    assert_member_trigger,
    command_outcome,
    drain,
    engine_module,
    make_command_dispatcher,
    make_completed_run,
    make_conversation,
    make_engine,
    pytest,
)


@pytest.mark.asyncio
async def test_block_content_skips_command_dispatch_and_triggers_run(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    command_dispatcher = make_command_dispatcher(result=command_outcome("stop", "Run cancelled."))
    engine, _sessions, _trigger, transport = make_engine(
        tmp_path, trigger_run=trigger_mock, command_dispatcher=command_dispatcher
    )

    content: list[ContentBlock] = [TextBlock(type="text", text="/stop")]
    queued = engine_module._QueuedInboundMessage(
        conversation=make_conversation(),
        message=MessageFacts(content=content),
    )

    await engine._process_queued_message(queued)

    command_dispatcher.execute.assert_not_awaited()
    trigger_mock.assert_awaited_once_with(
        "assistant",
        content,
        SESSION_ID,
        sender=None,
        reply_surface=CHANNEL_REPLY_SURFACE,
        run_kind=RunKind.CHANNEL,
    )
    assert transport.sent == [("12345", "ok")]
    await engine.stop()


@pytest.mark.asyncio
async def test_media_failure_isolates_siblings_and_triggers_successful_blocks(
    tmp_path: Path,
) -> None:
    block = MediaBlock(
        type="media", attachment_id="att-1", filename="a.png", media_type="image/png"
    )

    async def media_builder(raw_message: Any) -> list[ContentBlock]:
        if raw_message == "ok":
            return [block]
        raise RuntimeError("download failed")

    transport = FakeTransport(media_builder=media_builder)
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, _sessions, _trigger, _transport = make_engine(
        tmp_path, trigger_run=trigger_mock, transport=transport
    )

    queued = engine_module._QueuedInboundMedia(
        conversation=make_conversation(),
        messages=("ok", "broken"),
    )

    await engine._process_queued_media(queued)

    assert transport.sent_texts == [engine_module._MEDIA_FAILED_REPLY, "ok"]
    trigger_mock.assert_awaited_once()
    await_args = trigger_mock.await_args
    assert await_args is not None
    assert await_args.args[1] == [block]
    await engine.stop()


@pytest.mark.asyncio
async def test_media_duplicate_failure_replies_are_deduped(tmp_path: Path) -> None:
    async def media_builder(_raw_message: Any) -> list[ContentBlock]:
        raise RuntimeError("download failed")

    transport = FakeTransport(media_builder=media_builder)
    trigger_mock = AsyncMock()
    engine, _sessions, _trigger, _transport = make_engine(
        tmp_path, trigger_run=trigger_mock, transport=transport
    )

    queued = engine_module._QueuedInboundMedia(
        conversation=make_conversation(),
        messages=("broken-a", "broken-b"),
    )

    await engine._process_queued_media(queued)

    assert transport.sent_texts == [engine_module._MEDIA_FAILED_REPLY]
    trigger_mock.assert_not_awaited()
    await engine.stop()


@pytest.mark.asyncio
async def test_media_companion_text_precedes_built_media_blocks(tmp_path: Path) -> None:
    block = MediaBlock(
        type="media", attachment_id="att-1", filename="a.png", media_type="image/png"
    )
    transport = FakeTransport(media_builder=AsyncMock(return_value=[block]))
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, _sessions, _trigger, _transport = make_engine(
        tmp_path, trigger_run=trigger_mock, transport=transport
    )

    await engine.handle_inbound_media(
        make_conversation(),
        ("forwarded-photo",),
        companion_text="Please edit this",
    )
    await drain(engine, 12345)

    trigger_mock.assert_awaited_once_with(
        "assistant",
        [TextBlock(type="text", text="Please edit this"), block],
        SESSION_ID,
        sender=None,
        reply_surface=CHANNEL_REPLY_SURFACE,
        run_kind=RunKind.CHANNEL,
    )
    await engine.stop()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (AttachmentTypeNotAllowedError("nope"), engine_module._UNSUPPORTED_FILE_REPLY),
        (AttachmentTooLargeError("too big"), engine_module._FILE_TOO_LARGE_REPLY),
        (RuntimeError("other"), engine_module._MEDIA_FAILED_REPLY),
    ],
)
def test_media_failure_reply_mapping(error: Exception, expected: str) -> None:
    assert engine_module._media_failure_reply(error) == expected


@pytest.mark.asyncio
async def test_media_path_carries_group_sender(tmp_path: Path) -> None:
    block = MediaBlock(
        type="media", attachment_id="att-1", filename="a.png", media_type="image/png"
    )

    async def media_builder(_raw_message: Any) -> list[ContentBlock]:
        return [block]

    transport = FakeTransport(media_builder=media_builder)
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, _sessions, _trigger, _transport = make_engine(
        tmp_path, trigger_run=trigger_mock, transport=transport, response_mode="all"
    )
    conversation = make_conversation(kind="group", user_display_name="Alice")

    await engine.handle_inbound_media(conversation, ("photo",))
    await drain(engine, 12345)

    assert_member_trigger(
        trigger_mock,
        "assistant",
        [block],
        SESSION_ID,
        sender=MessageSender(id="50", display_name="Alice"),
    )
    await engine.stop()


@pytest.mark.asyncio
async def test_addressed_group_reply_ingests_quoted_media_with_original_authority(
    tmp_path: Path,
) -> None:
    block = MediaBlock(
        type="media",
        attachment_id="att-quoted",
        filename="juan.png",
        media_type="image/png",
    )
    quoted_builder = AsyncMock(
        return_value=QuotedMessageFacts(
            user_id="77",
            user_display_name="Juan",
            content=[block],
        )
    )
    transport = FakeTransport(quoted_builder=quoted_builder)
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, _sessions, _trigger, _transport = make_engine(
        tmp_path,
        trigger_run=trigger_mock,
        transport=transport,
        admin_user_ids=["77"],
    )

    await engine.handle_inbound_text(
        make_conversation(
            kind="group",
            user_id=50,
            user_display_name="Alice",
            mentioned_bot=True,
        ),
        "What do you think of Juan's image?",
        raw_message="telegram-reply",
    )
    await drain(engine, 12345)

    quoted_builder.assert_awaited_once_with("telegram-reply")
    assert_member_trigger(
        trigger_mock,
        "assistant",
        [
            TextBlock(type="text", text="What do you think of Juan's image?"),
            TextBlock(type="text", text="[quoted-message] [Juan|77|admin]:"),
            block,
        ],
        SESSION_ID,
        sender=MessageSender(id="50", display_name="Alice"),
    )
    await engine.stop()


@pytest.mark.asyncio
async def test_unaddressed_group_reply_does_not_resolve_quoted_media(tmp_path: Path) -> None:
    quoted_builder = AsyncMock()
    transport = FakeTransport(quoted_builder=quoted_builder)
    engine, _sessions, trigger_mock, _transport = make_engine(
        tmp_path,
        transport=transport,
    )

    await engine.handle_inbound_text(
        make_conversation(kind="group"),
        "What about this?",
        raw_message="telegram-reply",
    )
    await drain(engine, 12345)

    quoted_builder.assert_not_awaited()
    trigger_mock.assert_not_awaited()
    await engine.stop()


@pytest.mark.asyncio
async def test_unavailable_quoted_message_keeps_triggering_question(tmp_path: Path) -> None:
    transport = FakeTransport(
        quoted_builder=AsyncMock(
            return_value=QuotedMessageFacts(
                user_id=None,
                user_display_name=None,
                content=None,
            )
        )
    )
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, _sessions, _trigger, _transport = make_engine(
        tmp_path,
        trigger_run=trigger_mock,
        transport=transport,
    )

    await engine.handle_inbound_text(
        make_conversation(kind="group", mentioned_bot=True),
        "What was in the deleted reply?",
        raw_message="missing-reply",
    )
    await drain(engine, 12345)

    assert_member_trigger(
        trigger_mock,
        "assistant",
        [
            TextBlock(type="text", text="What was in the deleted reply?"),
            TextBlock(type="text", text="[quoted-message unavailable]"),
        ],
        SESSION_ID,
        sender=MessageSender(id="50", display_name="50"),
    )
    await engine.stop()


@pytest.mark.asyncio
async def test_group_media_without_addressing_is_dropped(tmp_path: Path) -> None:
    transport = FakeTransport()
    trigger_mock = AsyncMock()
    engine, chat_sessions, _trigger, _transport = make_engine(
        tmp_path, trigger_run=trigger_mock, transport=transport
    )

    await engine.handle_inbound_media(
        make_conversation(kind="group"),
        (SimpleNamespace(caption=None),),
    )
    await drain(engine, 12345)

    trigger_mock.assert_not_awaited()
    assert transport.sent == []
    assert not chat_sessions.exists("assistant", SESSION_ID)
    await engine.stop()


@pytest.mark.asyncio
async def test_group_unaddressed_media_is_observed_without_download(tmp_path: Path) -> None:
    media_builder = AsyncMock(return_value=[])
    transport = FakeTransport(media_builder=media_builder)
    engine, chat_sessions, trigger_mock, _transport = make_engine(
        tmp_path,
        trigger_run=AsyncMock(),
        transport=transport,
        observe_unaddressed=True,
    )

    await engine.handle_inbound_media(
        make_conversation(kind="group", user_display_name="Alice"),
        (SimpleNamespace(caption="look"), SimpleNamespace(caption=None)),
    )
    await drain(engine, 12345)

    notes = [
        message.content
        for message in chat_sessions.get("assistant", SESSION_ID).load()
        if message.role == "note"
    ]
    assert notes[-2:] == [
        "[channel-message] [Alice|50|member]: [media] look",
        "[channel-message] [Alice|50|member]: [media message]",
    ]
    media_builder.assert_not_awaited()
    trigger_mock.assert_not_awaited()
    assert transport.sent == []
    await engine.stop()


@pytest.mark.asyncio
async def test_group_media_caption_wake_word_triggers(tmp_path: Path) -> None:
    block = MediaBlock(
        type="media", attachment_id="att-1", filename="a.png", media_type="image/png"
    )

    async def media_builder(_raw_message: Any) -> list[ContentBlock]:
        return [block]

    transport = FakeTransport(media_builder=media_builder)
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, _sessions, _trigger, _transport = make_engine(
        tmp_path,
        trigger_run=trigger_mock,
        transport=transport,
        mention_patterns=[r"\bvbot\b"],
    )

    await engine.handle_inbound_media(
        make_conversation(kind="group"),
        (SimpleNamespace(caption=None), SimpleNamespace(caption="vbot look at this")),
    )
    await drain(engine, 12345)

    trigger_mock.assert_awaited_once()
    await engine.stop()
