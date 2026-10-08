"""Discord: Gateway lifecycle, access and routing of inbound messages."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import core.channels.discord as discord_module
from core.attachments import AttachmentStore
from core.channels import ChannelConfigError
from core.channels.adapter import ConversationFacts
from core.channels.discord import DiscordChannelAdapter
from core.chat import CommandUnavailability, MessageSender
from core.chat.content_blocks import MediaBlock, TextBlock
from core.sessions import ChatSessionManager

from .discord_test_support import (
    BOT_MENTION,
    CHANNEL_ID,
    GROUP_REPLY_SURFACE,
    FakeAttachment,
    FakeChannel,
    make_adapter,
    make_config,
    make_message,
    session_address,
)
from .engine_test_support import (
    assert_member_trigger,
    command_outcome,
    make_channel_engine,
    make_command_dispatcher,
)

pytestmark = pytest.mark.usefixtures("current_format_data_directory")


def test_constructor_requires_token(tmp_path: Path) -> None:
    with pytest.raises(ChannelConfigError):
        config = make_config(allowed_chat_ids=[100])
        DiscordChannelAdapter(
            config,
            make_channel_engine(tmp_path, config, SimpleNamespace(), ChatSessionManager(tmp_path)),
            lambda _key: "",
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
    config = make_config(allowed_chat_ids=[100])
    adapter = DiscordChannelAdapter(
        config,
        make_channel_engine(tmp_path, config, SimpleNamespace(), ChatSessionManager(tmp_path)),
        lambda _key: "test-token",
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
async def test_stop_drains_backfill_and_waiting_gateway_callbacks(tmp_path: Path) -> None:
    channel = FakeChannel(100, guild=SimpleNamespace(id=1))
    h = make_adapter(tmp_path, target=channel, allowed_chat_ids=[100])
    history_started = asyncio.Event()
    release_history = asyncio.Event()
    history_cancelled = asyncio.Event()

    async def history(**_kwargs: Any) -> Any:
        history_started.set()
        try:
            await release_history.wait()
        except asyncio.CancelledError:
            history_cancelled.set()
            raise
        if False:
            yield None

    channel.history = history  # type: ignore[method-assign]
    message = make_message(
        channel, message_id=200, author_id=50, content="hello <@999>", mentions=[BOT_MENTION]
    )
    first = asyncio.create_task(h.receive(message))
    await history_started.wait()
    # The second callback waits behind the chat's message lock.
    second = asyncio.create_task(h.receive(message))
    await asyncio.sleep(0)

    try:
        await h.adapter.stop()
        assert first.done() and second.done()
        assert history_cancelled.is_set()
        h.reserve_waiting_work.assert_not_called()
        h.client.close.assert_awaited_once()
        assert h.adapter._message_locks == {}
    finally:
        release_history.set()
        await asyncio.gather(first, second, return_exceptions=True)


@pytest.mark.asyncio
async def test_late_gateway_callback_after_stop_cannot_admit_work(tmp_path: Path) -> None:
    channel = FakeChannel(100, guild=None, recipient_id=50)
    h = make_adapter(tmp_path, target=channel, allowed_chat_ids=[100])

    await h.adapter.stop()
    await h.receive(make_message(channel, message_id=200, author_id=50, content="hello"))

    h.reserve_waiting_work.assert_not_called()
    h.trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_transient_per_chat_state_is_bounded_and_idle_locks_are_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Private bookkeeping: no public surface shows the eviction or the lock cleanup.
    adapter = make_adapter(tmp_path, target=FakeChannel(100, guild=SimpleNamespace(id=1))).adapter
    monkeypatch.setattr(discord_module, "_DISCORD_CHAT_STATE_LIMIT", 2)

    for chat_id in ("one", "two", "one", "three"):
        adapter._remember_conversation(
            ConversationFacts(
                platform="discord",
                channel_id=CHANNEL_ID,
                chat_id=chat_id,
                user_id="50",
                access_scope_id=chat_id,
                kind="group",
            )
        )
        adapter._seen_message_ids(chat_id).add(f"message-{chat_id}")

    assert list(adapter._known_conversations) == ["one", "three"]
    assert list(adapter._backfilled_message_ids) == ["one", "three"]

    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def hold_lock() -> None:
        async with adapter._message_lock("three"):
            first_entered.set()
            await release_first.wait()

    async def wait_for_lock() -> None:
        async with adapter._message_lock("three"):
            pass

    first = asyncio.create_task(hold_lock())
    await first_entered.wait()
    second = asyncio.create_task(wait_for_lock())
    await asyncio.sleep(0)
    assert adapter._message_locks["three"].users == 2
    release_first.set()
    await asyncio.gather(first, second)

    assert adapter._message_locks == {}
    await adapter.stop()


@pytest.mark.asyncio
async def test_group_mention_triggers_shared_run_with_sender(tmp_path: Path) -> None:
    channel = FakeChannel(100, guild=SimpleNamespace(id=1))
    h = make_adapter(tmp_path, target=channel, allowed_chat_ids=[100])

    await h.receive(
        make_message(
            channel,
            message_id=200,
            author_id=50,
            content="hello <@999>",
            display_name="Alice",
            mentions=[BOT_MENTION],
        )
    )
    await h.drain()

    assert h.sessions.exists(session_address(100))
    assert_member_trigger(
        h.trigger,
        "assistant",
        "hello <@999>",
        "ch-dc-assistant-100",
        sender=MessageSender(id="50", display_name="Alice"),
        reply_surface=GROUP_REPLY_SURFACE,
    )
    assert channel.sent[0]["content"] == "ok"
    await h.adapter.stop()


@pytest.mark.asyncio
async def test_addressed_group_reply_downloads_quoted_attachment_on_demand(
    tmp_path: Path,
) -> None:
    channel = FakeChannel(100, guild=SimpleNamespace(id=1))
    h = make_adapter(
        tmp_path,
        target=channel,
        allowed_chat_ids=[100],
        admin_user_ids=[50],
        attachment_store=AttachmentStore(tmp_path),
    )
    quoted_attachment = FakeAttachment(300, "juan.png", b"\x89PNG\r\n\x1a\nDATA")
    quoted_message = make_message(
        channel,
        message_id=199,
        author_id=77,
        content="Sunset",
        display_name="Juan",
        attachments=[quoted_attachment],
    )
    message = make_message(
        channel,
        message_id=200,
        author_id=50,
        content="What do you think, <@999>?",
        display_name="Alice",
        mentions=[BOT_MENTION],
        reference=SimpleNamespace(resolved=quoted_message, cached_message=None, message_id=199),
    )

    await h.receive(message)
    await h.drain()

    quoted_attachment.read.assert_awaited_once()
    h.trigger.assert_awaited_once()
    awaited = h.trigger.await_args
    assert awaited is not None
    blocks = awaited.args[1]
    assert isinstance(blocks, list)
    assert blocks[:3] == [
        TextBlock(type="text", text="What do you think, <@999>?"),
        TextBlock(type="text", text="[quoted-message] [Juan|77|member]:"),
        TextBlock(type="text", text="Sunset"),
    ]
    assert isinstance(blocks[3], MediaBlock)
    assert awaited.kwargs["sender"] == MessageSender(id="50", display_name="Alice", role="admin")
    assert "tool_restriction" not in awaited.kwargs
    await h.adapter.stop()


@pytest.mark.asyncio
async def test_agent_command_reports_exact_discord_unavailability(tmp_path: Path) -> None:
    channel = FakeChannel(100, guild=SimpleNamespace(id=1))
    dispatcher = make_command_dispatcher(
        result=command_outcome("agent", "switched"),
        execution_mode="immediate",
        unavailable=CommandUnavailability(command="/agent", surface="channel"),
    )
    h = make_adapter(
        tmp_path,
        target=channel,
        allowed_chat_ids=[100],
        admin_user_ids=[50],
        command_dispatcher=dispatcher,
    )

    await h.receive(make_message(channel, message_id=201, author_id=50, content="/agent planner"))

    assert [payload["content"] for payload in channel.sent] == [
        "The /agent command is not available through Discord."
    ]
    h.trigger.assert_not_awaited()
    dispatcher.execute.assert_not_awaited()
    await h.adapter.stop()


@pytest.mark.asyncio
async def test_thread_inherits_parent_allowlist_and_uses_own_session(tmp_path: Path) -> None:
    thread = FakeChannel(101, guild=SimpleNamespace(id=1), parent_id=100)
    h = make_adapter(tmp_path, target=thread, allowed_chat_ids=[100], response_mode="all")

    await h.receive(make_message(thread, message_id=201, author_id=50, content="thread message"))
    await h.drain()

    metadata = h.sessions.get_metadata(session_address(101))
    assert metadata["platform_conv_id"] == "101"
    assert metadata["last_reply_target"]["platform_target"] == "101"
    # Group roles are scoped to the parent Discord channel, not to the thread.
    assert h.access.participants == {"100": {"50": "Alice"}}
    await h.adapter.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("guild", "kind", "display_name"),
    [
        (SimpleNamespace(id=1, name="My Server"), "group", "My Server / general"),
        (None, "direct", "Alice"),
    ],
    ids=["guild-channel", "direct-message"],
)
async def test_denied_chat_is_recorded_with_its_display_name(
    tmp_path: Path,
    guild: object | None,
    kind: str,
    display_name: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel = FakeChannel(200, guild=guild, name="general", recipient_id=None if guild else 50)
    h = make_adapter(tmp_path, target=channel, allowed_chat_ids=[100])
    caplog.set_level(logging.DEBUG, logger="vbot.channels.discord")

    await h.receive(make_message(channel, message_id=201, author_id=50, content="hello"))
    await h.receive(make_message(channel, message_id=202, author_id=50, content="again"))

    h.reserve_waiting_work.assert_not_called()
    denied = [(entry.chat_id, entry.kind, entry.display_name) for entry in h.adapter.denied_chats()]
    assert denied == [("200", kind, display_name)]
    # Chat ids and names stay in channel status; the log names only the kind.
    messages = [record.getMessage() for record in caplog.records]
    assert messages
    assert not [message for message in messages if "200" in message or display_name in message]
    await h.adapter.stop()
