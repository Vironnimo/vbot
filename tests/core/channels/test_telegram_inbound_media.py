"""Tests for TelegramChannelAdapter behavior."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import core.channels.engine as engine_module
import core.channels.telegram as telegram_module
from core.attachments import AttachmentStore
from core.channels.adapter import (
    ConversationFacts,
)
from core.chat import MessageSender
from core.chat.content_blocks import FileBlock, MediaBlock, TextBlock
from tests.core.channels.telegram_test_support import (
    drain_chat_queue,
    make_adapter,
    make_completed_run,
    make_document_update,
    make_group_update,
    make_photo_update,
    make_update,
)


@pytest.mark.asyncio
async def test_inbound_photo_stores_attachment_and_triggers_media_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment_store = AttachmentStore(tmp_path)
    session_id = "ch-tg-assistant-12345"
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="ok")
    )
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
        attachment_store=attachment_store,
    )

    bot.get_file.return_value = SimpleNamespace(
        download_as_bytearray=AsyncMock(return_value=bytearray(b"\x89PNG\r\n\x1a\nIMG"))
    )

    await adapter._handle_inbound_media(
        make_photo_update(
            chat_id=12345,
            user_id=50,
            file_id="photo-1",
            file_unique_id="uniq-1",
        ),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    trigger_mock.assert_awaited_once()
    await_args = trigger_mock.await_args
    assert await_args is not None
    trigger_args = await_args.args
    assert trigger_args[0] == "assistant"
    assert trigger_args[2] == session_id
    blocks = trigger_args[1]
    assert isinstance(blocks, list)
    assert len(blocks) == 1
    assert isinstance(blocks[0], MediaBlock)
    stored = attachment_store.get(blocks[0].attachment_id)
    assert stored.media_type == "image/png"
    await adapter.stop()


@pytest.mark.asyncio
async def test_addressed_group_reply_downloads_quoted_photo_on_demand(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment_store = AttachmentStore(tmp_path)
    session_id = "ch-tg-assistant--10001"
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="ok")
    )
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[-10001],
        admin_user_ids=["50"],
        bot_username="MyBot",
        bot_id=999,
        trigger_run=trigger_mock,
        attachment_store=attachment_store,
    )
    telegram_file = SimpleNamespace(
        file_size=12,
        download_as_bytearray=AsyncMock(return_value=bytearray(b"\x89PNG\r\n\x1a\nIMG")),
    )
    bot.get_file.return_value = telegram_file
    replied_photo = SimpleNamespace(
        from_user=SimpleNamespace(id=77, full_name="Juan", username="juan"),
        sender_chat=None,
        text=None,
        caption="Sunset",
        photo=[SimpleNamespace(file_id="photo-juan", file_unique_id="juan-1")],
        document=None,
        voice=None,
        audio=None,
        video=None,
        video_note=None,
        animation=None,
    )

    await adapter._handle_inbound_message(
        make_group_update(
            user_id=50,
            text="@MyBot, what do you think?",
            message_id=701,
            reply_to_message=replied_photo,
        ),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, -10001)

    bot.get_file.assert_awaited_once_with("photo-juan")
    telegram_file.download_as_bytearray.assert_awaited_once()
    trigger_mock.assert_awaited_once()
    awaited = trigger_mock.await_args
    assert awaited is not None
    blocks = awaited.args[1]
    assert isinstance(blocks, list)
    assert blocks[:3] == [
        TextBlock(type="text", text="@MyBot, what do you think?"),
        TextBlock(type="text", text="[quoted-message] [Juan|77|member]:"),
        TextBlock(type="text", text="Sunset"),
    ]
    assert isinstance(blocks[3], MediaBlock)
    assert awaited.kwargs["sender"] == MessageSender(
        id="50",
        display_name="50",
        role="admin",
    )
    assert "tool_restriction" not in awaited.kwargs
    await adapter.stop()


@pytest.mark.asyncio
async def test_forward_comment_and_photo_trigger_one_combined_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment_store = AttachmentStore(tmp_path)
    session_id = "ch-tg-assistant-12345"
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="ok")
    )
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
        attachment_store=attachment_store,
    )
    bot.get_file.return_value = SimpleNamespace(
        download_as_bytearray=AsyncMock(return_value=bytearray(b"\x89PNG\r\n\x1a\nIMG"))
    )

    await adapter._handle_inbound_message(
        make_update(
            chat_id=12345,
            user_id=50,
            text="Please edit this image",
            message_id=700,
        ),
        SimpleNamespace(),
    )
    trigger_mock.assert_not_awaited()

    await adapter._handle_inbound_media(
        make_photo_update(
            chat_id=12345,
            user_id=50,
            file_id="photo-1",
            file_unique_id="uniq-1",
            message_id=701,
            forward_origin=SimpleNamespace(type="user"),
        ),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    trigger_mock.assert_awaited_once()
    await_args = trigger_mock.await_args
    assert await_args is not None
    blocks = await_args.args[1]
    assert isinstance(blocks, list)
    assert len(blocks) == 2
    assert blocks[0] == TextBlock(type="text", text="Please edit this image")
    assert isinstance(blocks[1], MediaBlock)
    assert blocks[1].filename == "telegram-photo-uniq-1.jpg"
    assert adapter._pending_forward_comments == {}
    await adapter.stop()


@pytest.mark.asyncio
async def test_possible_forward_comment_flushes_as_plain_text_after_settle_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(telegram_module, "_FORWARD_COMMENT_SETTLE_SECONDS", 0)
    trigger_mock = AsyncMock(
        return_value=make_completed_run(
            session_id="ch-tg-assistant-12345",
            output_text="ok",
        )
    )
    adapter, _chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
    )

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="hello", message_id=700),
        SimpleNamespace(),
    )
    await asyncio.gather(*adapter._forward_comment_tasks.values())
    await drain_chat_queue(adapter, 12345)

    trigger_mock.assert_awaited_once()
    assert trigger_mock.await_args is not None
    assert trigger_mock.await_args.args[1] == "hello"
    await adapter.stop()


@pytest.mark.asyncio
async def test_inbound_pdf_document_triggers_file_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment_store = AttachmentStore(tmp_path)
    session_id = "ch-tg-assistant-12345"
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="ok")
    )
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
        attachment_store=attachment_store,
    )

    bot.get_file.return_value = SimpleNamespace(
        download_as_bytearray=AsyncMock(return_value=bytearray(b"%PDF-1.7\n"))
    )

    await adapter._handle_inbound_media(
        make_document_update(
            chat_id=12345,
            user_id=50,
            file_id="doc-1",
            file_unique_id="docuniq-1",
            file_name="report.pdf",
        ),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    trigger_mock.assert_awaited_once()
    await_args = trigger_mock.await_args
    assert await_args is not None
    blocks = await_args.args[1]
    assert isinstance(blocks, list)
    assert len(blocks) == 1
    assert isinstance(blocks[0], FileBlock)
    assert blocks[0].media_type == "application/pdf"
    await adapter.stop()


@pytest.mark.asyncio
async def test_inbound_text_document_triggers_file_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment_store = AttachmentStore(tmp_path)
    session_id = "ch-tg-assistant-12345"
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="ok")
    )
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
        attachment_store=attachment_store,
    )

    bot.get_file.return_value = SimpleNamespace(
        download_as_bytearray=AsyncMock(return_value=bytearray(b"hello from text file"))
    )

    await adapter._handle_inbound_media(
        make_document_update(
            chat_id=12345,
            user_id=50,
            file_id="doc-2",
            file_unique_id="docuniq-2",
            file_name="notes.txt",
        ),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    trigger_mock.assert_awaited_once()
    await_args = trigger_mock.await_args
    assert await_args is not None
    blocks = await_args.args[1]
    assert isinstance(blocks, list)
    assert len(blocks) == 1
    assert isinstance(blocks[0], FileBlock)
    assert blocks[0].filename == "notes.txt"
    assert blocks[0].media_type == "text/plain"
    await adapter.stop()


@pytest.mark.asyncio
async def test_inbound_audio_document_triggers_media_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment_store = AttachmentStore(tmp_path)
    session_id = "ch-tg-assistant-12345"
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="ok")
    )
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
        attachment_store=attachment_store,
    )

    # An MP3 carries an ID3 header, so the store sniffs it as audio/mpeg even when Telegram
    # delivers it as a generic document instead of an audio message.
    bot.get_file.return_value = SimpleNamespace(
        download_as_bytearray=AsyncMock(return_value=bytearray(b"ID3\x04\x00\x00\x00\x00\x00\x00"))
    )

    await adapter._handle_inbound_media(
        make_document_update(
            chat_id=12345,
            user_id=50,
            file_id="doc-audio",
            file_unique_id="docuniq-audio",
            file_name="song.mp3",
        ),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    trigger_mock.assert_awaited_once()
    await_args = trigger_mock.await_args
    assert await_args is not None
    blocks = await_args.args[1]
    assert isinstance(blocks, list)
    assert len(blocks) == 1
    assert isinstance(blocks[0], MediaBlock)
    assert blocks[0].media_type == "audio/mpeg"
    await adapter.stop()


@pytest.mark.asyncio
async def test_inbound_video_document_triggers_media_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment_store = AttachmentStore(tmp_path)
    session_id = "ch-tg-assistant-12345"
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="ok")
    )
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
        attachment_store=attachment_store,
    )

    # An MP4 carries an ftyp box, so the store sniffs it as video/mp4 even when Telegram
    # delivers it as a generic document instead of a video message.
    bot.get_file.return_value = SimpleNamespace(
        download_as_bytearray=AsyncMock(
            return_value=bytearray(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00")
        )
    )

    await adapter._handle_inbound_media(
        make_document_update(
            chat_id=12345,
            user_id=50,
            file_id="doc-video",
            file_unique_id="docuniq-video",
            file_name="clip.mp4",
        ),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    trigger_mock.assert_awaited_once()
    await_args = trigger_mock.await_args
    assert await_args is not None
    blocks = await_args.args[1]
    assert isinstance(blocks, list)
    assert len(blocks) == 1
    assert isinstance(blocks[0], MediaBlock)
    assert blocks[0].media_type == "video/mp4"
    await adapter.stop()


@pytest.mark.asyncio
async def test_disallowed_document_type_replies_instead_of_silent_drop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment_store = AttachmentStore(tmp_path)
    trigger_mock = AsyncMock()
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
        attachment_store=attachment_store,
    )

    # Non-UTF8 binary without a known signature sniffs to octet-stream -> rejected.
    bot.get_file.return_value = SimpleNamespace(
        download_as_bytearray=AsyncMock(return_value=bytearray(b"\xff\xfe\xfdbinary"))
    )

    await adapter._handle_inbound_media(
        make_document_update(
            chat_id=12345,
            user_id=50,
            file_id="doc-3",
            file_unique_id="docuniq-3",
            file_name="archive.zip",
        ),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    trigger_mock.assert_not_awaited()
    bot.send_message.assert_awaited_once_with(
        chat_id=12345,
        text="Sorry, this file type isn't supported yet.",
    )
    await adapter.stop()


@pytest.mark.asyncio
async def test_album_with_one_failing_item_keeps_siblings_and_reports_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment_store = AttachmentStore(tmp_path)
    session_id = "ch-tg-assistant-12345"
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="ok")
    )
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
        attachment_store=attachment_store,
    )

    bot.get_file.side_effect = [
        SimpleNamespace(
            download_as_bytearray=AsyncMock(return_value=bytearray(b"\x89PNG\r\n\x1a\nok"))
        ),
        SimpleNamespace(
            download_as_bytearray=AsyncMock(side_effect=RuntimeError("download failed"))
        ),
    ]

    queued = engine_module._QueuedInboundMedia(
        conversation=ConversationFacts(
            platform="telegram",
            channel_id="tg-assistant",
            chat_id="12345",
            user_id="50",
        ),
        messages=(
            make_photo_update(
                chat_id=12345,
                user_id=50,
                file_id="photo-ok",
                file_unique_id="uniq-ok",
            ).effective_message,
            make_photo_update(
                chat_id=12345,
                user_id=50,
                file_id="photo-broken",
                file_unique_id="uniq-broken",
            ).effective_message,
        ),
    )

    await adapter._engine._process_queued_media(queued)

    sent_texts = [call.kwargs["text"] for call in bot.send_message.await_args_list]
    assert sent_texts == [
        "Sorry, I couldn't process the attached file. Please try again.",
        "ok",
    ]
    trigger_mock.assert_awaited_once()
    await_args = trigger_mock.await_args
    assert await_args is not None
    blocks = await_args.args[1]
    assert isinstance(blocks, list)
    assert len(blocks) == 1
    assert isinstance(blocks[0], MediaBlock)
    await adapter.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attribute_name", "payload", "expected_media_type", "expected_filename"),
    [
        ("voice", b"OggS\x00\x02opus", "audio/ogg", "telegram-voice-vu-1.ogg"),
        ("audio", b"ID3\x04\x00mp3", "audio/mpeg", "telegram-audio-vu-1.mp3"),
        ("video", b"\x00\x00\x00\x18ftypisom", "video/mp4", "telegram-video-vu-1.mp4"),
        (
            "video_note",
            b"\x00\x00\x00\x18ftypisom",
            "video/mp4",
            "telegram-video-note-vu-1.mp4",
        ),
        (
            "animation",
            b"\x00\x00\x00\x18ftypisom",
            "video/mp4",
            "telegram-animation-vu-1.mp4",
        ),
    ],
)
async def test_inbound_audio_video_message_triggers_media_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attribute_name: str,
    payload: bytes,
    expected_media_type: str,
    expected_filename: str,
) -> None:
    attachment_store = AttachmentStore(tmp_path)
    session_id = "ch-tg-assistant-12345"
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="ok")
    )
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
        attachment_store=attachment_store,
    )

    bot.get_file.return_value = SimpleNamespace(
        download_as_bytearray=AsyncMock(return_value=bytearray(payload))
    )

    media_object = SimpleNamespace(file_id="media-1", file_unique_id="vu-1", file_name=None)
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=12345),
        effective_user=SimpleNamespace(id=50),
        effective_message=SimpleNamespace(
            text=None,
            caption="check this",
            photo=None,
            document=None,
            media_group_id=None,
            message_thread_id=None,
            **{attribute_name: media_object},
        ),
    )

    await adapter._handle_inbound_media(update, SimpleNamespace())
    await drain_chat_queue(adapter, 12345)

    trigger_mock.assert_awaited_once()
    await_args = trigger_mock.await_args
    assert await_args is not None
    blocks = await_args.args[1]
    assert isinstance(blocks, list)
    assert len(blocks) == 2
    assert isinstance(blocks[0], TextBlock)
    assert blocks[0].text == "check this"
    assert isinstance(blocks[1], MediaBlock)
    assert blocks[1].media_type == expected_media_type
    assert blocks[1].filename == expected_filename
    await adapter.stop()


@pytest.mark.asyncio
async def test_unsupported_message_type_replies_for_allowed_chat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _chat_sessions, trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )
    voice_update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=12345),
        effective_user=SimpleNamespace(id=50),
        effective_message=SimpleNamespace(text=None, message_thread_id=None),
    )

    await adapter._handle_unsupported_message_type(voice_update, SimpleNamespace())

    trigger_mock.assert_not_awaited()
    bot.send_message.assert_awaited_once_with(
        chat_id=12345,
        text="Sorry, this message type isn't supported yet.",
    )
    await adapter.stop()


@pytest.mark.asyncio
async def test_album_messages_are_buffered_into_single_trigger_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment_store = AttachmentStore(tmp_path)
    session_id = "ch-tg-assistant-12345"
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="ok")
    )
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
        attachment_store=attachment_store,
    )

    bot.get_file.side_effect = [
        SimpleNamespace(
            download_as_bytearray=AsyncMock(return_value=bytearray(b"\x89PNG\r\n\x1a\nA"))
        ),
        SimpleNamespace(
            download_as_bytearray=AsyncMock(return_value=bytearray(b"\x89PNG\r\n\x1a\nB"))
        ),
    ]

    await adapter._handle_inbound_media(
        make_photo_update(
            chat_id=12345,
            user_id=50,
            file_id="photo-a",
            file_unique_id="uniq-a",
            media_group_id="album-1",
        ),
        SimpleNamespace(),
    )
    await adapter._handle_inbound_media(
        make_photo_update(
            chat_id=12345,
            user_id=50,
            file_id="photo-b",
            file_unique_id="uniq-b",
            media_group_id="album-1",
        ),
        SimpleNamespace(),
    )

    await asyncio.sleep(0.6)
    await drain_chat_queue(adapter, 12345)

    trigger_mock.assert_awaited_once()
    await_args = trigger_mock.await_args
    assert await_args is not None
    blocks = await_args.args[1]
    assert isinstance(blocks, list)
    assert len(blocks) == 2
    assert isinstance(blocks[0], MediaBlock)
    assert isinstance(blocks[1], MediaBlock)
    await adapter.stop()


@pytest.mark.asyncio
async def test_group_album_carries_sender_into_trigger_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment_store = AttachmentStore(tmp_path)
    session_id = "ch-tg-assistant--10001"
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="ok")
    )
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[-10001],
        response_mode="all",
        trigger_run=trigger_mock,
        attachment_store=attachment_store,
    )

    bot.get_file.side_effect = [
        SimpleNamespace(
            download_as_bytearray=AsyncMock(return_value=bytearray(b"\x89PNG\r\n\x1a\nA"))
        ),
        SimpleNamespace(
            download_as_bytearray=AsyncMock(return_value=bytearray(b"\x89PNG\r\n\x1a\nB"))
        ),
    ]

    await adapter._handle_inbound_media(
        make_photo_update(
            chat_id=-10001,
            user_id=50,
            file_id="photo-a",
            file_unique_id="uniq-a",
            media_group_id="album-1",
            user_full_name="Alice Example",
        ),
        SimpleNamespace(),
    )
    await adapter._handle_inbound_media(
        make_photo_update(
            chat_id=-10001,
            user_id=50,
            file_id="photo-b",
            file_unique_id="uniq-b",
            media_group_id="album-1",
            user_full_name="Alice Example",
        ),
        SimpleNamespace(),
    )

    await asyncio.sleep(0.6)
    await drain_chat_queue(adapter, -10001)

    trigger_mock.assert_awaited_once()
    await_args = trigger_mock.await_args
    assert await_args is not None
    assert await_args.kwargs["sender"] == MessageSender(id="50", display_name="Alice Example")
    await adapter.stop()


@pytest.mark.asyncio
async def test_album_flush_failure_log_carries_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Losing a whole inbound album is not "expected"; the done-callback warning must carry
    # the traceback so the dropped media is diagnosable beyond str(error).
    adapter, _chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )

    async def raise_flush_error() -> None:
        raise RuntimeError("album flush blew up")

    failed_task = asyncio.create_task(raise_flush_error())
    await asyncio.wait([failed_task])

    with caplog.at_level(logging.WARNING, logger="vbot.channels.telegram"):
        adapter._on_album_task_done("album-1", failed_task)

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert any(
        "album flush failed" in record.getMessage() and "album-1" in record.getMessage()
        for record in warnings
    )
    assert any(record.exc_info is not None for record in warnings)
    await adapter.stop()
