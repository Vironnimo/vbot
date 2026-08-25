"""Tests for TelegramChannelAdapter behavior."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from core.channels.adapter import (
    FileData,
)
from core.channels.channels import ChannelError
from core.channels.telegram import (
    TELEGRAM_CAPTION_LIMIT,
    split_telegram_message,
)
from tests.core.channels.telegram_test_support import (
    install_fake_telegram_media,
    make_adapter,
)


@pytest.mark.asyncio
async def test_send_single_image_file_uses_send_photo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_telegram_media(monkeypatch)
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )

    await adapter.send(
        "caption",
        "12345",
        files=[FileData(filename="image.png", media_type="image/png", data=b"img-bytes")],
    )

    bot.send_photo.assert_awaited_once()
    assert bot.send_photo.await_args.kwargs["chat_id"] == 12345
    assert bot.send_photo.await_args.kwargs["caption"] == "caption"
    bot.send_document.assert_not_awaited()
    bot.send_media_group.assert_not_awaited()
    await adapter.stop()


@pytest.mark.asyncio
async def test_send_mixed_single_image_and_document_sends_separate_messages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_telegram_media(monkeypatch)
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )

    await adapter.send(
        "batch caption",
        "12345",
        files=[
            FileData(filename="a.png", media_type="image/png", data=b"a"),
            FileData(filename="b.pdf", media_type="application/pdf", data=b"%PDF"),
        ],
    )

    bot.send_photo.assert_awaited_once()
    assert bot.send_photo.await_args.kwargs["chat_id"] == 12345
    assert bot.send_photo.await_args.kwargs["caption"] == "batch caption"

    bot.send_document.assert_awaited_once()
    assert bot.send_document.await_args.kwargs["chat_id"] == 12345
    assert "caption" not in bot.send_document.await_args.kwargs

    bot.send_media_group.assert_not_awaited()
    await adapter.stop()


@pytest.mark.asyncio
async def test_send_two_images_uses_single_homogeneous_media_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_telegram_media(monkeypatch)
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )

    await adapter.send(
        "batch caption",
        "12345",
        files=[
            FileData(filename="a.png", media_type="image/png", data=b"a"),
            FileData(filename="b.jpg", media_type="image/jpeg", data=b"b"),
        ],
    )

    bot.send_media_group.assert_awaited_once()
    media = bot.send_media_group.await_args.kwargs["media"]
    assert len(media) == 2
    assert {type(item).__name__ for item in media} == {"FakeInputMediaPhoto"}
    assert media[0].caption == "batch caption"
    assert media[1].caption is None
    bot.send_photo.assert_not_awaited()
    bot.send_document.assert_not_awaited()
    await adapter.stop()


@pytest.mark.asyncio
async def test_send_mixed_batches_caption_only_on_first_item_of_first_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_telegram_media(monkeypatch)
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )

    await adapter.send(
        "batch caption",
        "12345",
        files=[
            FileData(filename="a.png", media_type="image/png", data=b"a"),
            FileData(filename="b.jpg", media_type="image/jpeg", data=b"b"),
            FileData(filename="c.pdf", media_type="application/pdf", data=b"c"),
            FileData(filename="d.pdf", media_type="application/pdf", data=b"d"),
        ],
    )

    assert bot.send_media_group.await_count == 2
    first_batch = bot.send_media_group.await_args_list[0].kwargs["media"]
    second_batch = bot.send_media_group.await_args_list[1].kwargs["media"]

    assert {type(item).__name__ for item in first_batch} == {"FakeInputMediaPhoto"}
    assert {type(item).__name__ for item in second_batch} == {"FakeInputMediaDocument"}

    assert first_batch[0].caption == "batch caption"
    assert all(item.caption is None for item in first_batch[1:])
    assert all(item.caption is None for item in second_batch)

    bot.send_photo.assert_not_awaited()
    bot.send_document.assert_not_awaited()
    await adapter.stop()


def test_split_telegram_message_counts_emoji_as_utf16_units() -> None:
    # "😀" is one Python code point but two UTF-16 units, so eight of them fill a
    # max-4-unit chunk into pairs even though code-point slicing would have kept four.
    message = "😀" * 8

    chunks = split_telegram_message(message, max_chars=4)

    assert chunks == ["😀😀", "😀😀", "😀😀", "😀😀"]
    # No chunk exceeds the limit measured the way Telegram measures it.
    assert all(len(chunk.encode("utf-16-le")) // 2 <= 4 for chunk in chunks)
    assert "".join(chunks) == message


def test_split_telegram_message_does_not_split_inside_a_surrogate_pair() -> None:
    # An odd UTF-16 budget must round down rather than cut an emoji in half; every chunk
    # has to stay valid UTF-16 (a lone surrogate would raise on encode).
    message = "a😀b😀c"

    chunks = split_telegram_message(message, max_chars=3)

    assert "".join(chunks) == message
    for chunk in chunks:
        assert chunk == chunk.encode("utf-16-le").decode("utf-16-le")
        assert len(chunk.encode("utf-16-le")) // 2 <= 3


@pytest.mark.asyncio
async def test_send_long_caption_falls_back_to_standalone_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_telegram_media(monkeypatch)
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )

    long_message = "x" * (TELEGRAM_CAPTION_LIMIT + 1)
    await adapter.send(
        long_message,
        "12345",
        files=[FileData(filename="image.png", media_type="image/png", data=b"img-bytes")],
    )

    # The over-limit text is delivered as its own message, not as the file caption.
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["text"] == long_message
    bot.send_photo.assert_awaited_once()
    assert "caption" not in bot.send_photo.await_args.kwargs
    await adapter.stop()


@pytest.mark.asyncio
async def test_send_caption_within_limit_still_rides_on_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_telegram_media(monkeypatch)
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )

    caption = "x" * TELEGRAM_CAPTION_LIMIT
    await adapter.send(
        caption,
        "12345",
        files=[FileData(filename="image.png", media_type="image/png", data=b"img-bytes")],
    )

    bot.send_photo.assert_awaited_once()
    assert bot.send_photo.await_args.kwargs["caption"] == caption
    bot.send_message.assert_not_awaited()
    await adapter.stop()


@pytest.mark.asyncio
async def test_send_wraps_telegram_error_as_channel_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from telegram.error import BadRequest

    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )
    bot.send_message = AsyncMock(side_effect=BadRequest("Message caption is too long"))

    with pytest.raises(ChannelError) as excinfo:
        await adapter.send("hi", "12345")

    assert isinstance(excinfo.value.__cause__, BadRequest)
    await adapter.stop()


@pytest.mark.asyncio
async def test_boundary_marks_network_errors_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from telegram.error import NetworkError, TimedOut

    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )
    bot.send_message = AsyncMock(side_effect=NetworkError("connection reset"))

    with pytest.raises(ChannelError) as excinfo:
        await adapter.send("hi", "12345")

    assert excinfo.value.retryable is True
    assert excinfo.value.retry_after is None
    # TimedOut subclasses NetworkError, so it inherits the same classification.
    assert issubclass(TimedOut, NetworkError)
    await adapter.stop()


@pytest.mark.asyncio
async def test_boundary_marks_rate_limit_retryable_with_retry_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from telegram.error import RetryAfter

    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )
    bot.send_message = AsyncMock(side_effect=RetryAfter(retry_after=7))

    with pytest.raises(ChannelError) as excinfo:
        await adapter.send("hi", "12345")

    assert excinfo.value.retryable is True
    assert excinfo.value.retry_after == 7.0
    await adapter.stop()
