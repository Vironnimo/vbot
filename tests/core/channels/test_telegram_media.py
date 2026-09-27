"""Telegram inbound media: attachments, downloads, quoted media, albums and forwarding comments."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from telegram.error import NetworkError

import core.channels._telegram_inbound as telegram_inbound
from core.attachments import AttachmentStore
from core.chat import MessageSender
from core.chat.content_blocks import FileBlock, MediaBlock, TextBlock

from .engine_test_support import make_completed_run
from .telegram_test_support import (
    PNG_BYTES,
    GatedAccessRegistry,
    ManualClock,
    drain_chat_queue,
    make_adapter,
    make_media_update,
    make_update,
    photo,
    sent_texts,
    telegram_file,
)

pytestmark = pytest.mark.usefixtures("current_format_data_directory")

_GROUP_SESSION = "ch-tg-assistant--10001"
_OGG_BYTES = b"OggS\x00\x02opus"
_MP4_BYTES = b"\x00\x00\x00\x18ftypisom"
_ALBUM_WINDOW = telegram_inbound._ALBUM_FLUSH_SECONDS
_SETTLE_WINDOW = telegram_inbound._FORWARD_COMMENT_SETTLE_SECONDS


def _media(file_unique_id: str = "vu-1", **fields: Any) -> SimpleNamespace:
    return SimpleNamespace(file_id="media-1", file_unique_id=file_unique_id, **fields)


def _run_contents(trigger: AsyncMock) -> list[Any]:
    return [awaited.args[1] for awaited in trigger.await_args_list]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("media", "payload", "file_id", "block_type", "media_type", "filename"),
    [
        (
            # Telegram lists every size of a photo; the largest (last) is fetched.
            {
                "photo": [
                    SimpleNamespace(file_id="photo-small", file_unique_id="small"),
                    SimpleNamespace(file_id="photo-large", file_unique_id="uniq-1"),
                ]
            },
            PNG_BYTES,
            "photo-large",
            MediaBlock,
            "image/png",
            "telegram-photo-uniq-1.jpg",
        ),
        (
            {"voice": _media()},
            _OGG_BYTES,
            "media-1",
            MediaBlock,
            "audio/ogg",
            "telegram-voice-vu-1.ogg",
        ),
        (
            {"audio": _media()},
            b"ID3\x04\x00mp3",
            "media-1",
            MediaBlock,
            "audio/mpeg",
            "telegram-audio-vu-1.mp3",
        ),
        (
            {"video": _media()},
            _MP4_BYTES,
            "media-1",
            MediaBlock,
            "video/mp4",
            "telegram-video-vu-1.mp4",
        ),
        (
            {"video_note": _media()},
            _MP4_BYTES,
            "media-1",
            MediaBlock,
            "video/mp4",
            "telegram-video-note-vu-1.mp4",
        ),
        (
            # Animations also carry a backward-compatible document; the animation wins.
            {
                "animation": _media(),
                "document": SimpleNamespace(
                    file_id="compat-document", file_unique_id="cd", file_name="animation.mp4"
                ),
            },
            _MP4_BYTES,
            "media-1",
            MediaBlock,
            "video/mp4",
            "telegram-animation-vu-1.mp4",
        ),
        (
            {"document": _media(file_name="report.pdf")},
            b"%PDF-1.7\n",
            "media-1",
            FileBlock,
            "application/pdf",
            "report.pdf",
        ),
        (
            # A document is classified by its content: an MP3 sent as a file is audio.
            {"document": _media(file_name="song.mp3")},
            b"ID3\x04\x00\x00\x00\x00\x00\x00",
            "media-1",
            MediaBlock,
            "audio/mpeg",
            "song.mp3",
        ),
    ],
    ids=[
        "photo",
        "voice",
        "audio",
        "video",
        "video-note",
        "animation",
        "pdf-document",
        "audio-document",
    ],
)
async def test_inbound_media_reaches_the_agent_as_a_stored_attachment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    media: dict[str, Any],
    payload: bytes,
    file_id: str,
    block_type: type[MediaBlock | FileBlock],
    media_type: str,
    filename: str,
) -> None:
    store = AttachmentStore(tmp_path)
    trigger = AsyncMock(return_value=make_completed_run(output_text="ok"))
    adapter, _sessions, _trigger, bot = make_adapter(
        tmp_path, monkeypatch, allowed_chat_ids=[12345], trigger_run=trigger, attachment_store=store
    )
    bot.get_file.return_value = telegram_file(payload)

    await adapter._handle_inbound_media(make_media_update(caption=" check this ", **media), None)
    await drain_chat_queue(adapter, 12345)

    bot.get_file.assert_awaited_once_with(file_id)
    [[caption, block]] = _run_contents(trigger)
    assert caption == TextBlock(type="text", text="check this")
    assert isinstance(block, block_type)
    assert (block.media_type, block.filename) == (media_type, filename)
    assert store.get(block.attachment_id).media_type == media_type
    await adapter.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row", "media", "get_file_count", "reply"),
    [
        ("transient-network-error", {"voice": _media()}, 2, "ok"),
        (
            "persistent-network-error",
            {"voice": _media()},
            4,
            "Sorry, the messaging platform couldn't download the attached file after "
            "several attempts. Please resend it.",
        ),
        ("oversized", {"photo": photo("u1")}, 1, "Sorry, this file is too large to process."),
        (
            "unsupported-type",
            {"document": _media(file_name="archive.zip")},
            1,
            "Sorry, this file type isn't supported yet.",
        ),
    ],
    ids=["transient-network-error", "persistent-network-error", "oversized", "unsupported-type"],
)
async def test_a_failed_download_is_retried_or_explained_to_the_sender(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    row: str,
    media: dict[str, Any],
    get_file_count: int,
    reply: str,
) -> None:
    monkeypatch.setattr("core.utils.retry._sleep", AsyncMock())
    trigger = AsyncMock(return_value=make_completed_run(output_text="ok"))
    adapter, _sessions, _trigger, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger,
        attachment_store=AttachmentStore(tmp_path, max_size_bytes=64),
    )
    bot.get_file.side_effect = {
        "transient-network-error": [NetworkError("timeout"), telegram_file(_OGG_BYTES)],
        "persistent-network-error": NetworkError("timeout"),
        # Telegram reports the size first; an oversized body is never fetched.
        "oversized": [
            SimpleNamespace(
                file_size=1_000_000,
                download_as_bytearray=AsyncMock(side_effect=AssertionError("body fetched")),
            )
        ],
        # No known signature and not text: the store refuses the type.
        "unsupported-type": [telegram_file(b"\xff\xfe\xfdbinary")],
    }[row]

    await adapter._handle_inbound_media(make_media_update(**media), None)
    await drain_chat_queue(adapter, 12345)

    assert bot.get_file.await_count == get_file_count
    assert trigger.await_count == (1 if reply == "ok" else 0)
    assert sent_texts(bot) == [reply]
    await adapter.stop()


@pytest.mark.asyncio
async def test_an_addressed_reply_to_a_photo_brings_the_quoted_photo_along(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trigger = AsyncMock(
        return_value=make_completed_run(output_text="ok", session_id=_GROUP_SESSION)
    )
    adapter, _sessions, _trigger, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[-10001],
        admin_user_ids=["50"],
        bot_username="MyBot",
        bot_id=999,
        trigger_run=trigger,
        attachment_store=AttachmentStore(tmp_path),
    )
    quoted_file = telegram_file(PNG_BYTES, file_size=12)
    bot.get_file.return_value = quoted_file
    quoted_photo = SimpleNamespace(
        from_user=SimpleNamespace(id=77, full_name="Juan", username="juan"),
        text=None,
        caption="Sunset",
        photo=photo("juan-1"),
    )

    await adapter._handle_inbound_message(
        make_update(
            chat_id=-10001,
            text="@MyBot, what do you think?",
            message_id=701,
            reply_to_message=quoted_photo,
        ),
        None,
    )
    await drain_chat_queue(adapter, -10001)

    # The quoted photo is only downloaded because the reply addresses the bot.
    bot.get_file.assert_awaited_once_with("photo-juan-1")
    quoted_file.download_as_bytearray.assert_awaited_once()
    assert trigger.await_args is not None
    blocks = trigger.await_args.args[1]
    assert blocks[:3] == [
        TextBlock(type="text", text="@MyBot, what do you think?"),
        TextBlock(type="text", text="[quoted-message] [Juan|77|member]:"),
        TextBlock(type="text", text="Sunset"),
    ]
    assert [type(block) for block in blocks[3:]] == [MediaBlock]
    assert trigger.await_args.kwargs["sender"] == MessageSender(
        id="50", display_name="50", role="admin"
    )
    assert "tool_restriction" not in trigger.await_args.kwargs
    await adapter.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("forward_origin", "contents"),
    [
        (SimpleNamespace(type="user"), [["Please edit this image", "telegram-photo-uniq-1.jpg"]]),
        # A photo the sender took themselves is not the forward the comment announced.
        (None, ["Please edit this image", ["telegram-photo-uniq-1.jpg"]]),
    ],
    ids=["forwarded-photo", "own-photo"],
)
async def test_a_forwarding_comment_joins_the_forwarded_media_in_one_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forward_origin: object | None,
    contents: list[Any],
) -> None:
    clock = ManualClock()
    trigger = AsyncMock(side_effect=lambda *_args, **_kwargs: make_completed_run(output_text="ok"))
    adapter, _sessions, _trigger, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger,
        attachment_store=AttachmentStore(tmp_path),
        clock=clock,
    )
    bot.get_file.return_value = telegram_file(PNG_BYTES)

    await adapter._handle_inbound_message(
        make_update(text="Please edit this image", message_id=700), None
    )
    # The photo follows inside the settle window and decides the comment's fate;
    # the window itself never elapses.
    await clock.advance(_SETTLE_WINDOW * 0.9)
    await adapter._handle_inbound_media(
        make_media_update(message_id=701, forward_origin=forward_origin, photo=photo("uniq-1")),
        None,
    )
    await drain_chat_queue(adapter, 12345)

    def describe(content: Any) -> Any:
        if isinstance(content, str):
            return content
        return [block.text if isinstance(block, TextBlock) else block.filename for block in content]

    assert [describe(content) for content in _run_contents(trigger)] == contents
    await adapter.stop()


@pytest.mark.asyncio
async def test_an_album_becomes_one_run_once_its_items_stop_arriving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = ManualClock()
    trigger = AsyncMock(
        return_value=make_completed_run(output_text="ok", session_id=_GROUP_SESSION)
    )
    adapter, _sessions, _trigger, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[-10001],
        bot_username="MyBot",
        bot_id=999,
        trigger_run=trigger,
        attachment_store=AttachmentStore(tmp_path),
        clock=clock,
    )
    bot.get_file.side_effect = [telegram_file(PNG_BYTES + bytes([index])) for index in range(3)]

    # Items arrive inside the window but span more than one window in total, and
    # only the middle item addresses the bot.
    for index, caption in enumerate([None, "@MyBot what do you see?", None]):
        await adapter._handle_inbound_media(
            make_media_update(
                chat_id=-10001,
                user_full_name="Alice Example",
                media_group_id="album-1",
                caption=caption,
                photo=photo(f"uniq-{index}"),
            ),
            None,
        )
        await clock.advance(_ALBUM_WINDOW * 0.6)
    trigger.assert_not_awaited()
    await clock.advance(_ALBUM_WINDOW)
    await drain_chat_queue(adapter, -10001)

    trigger.assert_awaited_once()
    assert trigger.await_args is not None
    assert [type(block) for block in trigger.await_args.args[1]] == [
        MediaBlock,
        TextBlock,
        MediaBlock,
        MediaBlock,
    ]
    assert trigger.await_args.kwargs["sender"] == MessageSender(
        id="50", display_name="Alice Example"
    )
    await adapter.stop()


@pytest.mark.asyncio
async def test_a_late_album_item_does_not_cancel_the_batch_already_dispatching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = ManualClock()
    registry = GatedAccessRegistry()
    trigger = AsyncMock(
        side_effect=lambda *_args, **_kwargs: make_completed_run(
            output_text="ok", session_id=_GROUP_SESSION
        )
    )
    adapter, _sessions, _trigger, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[-10001],
        response_mode="all",
        trigger_run=trigger,
        attachment_store=AttachmentStore(tmp_path),
        access_registry=registry,
        clock=clock,
    )
    bot.get_file.side_effect = [telegram_file(PNG_BYTES + bytes([index])) for index in range(2)]

    def album_item(index: int) -> SimpleNamespace:
        return make_media_update(
            chat_id=-10001, media_group_id="album-1", photo=photo(f"uniq-{index}")
        )

    await adapter._handle_inbound_media(album_item(0), None)
    await clock.advance(_ALBUM_WINDOW)
    await registry.entered.wait()
    await adapter._handle_inbound_media(album_item(1), None)
    await clock.advance(_ALBUM_WINDOW)
    registry.release.set()
    await drain_chat_queue(adapter, -10001)

    assert [len(content) for content in _run_contents(trigger)] == [1, 1]
    await adapter.stop()


@pytest.mark.asyncio
async def test_stop_cancels_a_forwarding_comment_already_dispatching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = ManualClock()
    registry = GatedAccessRegistry()
    adapter, _sessions, trigger, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[-10001],
        response_mode="all",
        access_registry=registry,
        clock=clock,
    )

    await adapter._handle_inbound_message(
        make_update(chat_id=-10001, text="question", message_id=700), None
    )
    await clock.advance(_SETTLE_WINDOW)
    await registry.entered.wait()
    await adapter.stop()

    # The dispatch had left the settle window, and stop still owned and ended it.
    assert registry.cancelled.is_set()
    trigger.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("update", "window", "log_text"),
    [
        (
            make_media_update(chat_id=-10001, media_group_id="album-1", photo=photo("uniq-1")),
            _ALBUM_WINDOW,
            "Telegram album flush failed (channel=tg-assistant)",
        ),
        (
            make_update(chat_id=-10001, text="question", message_id=700),
            _SETTLE_WINDOW,
            "Telegram forward-comment flush failed (channel=tg-assistant)",
        ),
    ],
    ids=["album", "forwarding-comment"],
)
async def test_a_failed_delayed_dispatch_is_logged_with_its_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    update: SimpleNamespace,
    window: float,
    log_text: str,
) -> None:
    # Losing buffered inbound messages is not expected; the warning must carry the
    # traceback, but never the album id or the chat id.
    clock = ManualClock()
    adapter, _sessions, trigger, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[-10001],
        response_mode="all",
        attachment_store=AttachmentStore(tmp_path),
        access_registry=GatedAccessRegistry(error=RuntimeError("registry unavailable")),
        clock=clock,
    )
    caplog.set_level(logging.WARNING, logger="vbot.channels.telegram")
    handler = (
        adapter._handle_inbound_media
        if hasattr(update.effective_message, "photo")
        else adapter._handle_inbound_message
    )

    await handler(update, None)
    await clock.advance(window)
    await drain_chat_queue(adapter, -10001)

    [record] = [record for record in caplog.records if "flush failed" in record.getMessage()]
    message = record.getMessage()
    assert message.startswith(log_text)
    assert record.levelno == logging.WARNING
    assert record.exc_info is not None
    assert "album-1" not in message
    assert "-10001" not in message
    trigger.assert_not_awaited()
    await adapter.stop()
