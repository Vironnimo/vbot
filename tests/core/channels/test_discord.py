"""Tests for DiscordChannelAdapter behavior."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

import core.channels.discord as discord_module
from core.attachments import AttachmentStore, AttachmentTooLargeError
from core.channels.adapter import FileData
from core.channels.channels import ChannelConfig, ChannelConfigError
from core.channels.discord import (
    DISCORD_MESSAGE_LIMIT,
    DiscordChannelAdapter,
    split_discord_message,
)
from core.chat import MessageSender
from core.chat.commands import NotACommand
from core.chat.content_blocks import MediaBlock, TextBlock
from core.runs import ASSISTANT_OUTPUT_EVENT, Run
from core.sessions import ChatSessionManager


class FakePartialMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id

    def to_reference(self, *, fail_if_not_exists: bool) -> SimpleNamespace:
        return SimpleNamespace(
            message_id=self.id,
            fail_if_not_exists=fail_if_not_exists,
        )


class FakeTyping:
    def __init__(self) -> None:
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> None:
        self.entered = True

    async def __aexit__(self, *_args: object) -> None:
        self.exited = True


class FakeChannel:
    def __init__(
        self,
        channel_id: int,
        *,
        guild: object | None,
        parent_id: int | None = None,
        recipient_id: int | None = None,
    ) -> None:
        self.id = channel_id
        self.guild = guild
        self.parent_id = parent_id
        self.recipient = SimpleNamespace(id=recipient_id) if recipient_id is not None else None
        self.sent: list[dict[str, Any]] = []
        self.history_messages: list[Any] = []
        self.history_calls: list[dict[str, Any]] = []
        self.typing_indicator = FakeTyping()

    async def send(self, **payload: Any) -> SimpleNamespace:
        self.sent.append(payload)
        return SimpleNamespace(id=9000 + len(self.sent))

    def history(self, **kwargs: Any) -> Any:
        self.history_calls.append(kwargs)

        async def iterate() -> Any:
            for message in self.history_messages:
                yield message

        return iterate()

    def typing(self) -> FakeTyping:
        return self.typing_indicator

    def get_partial_message(self, message_id: int) -> FakePartialMessage:
        return FakePartialMessage(message_id)


class FakeClient:
    def __init__(self, channels: list[FakeChannel], *, bot_id: int = 999) -> None:
        self.user = SimpleNamespace(id=bot_id)
        self._channels = {channel.id: channel for channel in channels}
        self.fetch_channel = AsyncMock(side_effect=self._fetch_channel)
        self.close = AsyncMock()

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        return self._channels.get(channel_id)

    async def _fetch_channel(self, channel_id: int) -> FakeChannel:
        channel = self._channels.get(channel_id)
        if channel is None:
            raise LookupError(channel_id)
        return channel


class FakeAttachment:
    def __init__(
        self,
        attachment_id: int,
        filename: str,
        data: bytes,
        size: int | None = None,
    ) -> None:
        self.id = attachment_id
        self.filename = filename
        # Discord populates size from metadata; default to the real byte length.
        self.size = len(data) if size is None else size
        self.read = AsyncMock(return_value=data)


def make_config(
    *,
    allowed_chat_ids: list[int | str] | None = None,
    response_mode: str = "mention",
    observe_unaddressed: bool = False,
) -> ChannelConfig:
    config = ChannelConfig(
        id="dc-assistant",
        platform="discord",
        agent_id="assistant",
        dm_scope="per_conversation",
        allowed_chat_ids=cast(Any, list(allowed_chat_ids or [])),
        token_env_var="DISCORD_BOT_TOKEN_DC_ASSISTANT",
        enabled=True,
        response_mode=response_mode,
        observe_unaddressed=observe_unaddressed,
    )
    config.validate()
    return config


def make_command_dispatcher() -> SimpleNamespace:
    return SimpleNamespace(
        dispatch=Mock(return_value=NotACommand()),
        recognizes=Mock(side_effect=lambda text: text.strip().startswith("/")),
    )


def make_completed_run(*, session_id: str, output_text: str = "ok") -> Run:
    run = Run(run_id=f"run-{session_id}", agent_id="assistant", session_id=session_id)
    run.emit(ASSISTANT_OUTPUT_EVENT, {"message": {"content": output_text}})
    run.mark_completed("ok")
    return run


def make_message(
    channel: FakeChannel,
    *,
    message_id: int,
    author_id: int,
    content: str | None,
    display_name: str = "Alice",
    author_is_bot: bool = False,
    mentions: list[Any] | None = None,
    attachments: list[object] | None = None,
    reference: object | None = None,
) -> SimpleNamespace:
    author = SimpleNamespace(
        id=author_id,
        bot=author_is_bot,
        display_name=display_name,
        name=display_name,
    )
    return SimpleNamespace(
        id=message_id,
        author=author,
        channel=channel,
        guild=channel.guild,
        content=content,
        mentions=list(mentions or []),
        raw_mentions=[item.id for item in mentions or []],
        attachments=list(attachments or []),
        reference=reference,
    )


def make_adapter(
    tmp_path: Path,
    *,
    target: FakeChannel,
    allowed_chat_ids: list[int | str] | None = None,
    response_mode: str = "mention",
    observe_unaddressed: bool = False,
    trigger_run: AsyncMock | None = None,
    attachment_store: AttachmentStore | None = None,
) -> tuple[DiscordChannelAdapter, ChatSessionManager, AsyncMock, FakeClient]:
    chat_sessions = ChatSessionManager(tmp_path)
    trigger_mock = trigger_run or AsyncMock()
    trigger_service = SimpleNamespace(
        trigger_run=trigger_mock,
        retry_run=AsyncMock(),
        compact_session=AsyncMock(return_value="Context compacted."),
    )
    adapter = DiscordChannelAdapter(
        make_config(
            allowed_chat_ids=allowed_chat_ids,
            response_mode=response_mode,
            observe_unaddressed=observe_unaddressed,
        ),
        cast(Any, trigger_service),
        cast(Any, chat_sessions),
        lambda _key: "test-token",
        attachment_store=attachment_store,
        command_dispatcher=cast(Any, make_command_dispatcher()),
    )
    client = FakeClient([target])
    adapter._client = client
    adapter._bot_id = "999"
    return adapter, chat_sessions, trigger_mock, client


async def drain_chat_queue(adapter: DiscordChannelAdapter, target_id: int) -> None:
    queue = adapter._engine._chat_queues.get(str(target_id))
    if queue is None:
        await asyncio.sleep(0)
        return
    await asyncio.wait_for(queue.join(), timeout=1)


def test_channel_config_normalizes_discord_snowflakes_to_strings() -> None:
    config = make_config(allowed_chat_ids=[123456789012345678])

    config.validate()

    assert config.allowed_chat_ids == ["123456789012345678"]
    assert config.to_dict()["allowed_chat_ids"] == ["123456789012345678"]


def test_constructor_requires_token(tmp_path: Path) -> None:
    with pytest.raises(ChannelConfigError, match="Missing Discord token"):
        DiscordChannelAdapter(
            make_config(allowed_chat_ids=[100]),
            cast(Any, SimpleNamespace()),
            cast(Any, ChatSessionManager(tmp_path)),
            lambda _key: "",
            command_dispatcher=cast(Any, make_command_dispatcher()),
        )


@pytest.mark.asyncio
async def test_start_enables_message_content_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeIntents:
        def __init__(self) -> None:
            self.message_content = False

        @classmethod
        def default(cls) -> FakeIntents:
            return cls()

    class FakeGatewayClient:
        created: list[FakeGatewayClient] = []

        def __init__(self, *, intents: FakeIntents) -> None:
            self.intents = intents
            self.user = SimpleNamespace(id=999)
            self.events: dict[str, Any] = {}
            self.started_with: str | None = None
            self.closed = False
            self.created.append(self)

        def event(self, callback: Any) -> Any:
            self.events[callback.__name__] = callback
            return callback

        async def start(self, token: str) -> None:
            self.started_with = token
            await self.events["on_ready"]()

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(
        discord_module,
        "_load_discord",
        lambda: SimpleNamespace(Intents=FakeIntents, Client=FakeGatewayClient),
    )
    adapter = DiscordChannelAdapter(
        make_config(allowed_chat_ids=[100]),
        cast(Any, SimpleNamespace()),
        cast(Any, ChatSessionManager(tmp_path)),
        lambda _key: "test-token",
        command_dispatcher=cast(Any, make_command_dispatcher()),
    )

    await adapter.start()

    client = FakeGatewayClient.created[0]
    assert client.intents.message_content is True
    assert client.started_with == "test-token"
    assert set(client.events) == {"on_message", "on_ready"}
    assert adapter._bot_id == "999"
    await adapter.stop()
    assert client.closed is True


@pytest.mark.asyncio
async def test_group_mention_triggers_shared_run_with_sender(tmp_path: Path) -> None:
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(100, guild=guild)
    session_id = "ch-dc-assistant-100"
    trigger_mock = AsyncMock(return_value=make_completed_run(session_id=session_id))
    adapter, chat_sessions, _trigger, _client = make_adapter(
        tmp_path,
        target=channel,
        allowed_chat_ids=[100],
        trigger_run=trigger_mock,
    )
    message = make_message(
        channel,
        message_id=200,
        author_id=50,
        content="hello <@999>",
        display_name="Alice",
        mentions=[SimpleNamespace(id=999)],
    )

    await adapter._handle_inbound_message(message)
    await drain_chat_queue(adapter, 100)

    assert chat_sessions.exists("assistant", session_id)
    trigger_mock.assert_awaited_once_with(
        "assistant",
        "hello <@999>",
        session_id,
        sender=MessageSender(id="50", display_name="Alice"),
    )
    assert channel.sent[0]["content"] == "ok"
    await adapter.stop()


@pytest.mark.asyncio
async def test_thread_inherits_parent_allowlist_and_uses_own_session(tmp_path: Path) -> None:
    guild = SimpleNamespace(id=1)
    thread = FakeChannel(101, guild=guild, parent_id=100)
    session_id = "ch-dc-assistant-101"
    trigger_mock = AsyncMock(return_value=make_completed_run(session_id=session_id))
    adapter, chat_sessions, _trigger, _client = make_adapter(
        tmp_path,
        target=thread,
        allowed_chat_ids=[100],
        response_mode="all",
        trigger_run=trigger_mock,
    )

    await adapter._handle_inbound_message(
        make_message(thread, message_id=201, author_id=50, content="thread message")
    )
    await drain_chat_queue(adapter, 101)

    assert chat_sessions.exists("assistant", session_id)
    metadata = chat_sessions.get_metadata("assistant", session_id)
    assert metadata["platform_conv_id"] == "101"
    assert metadata["last_reply_target"]["platform_target"] == "101"
    await adapter.stop()


@pytest.mark.asyncio
async def test_unaddressed_group_message_is_dropped_without_session(tmp_path: Path) -> None:
    channel = FakeChannel(100, guild=SimpleNamespace(id=1))
    adapter, chat_sessions, trigger_mock, _client = make_adapter(
        tmp_path,
        target=channel,
        allowed_chat_ids=[100],
    )

    await adapter._handle_inbound_message(
        make_message(channel, message_id=200, author_id=50, content="just chatting")
    )
    await drain_chat_queue(adapter, 100)

    trigger_mock.assert_not_awaited()
    assert not chat_sessions.exists("assistant", "ch-dc-assistant-100")
    assert channel.history_calls == []
    await adapter.stop()


@pytest.mark.asyncio
async def test_mention_backfills_history_since_last_bot_reply_in_order(
    tmp_path: Path,
) -> None:
    channel = FakeChannel(100, guild=SimpleNamespace(id=1))
    channel.history_messages = [
        make_message(
            channel,
            message_id=12,
            author_id=51,
            content="second context",
            display_name="Bob",
        ),
        make_message(
            channel,
            message_id=11,
            author_id=50,
            content="first context",
            display_name="Alice",
        ),
        make_message(
            channel,
            message_id=10,
            author_id=999,
            content="previous bot reply",
            display_name="vBot",
            author_is_bot=True,
        ),
        make_message(
            channel,
            message_id=9,
            author_id=52,
            content="too old",
            display_name="Eve",
        ),
    ]
    chat_sessions = ChatSessionManager(tmp_path)
    observed_at_trigger: list[str] = []

    async def trigger_run(
        _agent_id: str,
        _content: str,
        session_id: str,
        *,
        sender: MessageSender | None,
    ) -> Run:
        observed_at_trigger.extend(
            message.content
            for message in chat_sessions.get("assistant", session_id).load()
            if message.role == "note" and isinstance(message.content, str)
        )
        return make_completed_run(session_id=session_id)

    trigger_mock = AsyncMock(side_effect=trigger_run)
    trigger_service = SimpleNamespace(
        trigger_run=trigger_mock,
        retry_run=AsyncMock(),
        compact_session=AsyncMock(return_value="Context compacted."),
    )
    adapter = DiscordChannelAdapter(
        make_config(allowed_chat_ids=[100]),
        cast(Any, trigger_service),
        cast(Any, chat_sessions),
        lambda _key: "test-token",
        command_dispatcher=cast(Any, make_command_dispatcher()),
    )
    adapter._client = FakeClient([channel])
    adapter._bot_id = "999"

    await adapter._handle_inbound_message(
        make_message(
            channel,
            message_id=13,
            author_id=50,
            content="answer this <@999>",
            mentions=[SimpleNamespace(id=999)],
        )
    )
    await drain_chat_queue(adapter, 100)

    assert observed_at_trigger[-2:] == [
        "[channel-message] Alice (50): first context",
        "[channel-message] Bob (51): second context",
    ]
    assert all("too old" not in note for note in observed_at_trigger)
    assert channel.history_calls[0]["limit"] == 50
    assert channel.history_calls[0]["oldest_first"] is False
    await adapter.stop()


@pytest.mark.asyncio
async def test_history_failure_still_processes_triggering_message(tmp_path: Path) -> None:
    channel = FakeChannel(100, guild=SimpleNamespace(id=1))

    def fail_history(**_kwargs: Any) -> Any:
        raise PermissionError("missing read message history")

    channel.history = fail_history  # type: ignore[method-assign]
    session_id = "ch-dc-assistant-100"
    trigger_mock = AsyncMock(return_value=make_completed_run(session_id=session_id))
    adapter, _sessions, _trigger, _client = make_adapter(
        tmp_path,
        target=channel,
        allowed_chat_ids=[100],
        trigger_run=trigger_mock,
    )

    await adapter._handle_inbound_message(
        make_message(
            channel,
            message_id=13,
            author_id=50,
            content="answer this <@999>",
            mentions=[SimpleNamespace(id=999)],
        )
    )
    await drain_chat_queue(adapter, 100)

    trigger_mock.assert_awaited_once()
    await adapter.stop()


@pytest.mark.asyncio
async def test_passive_observation_disables_history_backfill(tmp_path: Path) -> None:
    channel = FakeChannel(100, guild=SimpleNamespace(id=1))
    adapter, chat_sessions, trigger_mock, _client = make_adapter(
        tmp_path,
        target=channel,
        allowed_chat_ids=[100],
        observe_unaddressed=True,
    )

    await adapter._handle_inbound_message(
        make_message(channel, message_id=11, author_id=50, content="background context")
    )
    await drain_chat_queue(adapter, 100)

    notes = chat_sessions.get("assistant", "ch-dc-assistant-100").load()
    assert any(
        message.content == "[channel-message] Alice (50): background context" for message in notes
    )
    assert channel.history_calls == []
    trigger_mock.assert_not_awaited()
    await adapter.stop()


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
    assert chat_sessions.exists("assistant", route.session_id)
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
    with pytest.raises(ValueError, match="positive"):
        split_discord_message("hello", 0)
