"""Shared fixtures and fakes for discord behavior tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

from core.attachments import AttachmentStore
from core.channels import ChannelConfig
from core.channels.discord import (
    DiscordChannelAdapter,
)
from core.runs import WaitingWorkAdmission
from core.sessions import ChatSessionManager

from .engine_test_support import MemoryChannelAccessRegistry


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
        prepare=Mock(return_value=None),
        unavailability=Mock(return_value=None),
        execute=AsyncMock(),
    )


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
    admin_user_ids: list[int | str] | None = None,
    response_mode: str = "mention",
    observe_unaddressed: bool = False,
    trigger_run: AsyncMock | None = None,
    attachment_store: AttachmentStore | None = None,
    command_dispatcher: Any | None = None,
) -> tuple[DiscordChannelAdapter, ChatSessionManager, AsyncMock, FakeClient]:
    chat_sessions = ChatSessionManager(tmp_path)
    trigger_mock = trigger_run or AsyncMock()

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
        access_registry=MemoryChannelAccessRegistry(
            [str(user_id) for user_id in admin_user_ids or []]
        ),
    )
    client = FakeClient([target])
    adapter._client = client
    adapter._bot_id = "999"
    return adapter, chat_sessions, trigger_mock, client
