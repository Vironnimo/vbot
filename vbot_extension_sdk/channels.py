"""Channel platform contracts, inbound conversation services and content values."""

from typing import Any

from ._exports import exports_for, resolve

_EXPORTS = exports_for(
    **{
        "core.channels.adapter": (
            "ChannelAdapter",
            "ConversationFacts",
            "MessageFacts",
            "QuotedMessageFacts",
            "RouteFacts",
            "ReplyPlanFacts",
            "FileData",
            "DeliveryProgress",
            "DeniedChatFacts",
            "DeniedChatLog",
            "content_blocks_for_attachment",
        ),
        "core.channels.config": (
            "ChannelConfig",
            "EmailSettings",
            "ChannelError",
            "ChannelConfigError",
            "ChannelNotFoundError",
            "ChannelPlatformUnavailableError",
        ),
        "core.channels.platforms": ("ChannelAdapterContext", "ChannelAdapterFactory"),
        "core.channels.engine": ("ChannelConversationEngine", "ConversationTransport"),
        "core.chat.content_blocks": (
            "ContentBlock",
            "TextBlock",
            "MediaBlock",
            "FileBlock",
            "FileMentionBlock",
        ),
        "core.extensions.interactions": (
            "InteractionButton",
            "InteractionEvent",
            "InteractionResponder",
        ),
        "core.attachments": ("AttachmentStore", "AttachmentRecord"),
        "core.chat.messages": ("GroupRole",),
        "core.tools.availability": ("ToolAccess",),
    }
)


def __getattr__(name: str) -> Any:
    value = resolve(__name__, _EXPORTS, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = list(_EXPORTS)
