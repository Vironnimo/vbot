"""Discord SDK fakes and an adapter harness on the real Channel engine."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

from core.attachments import AttachmentStore
from core.channels import ChannelConfig
from core.channels.discord import DiscordChannelAdapter
from core.chat import ReplySurface
from core.runs import ASSISTANT_OUTPUT_EVENT, ChatRunManager, Run
from core.sessions import ChatSessionManager, SessionAddress

from .engine_test_support import (
    MemoryChannelAccessRegistry,
    channel_state,
    drain,
    make_command_dispatcher,
    make_trigger_service,
)

CHANNEL_ID = "dc-assistant"
BOT_MENTION = SimpleNamespace(id=999)
GROUP_REPLY_SURFACE = ReplySurface.channel(
    platform="discord",
    platform_display_name="Discord",
    channel_id=CHANNEL_ID,
    conversation_kind="group",
)


def session_address(chat_id: int) -> SessionAddress:
    return SessionAddress(
        project_id=None, agent_id="assistant", session_id=f"ch-{CHANNEL_ID}-{chat_id}"
    )


def make_completed_run(session_id: str, output_text: str = "ok") -> Run:
    run = Run(run_id=f"run-{session_id}", agent_id="assistant", session_id=session_id)
    run.emit(ASSISTANT_OUTPUT_EVENT, {"message": {"content": output_text}})
    run.mark_completed(output_text)
    return run


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
        name: str | None = None,
    ) -> None:
        self.id = channel_id
        self.guild = guild
        self.parent_id = parent_id
        self.name = name
        self.recipient = SimpleNamespace(id=recipient_id) if recipient_id is not None else None
        self.sent: list[dict[str, Any]] = []
        self.history_messages: list[Any] = []
        self.history_calls: list[dict[str, Any]] = []
        self.fetch_message = AsyncMock(side_effect=LookupError("message unavailable"))
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
        id=CHANNEL_ID,
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


@dataclass
class DiscordHarness:
    """A Discord adapter wired to a fake Gateway client and a recording trigger service."""

    adapter: DiscordChannelAdapter
    sessions: ChatSessionManager
    trigger: AsyncMock
    reserve_waiting_work: Mock
    access: MemoryChannelAccessRegistry
    client: FakeClient
    channel: FakeChannel

    async def receive(self, message: Any) -> None:
        """Deliver one Gateway message the way the client's on_message callback does."""
        await self.adapter._handle_inbound_message(message)

    async def drain(self) -> None:
        """Wait until the channel's admitted work was processed."""
        await drain(self.adapter._engine, self.channel.id)

    def notes(self) -> list[str]:
        """Return the observed-context notes stored in the channel's Session."""
        session = self.sessions.get(session_address(self.channel.id))
        return [
            message.content
            for message in session.load()
            if message.role == "note" and isinstance(message.content, str)
        ]


def make_adapter(
    tmp_path: Path,
    *,
    target: FakeChannel,
    allowed_chat_ids: list[int | str] | None = None,
    admin_user_ids: list[int | str] | None = None,
    response_mode: str = "mention",
    observe_unaddressed: bool = False,
    trigger_run: AsyncMock | None = None,
    attachment_store: AttachmentStore | None = None,
    command_dispatcher: Any | None = None,
    waiting_work_manager: ChatRunManager | None = None,
) -> DiscordHarness:
    """Build a connected Discord adapter whose client knows only ``target``.

    Without ``trigger_run``, every triggered Run completes with the reply "ok".
    """
    chat_sessions = ChatSessionManager(tmp_path)
    trigger = trigger_run or AsyncMock(
        side_effect=lambda _agent, _content, session_id, **_kwargs: make_completed_run(session_id)
    )
    trigger_service = make_trigger_service(trigger, waiting_work_manager=waiting_work_manager)
    access = MemoryChannelAccessRegistry([str(user_id) for user_id in admin_user_ids or []])
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
        command_dispatcher=cast(Any, command_dispatcher or make_command_dispatcher()),
        conversation_pointers=channel_state(tmp_path, CHANNEL_ID),
        access_registry=access,
    )
    client = FakeClient([target])
    # Connected state that start() establishes once the Gateway reports ready.
    adapter._client = client
    adapter._bot_id = "999"
    return DiscordHarness(
        adapter=adapter,
        sessions=chat_sessions,
        trigger=trigger,
        reserve_waiting_work=trigger_service.reserve_waiting_work,
        access=access,
        client=client,
        channel=target,
    )
