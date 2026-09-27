"""Telegram outbound delivery: reply targeting, chunking, retries, typing and files."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import telegram
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

from core.channels import ChannelError
from core.channels.adapter import FileData, ReplyPlanFacts
from core.channels.telegram import (
    TELEGRAM_CAPTION_LIMIT,
    TELEGRAM_MESSAGE_LIMIT,
    split_telegram_message,
)

from .engine_test_support import QUEUE_DRAIN_TIMEOUT_SECONDS, HeldRuns, make_completed_run
from .telegram_test_support import (
    ManualClock,
    drain_chat_queue,
    make_adapter,
    make_update,
)

pytestmark = pytest.mark.usefixtures("current_format_data_directory")

_GROUP_SESSION = "ch-tg-assistant--10001"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chat_id", "thread", "is_topic_message", "reply_to", "message_thread_id"),
    [
        # Direct chats answer in sequence and never quote the question.
        (12345, None, False, None, None),
        # Group replies quote the question; forum topic replies stay in their topic.
        (-10001, 42, True, 777, 42),
        # Outside forum topics the thread id marks a plain reply chain; sending it
        # back would fail with "message thread not found".
        (-10001, 42, False, 777, None),
    ],
    ids=["direct", "forum-topic", "group-reply-chain"],
)
async def test_a_reply_quotes_the_group_message_on_its_first_chunk_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    chat_id: int,
    thread: int | None,
    is_topic_message: bool,
    reply_to: int | None,
    message_thread_id: int | None,
) -> None:
    long_reply = "x" * (TELEGRAM_MESSAGE_LIMIT + 5)
    trigger = AsyncMock(
        return_value=make_completed_run(
            output_text=long_reply,
            session_id=_GROUP_SESSION if chat_id < 0 else "ch-tg-assistant-12345",
        )
    )
    adapter, _sessions, _trigger, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[chat_id],
        response_mode="all",
        trigger_run=trigger,
    )

    await adapter._handle_inbound_message(
        make_update(
            chat_id=chat_id,
            text="hello",
            message_id=777 if chat_id < 0 else 555,
            message_thread_id=thread,
            is_topic_message=is_topic_message,
        ),
        None,
    )
    await drain_chat_queue(adapter, chat_id)

    first, second = (sent.kwargs for sent in bot.send_message.await_args_list)
    assert (first["chat_id"], len(first["text"]), second["chat_id"], len(second["text"])) == (
        chat_id,
        TELEGRAM_MESSAGE_LIMIT,
        chat_id,
        5,
    )
    assert first.get("message_thread_id") == second.get("message_thread_id") == message_thread_id
    if reply_to is None:
        assert "reply_parameters" not in first
    else:
        # The quoted message may be deleted meanwhile; the reply is still delivered.
        assert first["reply_parameters"] == telegram.ReplyParameters(
            message_id=reply_to, allow_sending_without_reply=True
        )
    assert "reply_parameters" not in second
    await adapter.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("exhausted", [False, True], ids=["transient", "exhausted"])
async def test_a_failed_chunk_is_retried_without_resending_delivered_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exhausted: bool
) -> None:
    monkeypatch.setattr("core.utils.retry._sleep", AsyncMock())
    adapter, _sessions, _trigger, bot = make_adapter(
        tmp_path, monkeypatch, allowed_chat_ids=[12345]
    )
    chunks = [letter * TELEGRAM_MESSAGE_LIMIT for letter in "abc"]
    payloads: list[dict[str, Any]] = []

    async def send_message(**payload: Any) -> None:
        payloads.append(payload)
        failed_before = sum(1 for sent in payloads if sent["text"] == chunks[1]) > 1
        if payload["text"] == chunks[1] and (exhausted or not failed_before):
            raise NetworkError("connection reset")

    bot.send_message.side_effect = send_message
    run = make_completed_run(output_text="".join(chunks))

    await adapter.relay_run(
        run,
        ReplyPlanFacts(
            channel_id="tg-assistant",
            platform_target="12345",
            reply_to_message_id="42",
            thread_id="17",
        ),
    )

    # Four attempts per chunk; an exhausted chunk ends the reply rather than
    # re-sending the chunks Telegram already acknowledged.
    assert [payload["text"] for payload in payloads] == (
        [chunks[0], *[chunks[1]] * 4] if exhausted else [chunks[0], chunks[1], chunks[1], chunks[2]]
    )
    assert all(payload["message_thread_id"] == 17 for payload in payloads)
    assert "reply_parameters" in payloads[0]
    assert all("reply_parameters" not in payload for payload in payloads[1:])
    await adapter.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("make_error", "ptb_timedelta", "attempts", "retry_hints", "retry_after"),
    [
        # BadRequest also subclasses NetworkError, yet a rejected request never retries.
        (lambda: BadRequest("Message caption is too long"), False, 1, [], None),
        (lambda: TimedOut("read timeout"), False, 4, [None] * 3, None),
        (lambda: RetryAfter(retry_after=7), False, 4, [7.0] * 3, 7.0),
        # python-telegram-bot reports the flood wait as a timedelta in this mode.
        (lambda: RetryAfter(retry_after=7), True, 4, [7.0] * 3, 7.0),
    ],
    ids=["bad-request", "timed-out", "flood-wait-seconds", "flood-wait-timedelta"],
)
async def test_send_errors_are_retried_by_their_telegram_classification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_error: Callable[[], Exception],
    ptb_timedelta: bool,
    attempts: int,
    retry_hints: list[float | None],
    retry_after: float | None,
) -> None:
    monkeypatch.setenv("PTB_TIMEDELTA", "1" if ptb_timedelta else "0")
    hints: list[float | None] = []

    def record_delay(
        _attempt: int, *, initial_delay: float, retry_after: float | None
    ) -> tuple[float, bool]:
        hints.append(retry_after)
        return 0, retry_after is not None

    monkeypatch.setattr("core.utils.retry.compute_retry_delay", record_delay)
    adapter, _sessions, _trigger, bot = make_adapter(
        tmp_path, monkeypatch, allowed_chat_ids=[12345]
    )
    error = make_error()
    bot.send_message.side_effect = error

    with pytest.raises(ChannelError) as raised:
        await adapter.send("hi", "12345")

    assert raised.value.__cause__ is error
    assert bot.send_message.await_count == attempts
    assert hints == retry_hints
    # The caller must not retry a send whose chunk already exhausted its attempts.
    assert raised.value.retryable is False
    assert raised.value.retry_after == retry_after
    await adapter.stop()


def test_messages_split_by_telegram_utf16_length_without_breaking_characters() -> None:
    # "😀" is one code point but two UTF-16 units, so four units hold two of them.
    assert split_telegram_message("😀" * 8, max_chars=4) == ["😀😀"] * 4
    # An odd budget rounds down instead of cutting a surrogate pair in half.
    message = "a😀b😀c"
    chunks = split_telegram_message(message, max_chars=3)
    assert "".join(chunks) == message
    for chunk in chunks:
        encoded = chunk.encode("utf-16-le")
        assert len(encoded) // 2 <= 3
        assert encoded.decode("utf-16-le") == chunk


@pytest.mark.asyncio
async def test_typing_shows_in_the_topic_while_a_run_is_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = ManualClock()
    held = HeldRuns()
    adapter, _sessions, _trigger, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[-10001],
        response_mode="all",
        trigger_run=held.trigger,
        clock=clock,
    )

    await adapter._handle_inbound_message(
        make_update(chat_id=-10001, message_thread_id=42, is_topic_message=True), None
    )
    await held.wait_started(_GROUP_SESSION)

    async def typing_started() -> None:
        while not bot.send_chat_action.await_count:
            await asyncio.sleep(0)

    await asyncio.wait_for(typing_started(), timeout=QUEUE_DRAIN_TIMEOUT_SECONDS)
    bot.send_chat_action.assert_awaited_once_with(
        chat_id=-10001, action="typing", message_thread_id=42
    )
    # Telegram shows the indicator for about five seconds, so it is refreshed.
    await clock.advance(4)
    assert bot.send_chat_action.await_count == 2

    held.release()
    await drain_chat_queue(adapter, -10001)
    await clock.advance(4)
    assert bot.send_chat_action.await_count == 2
    await adapter.stop()


def _file(filename: str, media_type: str) -> FileData:
    return FileData(filename=filename, media_type=media_type, data=filename.encode())


_PNG = _file("a.png", "image/png")
_JPG = _file("b.jpg", "image/jpeg")
_PDF = _file("c.pdf", "application/pdf")
_PDF2 = _file("d.pdf", "application/pdf")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "files", "thread_id", "expected"),
    [
        ("caption", [_PNG], "42", [("send_photo", [("a.png", "caption")])]),
        ("caption", [_PDF], None, [("send_document", [("c.pdf", "caption")])]),
        (
            # Photos and documents cannot share a media group.
            "caption",
            [_PNG, _PDF],
            "42",
            [("send_photo", [("a.png", "caption")]), ("send_document", [("c.pdf", None)])],
        ),
        (
            "caption",
            [_PNG, _JPG],
            None,
            [("send_media_group", [("a.png", "caption"), ("b.jpg", None)])],
        ),
        (
            "caption",
            [_PNG, _JPG, _PDF, _PDF2],
            "42",
            [
                ("send_media_group", [("a.png", "caption"), ("b.jpg", None)]),
                ("send_media_group", [("c.pdf", None), ("d.pdf", None)]),
            ],
        ),
        (
            "x" * TELEGRAM_CAPTION_LIMIT,
            [_PNG],
            None,
            [("send_photo", [("a.png", "x" * TELEGRAM_CAPTION_LIMIT)])],
        ),
        (
            # A caption over Telegram's limit is sent as its own message first.
            "x" * (TELEGRAM_CAPTION_LIMIT + 1),
            [_PNG],
            "42",
            [
                ("send_message", "x" * (TELEGRAM_CAPTION_LIMIT + 1)),
                ("send_photo", [("a.png", None)]),
            ],
        ),
    ],
    ids=[
        "photo",
        "document",
        "photo-and-document",
        "photo-group",
        "photo-and-document-groups",
        "caption-at-limit",
        "caption-over-limit",
    ],
)
async def test_files_are_sent_as_photos_documents_or_media_groups(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    files: list[FileData],
    thread_id: str | None,
    expected: list[tuple[str, Any]],
) -> None:
    adapter, _sessions, _trigger, bot = make_adapter(
        tmp_path, monkeypatch, allowed_chat_ids=[-10001]
    )
    sent: list[tuple[str, Any]] = []

    def record(method: str) -> AsyncMock:
        async def send(**payload: Any) -> None:
            # Every part of the message lands in the same forum topic, if any.
            assert (payload["chat_id"], payload.get("message_thread_id")) == (
                -10001,
                None if thread_id is None else int(thread_id),
            )
            sent.append((method, payload))

        return AsyncMock(side_effect=send)

    for method in ("send_message", "send_photo", "send_document", "send_media_group"):
        setattr(bot, method, record(method))

    await adapter.send(message, "-10001", files=files, thread_id=thread_id)

    contents = {file.filename: file.data for file in files}

    def describe(method: str, payload: dict[str, Any]) -> Any:
        if method == "send_message":
            return payload["text"]
        if method == "send_media_group":
            # Each group entry must point at its own multipart upload, or Telegram
            # rejects the whole group with "media not found".
            for item in payload["media"]:
                assert item.media.attach_uri.startswith("attach://")
                assert item.media.input_file_content == contents[item.media.filename]
            return [(item.media.filename, item.caption) for item in payload["media"]]
        upload = payload["photo" if method == "send_photo" else "document"]
        assert upload.input_file_content == contents[upload.filename]
        return [(upload.filename, payload.get("caption"))]

    assert [(method, describe(method, payload)) for method, payload in sent] == expected
    await adapter.stop()
