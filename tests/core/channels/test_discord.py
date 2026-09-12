"""Discord: routing behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

import core.channels.discord as discord_module
from core.attachments import AttachmentStore
from core.channels import ChannelConfigError
from core.channels.adapter import ConversationFacts
from core.channels.discord import (
    DiscordChannelAdapter,
)
from core.chat import CommandUnavailability, MessageSender, PreparedCommand, ReplySurface
from core.chat.content_blocks import MediaBlock, TextBlock
from core.runs import ASSISTANT_OUTPUT_EVENT, Run, RunKind, WaitingWorkAdmission
from core.sessions import ChatSessionManager, SessionAddress
from tests.core.channels.discord_helpers import (
    FakeAttachment,
    FakeChannel,
    FakeClient,
    make_adapter,
    make_command_dispatcher,
    make_config,
    make_message,
)

from .engine_test_support import assert_member_trigger

pytestmark = pytest.mark.usefixtures("current_format_data_directory")

CHANNEL_REPLY_SURFACE = ReplySurface.channel(
    platform="discord",
    platform_display_name="Discord",
    channel_id="dc-assistant",
    conversation_kind="group",
)


def make_completed_run(*, session_id: str, output_text: str = "ok") -> Run:
    run = Run(run_id=f"run-{session_id}", agent_id="assistant", session_id=session_id)
    run.emit(ASSISTANT_OUTPUT_EVENT, {"message": {"content": output_text}})
    run.mark_completed("ok")
    return run


async def drain_chat_queue(adapter: DiscordChannelAdapter, target_id: int) -> None:
    queue = adapter._engine._chat_queues.get(str(target_id))
    if queue is None:
        await asyncio.sleep(0)
        return
    # The suite-wide timeout remains the deadlock guard. A shorter nested timeout
    # flakes under xdist load even though the queue is still making progress.
    await queue.join()


def test_channel_config_normalizes_discord_snowflakes_to_strings() -> None:
    config = make_config(allowed_chat_ids=[123456789012345678])

    config.validate()

    assert config.allowed_chat_ids == ["123456789012345678"]
    assert config.to_dict()["allowed_chat_ids"] == ["123456789012345678"]


@pytest.mark.asyncio
async def test_transient_per_chat_state_is_bounded_and_idle_locks_are_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = FakeChannel(100, guild=SimpleNamespace(id=1))
    adapter, _sessions, _trigger, _client = make_adapter(tmp_path, target=channel)
    monkeypatch.setattr(discord_module, "_DISCORD_CHAT_STATE_LIMIT", 2)

    for chat_id in ("one", "two", "one", "three"):
        adapter._remember_conversation(
            ConversationFacts(
                platform="discord",
                channel_id="dc-assistant",
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
async def test_thread_uses_parent_discord_channel_as_access_scope(tmp_path: Path) -> None:
    guild = SimpleNamespace(id=1)
    thread = FakeChannel(101, guild=guild, parent_id=100)
    adapter, _chat_sessions, _trigger, _client = make_adapter(
        tmp_path,
        target=thread,
        allowed_chat_ids=[100],
    )

    conversation = adapter._conversation_facts(
        make_message(thread, message_id=201, author_id=50, content="hello")
    )

    assert conversation is not None
    assert conversation.chat_id == "101"
    assert conversation.thread_id == "101"
    assert conversation.access_scope_id == "100"
    await adapter.stop()


def test_constructor_requires_token(tmp_path: Path) -> None:
    with pytest.raises(ChannelConfigError):
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

    assert chat_sessions.exists(
        SessionAddress(project_id=None, agent_id="assistant", session_id=session_id)
    )
    assert_member_trigger(
        trigger_mock,
        "assistant",
        "hello <@999>",
        session_id,
        sender=MessageSender(id="50", display_name="Alice"),
        reply_surface=CHANNEL_REPLY_SURFACE,
    )
    assert channel.sent[0]["content"] == "ok"
    await adapter.stop()


@pytest.mark.asyncio
async def test_addressed_group_reply_downloads_quoted_attachment_on_demand(
    tmp_path: Path,
) -> None:
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(100, guild=guild)
    attachment_store = AttachmentStore(tmp_path)
    session_id = "ch-dc-assistant-100"
    trigger_mock = AsyncMock(return_value=make_completed_run(session_id=session_id))
    adapter, _sessions, _trigger, _client = make_adapter(
        tmp_path,
        target=channel,
        allowed_chat_ids=[100],
        admin_user_ids=[50],
        trigger_run=trigger_mock,
        attachment_store=attachment_store,
    )
    quoted_attachment = FakeAttachment(
        300,
        "juan.png",
        b"\x89PNG\r\n\x1a\nDATA",
    )
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
        mentions=[SimpleNamespace(id=999)],
        reference=SimpleNamespace(
            resolved=quoted_message,
            cached_message=None,
            message_id=199,
        ),
    )

    await adapter._handle_inbound_message(message)
    await drain_chat_queue(adapter, 100)

    quoted_attachment.read.assert_awaited_once()
    trigger_mock.assert_awaited_once()
    awaited = trigger_mock.await_args
    assert awaited is not None
    blocks = awaited.args[1]
    assert isinstance(blocks, list)
    assert blocks[:3] == [
        TextBlock(type="text", text="What do you think, <@999>?"),
        TextBlock(type="text", text="[quoted-message] [Juan|77|member]:"),
        TextBlock(type="text", text="Sunset"),
    ]
    assert isinstance(blocks[3], MediaBlock)
    assert awaited.kwargs["sender"] == MessageSender(
        id="50",
        display_name="Alice",
        role="admin",
    )
    assert "tool_restriction" not in awaited.kwargs
    await adapter.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["/agent", "/agent planner"])
async def test_agent_command_reports_exact_discord_unavailability(
    tmp_path: Path, message: str
) -> None:
    channel = FakeChannel(100, guild=SimpleNamespace(id=1))
    dispatcher = SimpleNamespace(
        prepare=Mock(
            return_value=PreparedCommand(
                name="agent",
                argument=None,
                execution_mode="immediate",
            )
        ),
        unavailability=Mock(
            return_value=CommandUnavailability(command="/agent", surface="channel")
        ),
        execute=AsyncMock(),
    )
    adapter, _sessions, trigger_mock, _client = make_adapter(
        tmp_path,
        target=channel,
        allowed_chat_ids=[100],
        admin_user_ids=[50],
        command_dispatcher=dispatcher,
    )

    await adapter._handle_inbound_message(
        make_message(channel, message_id=201, author_id=50, content=message)
    )

    assert [payload["content"] for payload in channel.sent] == [
        "The /agent command is not available through Discord."
    ]
    trigger_mock.assert_not_awaited()
    dispatcher.execute.assert_not_awaited()
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

    assert chat_sessions.exists(
        SessionAddress(project_id=None, agent_id="assistant", session_id=session_id)
    )
    metadata = chat_sessions.get_metadata(
        SessionAddress(project_id=None, agent_id="assistant", session_id=session_id)
    )
    assert metadata["platform_conv_id"] == "101"
    assert metadata["last_reply_target"]["platform_target"] == "101"
    await adapter.stop()


@pytest.mark.asyncio
async def test_denied_guild_channel_is_recorded_with_guild_and_channel_name(
    tmp_path: Path,
) -> None:
    channel = FakeChannel(200, guild=SimpleNamespace(id=1, name="My Server"), name="general")
    adapter, _chat_sessions, trigger_mock, _client = make_adapter(
        tmp_path,
        target=channel,
        allowed_chat_ids=[100],
    )

    await adapter._handle_inbound_message(
        make_message(channel, message_id=200, author_id=50, content="hello")
    )

    trigger_mock.assert_not_awaited()
    entries = adapter.denied_chats()
    assert len(entries) == 1
    assert entries[0].chat_id == "200"
    assert entries[0].kind == "group"
    assert entries[0].display_name == "My Server / general"


@pytest.mark.asyncio
async def test_denied_direct_message_is_recorded_with_sender_name(tmp_path: Path) -> None:
    channel = FakeChannel(300, guild=None, recipient_id=50)
    adapter, _chat_sessions, trigger_mock, _client = make_adapter(
        tmp_path,
        target=channel,
        allowed_chat_ids=[100],
    )

    await adapter._handle_inbound_message(
        make_message(channel, message_id=201, author_id=50, content="hello", display_name="Alice")
    )

    trigger_mock.assert_not_awaited()
    entries = adapter.denied_chats()
    assert len(entries) == 1
    assert entries[0].chat_id == "300"
    assert entries[0].kind == "direct"
    assert entries[0].display_name == "Alice"


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
    assert not chat_sessions.exists(
        SessionAddress(project_id=None, agent_id="assistant", session_id="ch-dc-assistant-100")
    )
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
        reply_surface: ReplySurface,
        tool_restriction: tuple[str, ...],
        tool_denial_resolver: Any,
        run_kind: RunKind,
    ) -> Run:
        assert reply_surface == CHANNEL_REPLY_SURFACE
        assert tool_restriction == ("web_search", "web_fetch")
        assert callable(tool_denial_resolver)
        assert run_kind is RunKind.CHANNEL
        observed_at_trigger.extend(
            message.content
            for message in chat_sessions.get(
                SessionAddress(project_id=None, agent_id="assistant", session_id=session_id)
            ).load()
            if message.role == "note" and isinstance(message.content, str)
        )
        return make_completed_run(session_id=session_id)

    trigger_mock = AsyncMock(side_effect=trigger_run)

    async def trigger_with_admission(*args: Any, **kwargs: Any) -> Any:
        kwargs.pop("waiting_work_admission", None)
        return await trigger_mock(*args, **kwargs)

    trigger_service = SimpleNamespace(
        trigger_run=trigger_with_admission,
        compact_session=AsyncMock(return_value="Context compacted."),
        reserve_waiting_work=Mock(
            return_value=WaitingWorkAdmission(id="test-admission", scope="test:chat")
        ),
        release_waiting_work=Mock(return_value=True),
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
        "[channel-message] [Alice|50|member]: first context",
        "[channel-message] [Bob|51|member]: second context",
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

    notes = chat_sessions.get(
        SessionAddress(project_id=None, agent_id="assistant", session_id="ch-dc-assistant-100")
    ).load()
    assert any(
        message.content == "[channel-message] [Alice|50|member]: background context"
        for message in notes
    )
    assert channel.history_calls == []
    trigger_mock.assert_not_awaited()
    await adapter.stop()
