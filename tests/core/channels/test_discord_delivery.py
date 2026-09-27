"""Discord: outbound delivery, inbound media and dispatch failure boundaries."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

import core.channels.discord as discord_module
from core.attachments import AttachmentStore, AttachmentTooLargeError
from core.channels import ChannelConfigError, ChannelError
from core.channels.adapter import FileData
from core.channels.discord import DISCORD_MESSAGE_LIMIT
from core.chat.content_blocks import MediaBlock, TextBlock
from core.extensions import InteractionButton
from core.utils.retry import retry_async

from .discord_test_support import (
    FakeAttachment,
    FakeChannel,
    make_adapter,
    make_message,
    session_address,
)

pytestmark = pytest.mark.usefixtures("current_format_data_directory")


class _FakeServerError(Exception):
    pass


class _FakeHTTPError(Exception):
    def __init__(self, status: int, retry_after: float | None = None) -> None:
        super().__init__(f"http {status}")
        self.status = status
        self.retry_after = retry_after


def _fast_payload_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    async def retry_payload(
        function: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any
    ) -> Any:
        return await retry_async(function, *args, max_retries=2, initial_delay=0, **kwargs)

    monkeypatch.setattr(discord_module, "retry_async", retry_payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("exhaust_retries", [False, True])
async def test_reply_is_split_and_retries_only_the_failed_chunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exhaust_retries: bool
) -> None:
    _fast_payload_retries(monkeypatch)
    channel = FakeChannel(100, guild=SimpleNamespace(id=1))
    h = make_adapter(tmp_path, target=channel)
    attempts: list[dict[str, Any]] = []
    first_chunk = "x" * DISCORD_MESSAGE_LIMIT

    async def send(**payload: Any) -> None:
        attempts.append(payload)
        if payload["content"] == "tail" and (exhaust_retries or len(attempts) == 2):
            raise TimeoutError("second chunk unavailable")
        channel.sent.append(payload)

    monkeypatch.setattr(channel, "send", send)
    try:
        if exhaust_retries:
            with pytest.raises(ChannelError) as failure:
                # Model the engine's outer retry: it must never replay acknowledged chunks.
                await retry_async(
                    h.adapter.send_text,
                    "100",
                    first_chunk + "tail",
                    max_retries=1,
                    initial_delay=0,
                )
            assert failure.value.retryable is False
            assert [entry["content"] for entry in attempts] == [first_chunk, "tail", "tail", "tail"]
            assert [entry["content"] for entry in channel.sent] == [first_chunk]
        else:
            await h.adapter.send_text("100", first_chunk + "tail", reply_to_message_id="200")
            assert [entry["content"] for entry in attempts] == [first_chunk, "tail", "tail"]
            assert [entry["content"] for entry in channel.sent] == [first_chunk, "tail"]
            # Only the first chunk replies to the triggering message, without a ping.
            reference = attempts[0]["reference"]
            assert (reference.message_id, reference.fail_if_not_exists) == (200, False)
            assert attempts[0]["mention_author"] is False
            assert all("reference" not in entry for entry in attempts[1:])
    finally:
        await h.adapter.stop()


@pytest.mark.asyncio
async def test_send_batches_files_ten_per_message(tmp_path: Path) -> None:
    channel = FakeChannel(100, guild=SimpleNamespace(id=1))
    h = make_adapter(tmp_path, target=channel, allowed_chat_ids=[100])
    files = [
        FileData(filename=f"file-{index}.txt", media_type="text/plain", data=b"data")
        for index in range(11)
    ]

    await h.adapter.send("caption", "100", files=files)

    assert len(channel.sent) == 2
    assert channel.sent[0]["content"] == "caption"
    assert len(channel.sent[0]["files"]) == 10
    assert "content" not in channel.sent[1]
    assert len(channel.sent[1]["files"]) == 1
    await h.adapter.stop()


@pytest.mark.asyncio
async def test_file_retry_recreates_consumed_sdk_file_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fast_payload_retries(monkeypatch)
    channel = FakeChannel(100, guild=SimpleNamespace(id=1))
    h = make_adapter(tmp_path, target=channel)
    uploads: list[Any] = []

    async def send(**payload: Any) -> None:
        uploaded = payload["files"][0]
        uploads.append(uploaded)
        assert uploaded.fp.read() == b"file bytes"
        # discord.py consumes the stream and closes its SDK wrapper on either outcome.
        uploaded.close()
        if len(uploads) == 1:
            raise TimeoutError("upload unavailable")

    monkeypatch.setattr(channel, "send", send)
    try:
        await h.adapter.send(
            "caption", "100", files=[FileData("file.txt", "text/plain", b"file bytes")]
        )
        assert len(uploads) == 2
        assert uploads[0] is not uploads[1]
        assert all(uploaded.fp.closed for uploaded in uploads)
    finally:
        await h.adapter.stop()


@pytest.mark.asyncio
async def test_send_rejects_buttons(tmp_path: Path) -> None:
    channel = FakeChannel(100, guild=SimpleNamespace(id=1))
    h = make_adapter(tmp_path, target=channel, allowed_chat_ids=[100])

    # Discord has no interactive-message support; a non-None buttons value is
    # rejected rather than silently dropped.
    with pytest.raises(ChannelError):
        await h.adapter.send(
            "hi",
            "100",
            buttons=[[InteractionButton(label="Milk ⬜", data="chk:milk")]],
        )

    assert channel.sent == []
    await h.adapter.stop()


@pytest.mark.asyncio
async def test_typing_indicator_uses_channel_context(tmp_path: Path) -> None:
    channel = FakeChannel(100, guild=SimpleNamespace(id=1))
    h = make_adapter(tmp_path, target=channel, allowed_chat_ids=[100])

    async with h.adapter.activity_indicator("100"):
        assert channel.typing_indicator.entered is True

    assert channel.typing_indicator.exited is True
    await h.adapter.stop()


@pytest.mark.asyncio
async def test_uncached_target_lookup_can_retry_transient_failure(tmp_path: Path) -> None:
    channel = FakeChannel(100, guild=None, recipient_id=50)
    h = make_adapter(tmp_path, target=channel)
    h.client._channels.clear()
    h.client.fetch_channel = AsyncMock(side_effect=[TimeoutError("lookup unavailable"), channel])
    try:
        await retry_async(h.adapter.send_text, "100", "hello", max_retries=1, initial_delay=0)
        assert h.client.fetch_channel.await_count == 2
        assert [entry["content"] for entry in channel.sent] == ["hello"]
    finally:
        await h.adapter.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "retryable", "retry_after"),
    [
        (TimeoutError("gateway timeout"), True, None),
        (_FakeServerError("gateway unavailable"), True, None),
        (_FakeHTTPError(403), False, None),
        (_FakeHTTPError(500), True, None),
        (_FakeHTTPError(429, retry_after=3.0), True, 3.0),
    ],
    ids=["timeout", "server-error", "http-403", "http-500", "rate-limit"],
)
async def test_sdk_failures_are_classified_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    retryable: bool,
    retry_after: float | None,
) -> None:
    monkeypatch.setattr(
        discord_module,
        "_load_discord",
        lambda: SimpleNamespace(DiscordServerError=_FakeServerError, HTTPException=_FakeHTTPError),
    )
    h = make_adapter(tmp_path, target=FakeChannel(100, guild=None, recipient_id=50))
    h.client._channels.clear()
    h.client.fetch_channel = AsyncMock(side_effect=error)

    with pytest.raises(ChannelError) as failure:
        await h.adapter.send_text("100", "hello")

    assert failure.value.retryable is retryable
    assert failure.value.retry_after == retry_after
    # A permanent lookup failure names the unusable target instead.
    assert isinstance(failure.value, ChannelConfigError) is not retryable
    await h.adapter.stop()


@pytest.mark.asyncio
async def test_inbound_attachments_become_canonical_blocks(tmp_path: Path) -> None:
    channel = FakeChannel(100, guild=None, recipient_id=50)
    h = make_adapter(
        tmp_path, target=channel, allowed_chat_ids=[100], attachment_store=AttachmentStore(tmp_path)
    )
    message = make_message(
        channel,
        message_id=200,
        author_id=50,
        content="look",
        attachments=[FakeAttachment(300, "image.png", b"\x89PNG\r\n\x1a\nDATA")],
    )

    blocks = await h.adapter.build_media_blocks(message)

    assert isinstance(blocks[0], TextBlock)
    assert blocks[0].text == "look"
    assert isinstance(blocks[1], MediaBlock)
    assert blocks[1].filename == "image.png"
    assert blocks[1].media_type == "image/png"
    await h.adapter.stop()


@pytest.mark.asyncio
async def test_oversized_inbound_attachment_rejected_before_download(tmp_path: Path) -> None:
    channel = FakeChannel(100, guild=None, recipient_id=50)
    h = make_adapter(
        tmp_path,
        target=channel,
        allowed_chat_ids=[100],
        attachment_store=AttachmentStore(tmp_path, max_size_bytes=8),
    )
    oversized = FakeAttachment(300, "huge.png", b"\x89PNG\r\n\x1a\nDATA", size=1_000_000)
    message = make_message(
        channel, message_id=200, author_id=50, content="look", attachments=[oversized]
    )

    with pytest.raises(AttachmentTooLargeError):
        await h.adapter.build_media_blocks(message)

    # The file must be refused on its reported size alone — never pulled into memory.
    oversized.read.assert_not_awaited()
    await h.adapter.stop()


@pytest.mark.asyncio
async def test_ensure_outbound_session_uses_cached_target_kind(tmp_path: Path) -> None:
    channel = FakeChannel(100, guild=None, recipient_id=50)
    h = make_adapter(tmp_path, target=channel, allowed_chat_ids=[100])

    route = await h.adapter.ensure_outbound_session("100")

    assert route.session_id == "ch-dc-assistant-100"
    assert h.sessions.exists(session_address(100))
    await h.adapter.stop()


@pytest.mark.asyncio
async def test_inbound_dispatch_failure_logged_in_vbot_logger(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # discord.py catches and logs handler exceptions only to its own `discord` logger and
    # silently drops the message; the adapter must surface the failure in
    # vbot.channels.discord so the crash is visible.
    channel = FakeChannel(100, guild=None, recipient_id=50)
    dispatcher = SimpleNamespace(prepare=Mock(side_effect=RuntimeError("dispatch exploded")))
    h = make_adapter(
        tmp_path, target=channel, allowed_chat_ids=[100], command_dispatcher=dispatcher
    )

    with caplog.at_level(logging.ERROR, logger="vbot.channels.discord"):
        # Must not raise out of the handler: discord.py would otherwise be the only place
        # the error lands.
        await h.receive(make_message(channel, message_id=200, author_id=50, content="hello"))

    error_records = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert any("Discord inbound dispatch failed" in record.getMessage() for record in error_records)
    assert any(record.exc_info is not None for record in error_records)
    await h.adapter.stop()


@pytest.mark.asyncio
async def test_inbound_dispatch_propagates_cancellation(tmp_path: Path) -> None:
    # Cooperative cancel must not be swallowed by the dispatch error boundary.
    channel = FakeChannel(100, guild=None, recipient_id=50)
    dispatcher = SimpleNamespace(prepare=Mock(side_effect=asyncio.CancelledError))
    h = make_adapter(
        tmp_path, target=channel, allowed_chat_ids=[100], command_dispatcher=dispatcher
    )

    with pytest.raises(asyncio.CancelledError):
        await h.receive(make_message(channel, message_id=200, author_id=50, content="hello"))
    await h.adapter.stop()
