"""Discord: outbound delivery, inbound media and dispatch failure boundaries."""

from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from random import Random
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image

import core.channels._discord_uploads as discord_uploads
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


def _fake_discord_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    discord = discord_module._load_discord()
    monkeypatch.setattr(
        discord_module,
        "_load_discord",
        lambda: SimpleNamespace(
            DiscordServerError=_FakeServerError, HTTPException=_FakeHTTPError, File=discord.File
        ),
    )


def _fast_payload_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry payloads without waiting; a rate limit is the write failure that retries."""

    async def retry_payload(
        function: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any
    ) -> Any:
        return await retry_async(function, *args, max_retries=2, initial_delay=0, **kwargs)

    monkeypatch.setattr(discord_module, "retry_async", retry_payload)
    _fake_discord_errors(monkeypatch)


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
            raise _FakeHTTPError(429)
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
            assert failure.value.possibly_delivered is True
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
            raise _FakeHTTPError(429)

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
async def test_upload_fits_a_copy_only_after_413_and_expires_on_a_guild_limit_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_discord_errors(monkeypatch)
    # Model a stale 10 MiB SDK tier and a real 20 MiB limit on a small scale.
    monkeypatch.setattr(discord_uploads, "_DEFAULT_FILE_LIMIT", 2000)
    guild = SimpleNamespace(id=1, filesize_limit=1000)
    target = FakeChannel(100, guild=guild)
    harness = make_adapter(tmp_path, target=target)
    original = io.BytesIO()
    # The lossless PNG is between the old and the current standard limits.
    rows = Random(42).randbytes(40 * 3 * 11)
    Image.frombytes("RGB", (40, 40), (rows * 4)[: 40 * 40 * 3]).save(original, format="BMP")
    file = FileData("map.bmp", "image/bmp", original.getvalue())
    smaller = io.BytesIO()
    Image.new("RGB", (22, 22), "red").save(smaller, format="BMP")
    medium = FileData("smaller.bmp", "image/bmp", smaller.getvalue())
    assert 1000 < len(medium.data) < 2000 < len(file.data)
    attempts: list[tuple[str, bytes]] = []

    async def send(**payload: Any) -> None:
        upload = payload["files"][0]
        data = upload.fp.read()
        attempts.append((upload.filename, data))
        assert payload["content"] == "map"
        if len(data) > max(guild.filesize_limit, 2000):
            raise _FakeHTTPError(413)

    monkeypatch.setattr(target, "send", send)
    try:
        await harness.adapter.send("map", "100", files=[file])
        await harness.adapter.send("map", "100", files=[file])
        await harness.adapter.send("map", "100", files=[medium])
        guild.filesize_limit = 8192
        await harness.adapter.send("map", "100", files=[file])
        assert [name for name, _ in attempts] == [
            "map.bmp",
            "map.png",
            "map.png",
            "smaller.bmp",
            "map.bmp",
        ]
        assert attempts[3][1] == medium.data
        assert 1000 < len(attempts[1][1]) <= 2000
        assert attempts[0][1] == attempts[-1][1] == original.getvalue() == file.data
    finally:
        await harness.adapter.stop()


@pytest.mark.asyncio
async def test_rejected_discord_batch_preserves_animation_and_never_replays_a_posted_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_discord_errors(monkeypatch)
    target = FakeChannel(100, guild=SimpleNamespace(id=1, filesize_limit=4096))
    h = make_adapter(tmp_path, target=target)
    original = io.BytesIO()
    Image.new("RGB", (20, 20), "red").save(
        original,
        "GIF",
        save_all=True,
        append_images=[Image.new("RGB", (20, 20), "blue")],
        duration=100,
    )
    animated = original.getvalue()
    calls: list[tuple[Any, Any]] = []

    async def send(**payload: Any) -> None:
        files = [(f.filename, f.fp.read()) for f in payload["files"]]
        calls.append((payload.get("content"), files))
        if len(files) > 1 or files[0][0] == "movie.gif":
            raise _FakeHTTPError(413)

    monkeypatch.setattr(target, "send", send)
    files = [
        FileData("notes.txt", "text/plain", b"hi"),
        FileData("movie.gif", "image/gif", animated),
    ]
    try:
        with pytest.raises(ChannelError) as failure:
            await h.adapter.send("caption", "100", files=files)
        assert failure.value.possibly_delivered
        assert calls == [
            ("caption", [("notes.txt", b"hi"), ("movie.gif", animated)]),
            ("caption", [("notes.txt", b"hi")]),
            (None, [("movie.gif", animated)]),
        ]
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
    ("error", "lookup_retryable", "post_retryable", "retry_after", "post_maybe_shown"),
    [
        # After a connection error or server fault a posted message may already be
        # visible, so only a target lookup repeats.
        (TimeoutError("gateway timeout"), True, False, None, True),
        (_FakeServerError("gateway unavailable"), True, False, None, True),
        (_FakeHTTPError(403), False, False, None, False),
        (_FakeHTTPError(500), True, False, None, True),
        (_FakeHTTPError(429, retry_after=3.0), True, True, 3.0, False),
    ],
    ids=["timeout", "server-error", "http-403", "http-500", "rate-limit"],
)
async def test_sdk_failures_are_classified_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    lookup_retryable: bool,
    post_retryable: bool,
    retry_after: float | None,
    post_maybe_shown: bool,
) -> None:
    _fake_discord_errors(monkeypatch)
    channel = FakeChannel(100, guild=None, recipient_id=50)
    h = make_adapter(tmp_path, target=channel)
    h.client._channels.clear()
    h.client.fetch_channel = AsyncMock(side_effect=error)

    with pytest.raises(ChannelError) as failure:
        await h.adapter.send_text("100", "hello")

    assert failure.value.retryable is lookup_retryable
    assert failure.value.retry_after == (retry_after if lookup_retryable else None)
    # A permanent lookup failure names the unusable target instead.
    assert isinstance(failure.value, ChannelConfigError) is not lookup_retryable

    _fast_payload_retries(monkeypatch)
    h.client.fetch_channel = AsyncMock(return_value=channel)
    posts = AsyncMock(side_effect=error)
    monkeypatch.setattr(channel, "send", posts)

    with pytest.raises(ChannelError) as failure:
        await h.adapter.send("hello", "100")

    assert posts.await_count == (3 if post_retryable else 1)
    assert failure.value.possibly_delivered is post_maybe_shown
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
