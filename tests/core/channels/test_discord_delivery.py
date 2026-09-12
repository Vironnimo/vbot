"""Discord: delivery behavior."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import core.channels.discord as discord_module
from core.attachments import AttachmentStore, AttachmentTooLargeError
from core.channels import ChannelError
from core.channels.adapter import FileData
from core.channels.discord import (
    DISCORD_MESSAGE_LIMIT,
    split_discord_message,
)
from core.chat.content_blocks import MediaBlock, TextBlock
from core.extensions import InteractionButton
from core.sessions import SessionAddress
from tests.core.channels.discord_helpers import (
    FakeAttachment,
    FakeChannel,
    make_adapter,
    make_message,
)

pytestmark = pytest.mark.usefixtures("current_format_data_directory")


class _FakeServerError(Exception):
    pass


class _FakeHTTPError(Exception):
    def __init__(self, status: int, retry_after: float | None = None) -> None:
        super().__init__(f"http {status}")
        self.status = status
        self.retry_after = retry_after


def _install_fake_discord_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        discord_module,
        "_load_discord",
        lambda: SimpleNamespace(
            DiscordServerError=_FakeServerError,
            HTTPException=_FakeHTTPError,
        ),
    )


@pytest.mark.asyncio
async def test_reply_splits_text_and_references_first_chunk_only(tmp_path: Path) -> None:
    channel = FakeChannel(100, guild=SimpleNamespace(id=1))
    adapter, _sessions, _trigger, _client = make_adapter(
        tmp_path,
        target=channel,
        allowed_chat_ids=[100],
    )
    long_text = "x" * (DISCORD_MESSAGE_LIMIT + 5)

    await adapter.send_text("100", long_text, reply_to_message_id="200")

    assert len(channel.sent) == 2
    assert channel.sent[0]["reference"].message_id == 200
    assert channel.sent[0]["reference"].fail_if_not_exists is False
    assert channel.sent[0]["mention_author"] is False
    assert "reference" not in channel.sent[1]
    assert [len(call["content"]) for call in channel.sent] == [2000, 5]
    await adapter.stop()


@pytest.mark.asyncio
async def test_send_batches_files_ten_per_message(tmp_path: Path) -> None:
    channel = FakeChannel(100, guild=SimpleNamespace(id=1))
    adapter, _sessions, _trigger, _client = make_adapter(
        tmp_path,
        target=channel,
        allowed_chat_ids=[100],
    )
    files = [
        FileData(filename=f"file-{index}.txt", media_type="text/plain", data=b"data")
        for index in range(11)
    ]

    await adapter.send("caption", "100", files=files)

    assert len(channel.sent) == 2
    assert channel.sent[0]["content"] == "caption"
    assert len(channel.sent[0]["files"]) == 10
    assert "content" not in channel.sent[1]
    assert len(channel.sent[1]["files"]) == 1
    await adapter.stop()


@pytest.mark.asyncio
async def test_send_rejects_buttons(tmp_path: Path) -> None:
    channel = FakeChannel(100, guild=SimpleNamespace(id=1))
    adapter, _sessions, _trigger, _client = make_adapter(
        tmp_path,
        target=channel,
        allowed_chat_ids=[100],
    )

    # Discord has no interactive-message support; a non-None buttons value is
    # rejected rather than silently dropped.
    with pytest.raises(ChannelError):
        await adapter.send(
            "hi",
            "100",
            buttons=[[InteractionButton(label="Milk ⬜", data="chk:milk")]],
        )

    assert channel.sent == []
    await adapter.stop()


@pytest.mark.asyncio
async def test_typing_indicator_uses_channel_context(tmp_path: Path) -> None:
    channel = FakeChannel(100, guild=SimpleNamespace(id=1))
    adapter, _sessions, _trigger, _client = make_adapter(
        tmp_path,
        target=channel,
        allowed_chat_ids=[100],
    )

    async with adapter.activity_indicator("100"):
        assert channel.typing_indicator.entered is True

    assert channel.typing_indicator.exited is True
    await adapter.stop()


@pytest.mark.asyncio
async def test_inbound_attachments_become_canonical_blocks(tmp_path: Path) -> None:
    channel = FakeChannel(100, guild=None, recipient_id=50)
    attachment_store = AttachmentStore(tmp_path)
    adapter, _sessions, _trigger, _client = make_adapter(
        tmp_path,
        target=channel,
        allowed_chat_ids=[100],
        attachment_store=attachment_store,
    )
    message = make_message(
        channel,
        message_id=200,
        author_id=50,
        content="look",
        attachments=[
            FakeAttachment(300, "image.png", b"\x89PNG\r\n\x1a\nDATA"),
        ],
    )

    blocks = await adapter.build_media_blocks(message)

    assert isinstance(blocks[0], TextBlock)
    assert blocks[0].text == "look"
    assert isinstance(blocks[1], MediaBlock)
    assert blocks[1].filename == "image.png"
    assert blocks[1].media_type == "image/png"
    await adapter.stop()


@pytest.mark.asyncio
async def test_oversized_inbound_attachment_rejected_before_download(tmp_path: Path) -> None:
    channel = FakeChannel(100, guild=None, recipient_id=50)
    attachment_store = AttachmentStore(tmp_path, max_size_bytes=8)
    adapter, _sessions, _trigger, _client = make_adapter(
        tmp_path,
        target=channel,
        allowed_chat_ids=[100],
        attachment_store=attachment_store,
    )
    oversized = FakeAttachment(300, "huge.png", b"\x89PNG\r\n\x1a\nDATA", size=1_000_000)
    message = make_message(
        channel,
        message_id=200,
        author_id=50,
        content="look",
        attachments=[oversized],
    )

    with pytest.raises(AttachmentTooLargeError):
        await adapter.build_media_blocks(message)

    # The file must be refused on its reported size alone — never pulled into memory.
    oversized.read.assert_not_awaited()
    await adapter.stop()


@pytest.mark.asyncio
async def test_ensure_outbound_session_uses_cached_target_kind(tmp_path: Path) -> None:
    channel = FakeChannel(100, guild=None, recipient_id=50)
    adapter, chat_sessions, _trigger, _client = make_adapter(
        tmp_path,
        target=channel,
        allowed_chat_ids=[100],
    )

    route = adapter.ensure_outbound_session("100")

    assert route.session_id == "ch-dc-assistant-100"
    assert chat_sessions.exists(
        SessionAddress(project_id=None, agent_id="assistant", session_id=route.session_id)
    )
    await adapter.stop()


@pytest.mark.asyncio
async def test_inbound_dispatch_failure_logged_in_vbot_logger(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # discord.py catches and logs handler exceptions only to its own `discord` logger and
    # silently drops the message; the adapter must surface the failure in
    # vbot.channels.discord so the crash is visible.
    channel = FakeChannel(100, guild=None, recipient_id=50)
    adapter, _sessions, _trigger, _client = make_adapter(
        tmp_path,
        target=channel,
        allowed_chat_ids=[100],
    )

    async def boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("dispatch exploded")

    adapter._engine.handle_inbound_text = boom  # type: ignore[method-assign]
    message = make_message(channel, message_id=200, author_id=50, content="hello")

    with caplog.at_level(logging.ERROR, logger="vbot.channels.discord"):
        # Must not raise out of the handler: discord.py would otherwise be the only place
        # the error lands.
        await adapter._handle_inbound_message(message)

    error_records = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert any("Discord inbound dispatch failed" in record.getMessage() for record in error_records)
    assert any(record.exc_info is not None for record in error_records)
    await adapter.stop()


@pytest.mark.asyncio
async def test_inbound_dispatch_propagates_cancellation(tmp_path: Path) -> None:
    # Cooperative cancel must not be swallowed by the dispatch error boundary.
    channel = FakeChannel(100, guild=None, recipient_id=50)
    adapter, _sessions, _trigger, _client = make_adapter(
        tmp_path,
        target=channel,
        allowed_chat_ids=[100],
    )

    async def cancel(*_args: Any, **_kwargs: Any) -> None:
        raise asyncio.CancelledError

    adapter._engine.handle_inbound_text = cancel  # type: ignore[method-assign]
    message = make_message(channel, message_id=200, author_id=50, content="hello")

    with pytest.raises(asyncio.CancelledError):
        await adapter._handle_inbound_message(message)
    await adapter.stop()


def test_split_discord_message_rejects_non_positive_limit() -> None:
    with pytest.raises(ValueError):
        split_discord_message("hello", 0)


@pytest.mark.parametrize(
    ("error", "expect_retryable"),
    [
        (TimeoutError("gateway timeout"), True),
        (ConnectionError("socket closed"), True),
        (_FakeServerError("gateway unavailable"), True),
        (_FakeHTTPError(400), False),
        (_FakeHTTPError(403), False),
        (_FakeHTTPError(500), True),
        (_FakeHTTPError(429, retry_after=3.0), True),
    ],
)
def test_send_error_classification(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expect_retryable: bool,
) -> None:
    from core.channels.discord import _classify_discord_send_error

    _install_fake_discord_errors(monkeypatch)
    classified = _classify_discord_send_error(error)

    assert isinstance(classified, ChannelError)
    assert classified.retryable is expect_retryable


def test_rate_limit_classification_carries_retry_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.channels.discord import _classify_discord_send_error

    _install_fake_discord_errors(monkeypatch)
    classified = _classify_discord_send_error(_FakeHTTPError(429, retry_after=3.0))

    assert classified.retryable is True
    assert classified.retry_after == 3.0
