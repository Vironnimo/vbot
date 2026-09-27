"""Channel engine: inbound media, quoted replies and media failure replies."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import core.channels._conversation_content as content_module
from core.attachments import AttachmentTooLargeError, AttachmentTypeNotAllowedError
from core.channels.adapter import QuotedMessageFacts
from core.chat import MessageSender
from core.chat.content_blocks import ContentBlock, MediaBlock, TextBlock
from core.runs import RunKind
from core.sessions import SessionAddress

from .engine_test_support import (
    CHANNEL_REPLY_SURFACE,
    SESSION_ID,
    FakeTransport,
    assert_member_trigger,
    drain,
    make_completed_run,
    make_conversation,
    make_engine,
)

_BLOCK = MediaBlock(type="media", attachment_id="att-1", filename="a.png", media_type="image/png")
_BUILD_ERRORS: dict[str, Exception] = {
    "unsupported": AttachmentTypeNotAllowedError("nope"),
    "too-large": AttachmentTooLargeError("too big"),
    "too-large-again": AttachmentTooLargeError("still too big"),
    "broken": RuntimeError("download failed"),
}


async def _build_media(raw_message: Any) -> list[ContentBlock]:
    if raw_message in _BUILD_ERRORS:
        raise _BUILD_ERRORS[raw_message]
    return [_BLOCK]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("messages", "replies", "triggered"),
    [
        (
            ("ok", "unsupported", "too-large", "too-large-again", "broken"),
            [
                content_module._UNSUPPORTED_FILE_REPLY,
                content_module._FILE_TOO_LARGE_REPLY,
                content_module._MEDIA_FAILED_REPLY,
                "ok",
            ],
            True,
        ),
        (
            ("broken", "too-large"),
            [content_module._MEDIA_FAILED_REPLY, content_module._FILE_TOO_LARGE_REPLY],
            False,
        ),
    ],
    ids=["one-item-survives", "every-item-fails"],
)
async def test_failed_media_items_reply_once_per_reason_and_keep_their_siblings(
    tmp_path: Path, messages: tuple[str, ...], replies: list[str], triggered: bool
) -> None:
    transport = FakeTransport(media_builder=_build_media)
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, _sessions, _trigger, _transport = make_engine(
        tmp_path, trigger_run=trigger_mock, transport=transport
    )

    await engine.handle_inbound_media(make_conversation(), messages)
    await drain(engine, 12345)

    assert transport.sent_texts == replies
    if triggered:
        trigger_mock.assert_awaited_once()
        assert trigger_mock.await_args is not None
        assert trigger_mock.await_args.args[1] == [_BLOCK]
    else:
        trigger_mock.assert_not_awaited()
    await engine.stop()


@pytest.mark.asyncio
async def test_media_companion_text_precedes_built_media_blocks(tmp_path: Path) -> None:
    transport = FakeTransport(media_builder=AsyncMock(return_value=[_BLOCK]))
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
        [TextBlock(type="text", text="Please edit this"), _BLOCK],
        SESSION_ID,
        sender=None,
        reply_surface=CHANNEL_REPLY_SURFACE,
        run_kind=RunKind.CHANNEL,
    )
    await engine.stop()


@pytest.mark.asyncio
async def test_a_wake_word_caption_triggers_group_media_with_its_sender(tmp_path: Path) -> None:
    transport = FakeTransport(media_builder=AsyncMock(return_value=[_BLOCK]))
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, _sessions, _trigger, _transport = make_engine(
        tmp_path,
        trigger_run=trigger_mock,
        transport=transport,
        mention_patterns=[r"\bvbot\b"],
    )

    # One captioned item of an album addresses the whole album.
    await engine.handle_inbound_media(
        make_conversation(kind="group", user_display_name="Alice"),
        (SimpleNamespace(caption=None), SimpleNamespace(caption="vbot look at this")),
    )
    await drain(engine, 12345)

    assert_member_trigger(
        trigger_mock,
        "assistant",
        [_BLOCK, _BLOCK],
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
    engine, _sessions, trigger_mock, _transport = make_engine(tmp_path, transport=transport)

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
@pytest.mark.parametrize("failed_download", [False, True])
async def test_unavailable_quoted_message_keeps_triggering_question(
    tmp_path: Path, failed_download: bool
) -> None:
    transport = FakeTransport(
        quoted_builder=AsyncMock(
            side_effect=RuntimeError("download failed") if failed_download else None,
            return_value=QuotedMessageFacts(
                user_id=None,
                user_display_name=None,
                content=None,
            ),
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
    assert transport.sent_texts == ["ok"]
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
    assert not chat_sessions.exists(
        SessionAddress(project_id=None, agent_id="assistant", session_id=SESSION_ID)
    )
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
        for message in chat_sessions.get(
            SessionAddress(project_id=None, agent_id="assistant", session_id=SESSION_ID)
        ).load()
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
