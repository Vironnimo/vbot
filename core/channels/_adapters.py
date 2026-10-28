"""Build platform adapters from the Channel domain dependencies."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.attachments import AttachmentStore
from core.channels.adapter import (
    ChannelAdapter,
)
from core.channels.config import (
    ChannelConfig,
    ChannelConfigError,
)
from core.channels.engine import ChannelConversationEngine
from core.channels.mail import InboundMailSink
from core.channels.state import ChannelStateStore
from core.extensions import InteractionEvent, InteractionResponder

if TYPE_CHECKING:
    pass


def create_adapter(
    config: ChannelConfig,
    *,
    engine_for: Callable[[ChannelConfig], ChannelConversationEngine],
    credential_resolver: Callable[[str], str],
    attachment_store: AttachmentStore | None,
    state: ChannelStateStore,
    channel_root: Path,
    chat_migration_persister: Callable[..., None],
    interaction_dispatcher: Callable[[InteractionEvent, InteractionResponder], Awaitable[bool]]
    | None,
    mail_sink: Callable[[], InboundMailSink | None],
) -> ChannelAdapter:
    if config.platform == "email":
        from core.channels.email import EmailChannelAdapter

        return EmailChannelAdapter(
            config,
            engine_for(config),
            credential_resolver,
            attachment_store=attachment_store,
            received_messages=state,
            mail_state=state.email,
            mail_sink=mail_sink,
        )
    if config.platform in {"slack", "mattermost", "whatsapp"}:
        from core.channels.mattermost import MattermostChannelAdapter
        from core.channels.slack import SlackChannelAdapter
        from core.channels.whatsapp import WhatsAppChannelAdapter

        adapter_type: Any = {
            "slack": SlackChannelAdapter,
            "mattermost": MattermostChannelAdapter,
            "whatsapp": WhatsAppChannelAdapter,
        }[config.platform]
        adapter: ChannelAdapter = adapter_type(
            config,
            engine_for(config),
            credential_resolver,
            attachment_store=attachment_store,
            received_messages=state,
            state_dir=channel_root / config.id,
        )
        return adapter
    if config.platform == "discord":
        from core.channels.discord import DiscordChannelAdapter

        return DiscordChannelAdapter(
            config,
            engine_for(config),
            credential_resolver,
            attachment_store=attachment_store,
        )

    if config.platform == "telegram":
        from core.channels.telegram import TelegramChannelAdapter

        return TelegramChannelAdapter(
            config,
            engine_for(config),
            credential_resolver,
            attachment_store=attachment_store,
            chat_migration_persister=partial(chat_migration_persister, config.id),
            interaction_dispatcher=interaction_dispatcher,
            update_offset_store=state,
        )

    raise ChannelConfigError(f"Unsupported channel platform: {config.platform}")
