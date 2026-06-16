"""Channel adapter interfaces and routing fact dataclasses."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from core.chat.content_blocks import ContentBlock, FileBlock, MediaBlock, TextBlock

if TYPE_CHECKING:
    from core.attachments import AttachmentRecord


@dataclass(frozen=True)
class ConversationFacts:
    """Facts about where one inbound platform message came from."""

    platform: str
    channel_id: str
    chat_id: str
    user_id: str
    thread_id: str | None = None
    # The adapter classifies the conversation; the engine derives session ids from it.
    # A group conversation routes to a shared session keyed by chat id, ignoring dm_scope.
    kind: Literal["direct", "group"] = "direct"
    # Human-readable platform name of the sender; the engine falls back to user_id.
    user_display_name: str | None = None
    # Platform message id of the inbound message; used for group reply threading.
    message_id: str | None = None
    # Addressing facts supplied by the adapter; the engine owns the gating decision.
    mentioned_bot: bool = False
    is_reply_to_bot: bool = False


@dataclass(frozen=True)
class RouteFacts:
    """Facts about routing one inbound message into the chat system."""

    agent_id: str
    session_id: str


@dataclass(frozen=True)
class ReplyPlanFacts:
    """Facts that define where outbound reply text should be delivered."""

    channel_id: str
    platform_target: str
    # Platform message id replies should reference (group conversations only).
    reply_to_message_id: str | None = None


@dataclass(frozen=True)
class MessageFacts:
    """Facts about the model-visible inbound message payload."""

    content: str | list[ContentBlock]


@dataclass(frozen=True)
class FileData:
    """One outbound file payload prepared for a channel adapter send."""

    filename: str
    media_type: str
    data: bytes


def channel_system_reminder(
    *,
    platform_display_name: str,
    channel_id: str,
    chat_id: str,
) -> str:
    """Build the one-time channel reminder note injected into a channel Session.

    Shared by the conversation engine (new-session note) and the ``session.link_channel``
    RPC so the two never drift.
    """
    return (
        f"This session is receiving messages via {platform_display_name} "
        f"(channel: {channel_id}, chat: {chat_id}).\n"
        f"Respond in a style appropriate for {platform_display_name} messaging."
    )


def content_block_for_attachment(record: AttachmentRecord) -> ContentBlock:
    """Classify one stored inbound attachment into its canonical content block.

    Shared by every channel adapter so inbound-file handling cannot drift between
    platforms: image/audio/video become a MediaBlock (the chat-layer resolver then
    decides native input vs. transcription vs. a path note), text files become an
    extracted TextBlock, and everything else stays a generic FileBlock. This is the
    single classification point regardless of how the platform delivered the file
    (e.g. a Telegram MP3 sent as a "document" is media, not a generic file).
    """
    if record.media_type.startswith(("image/", "audio/", "video/")):
        return MediaBlock(
            type="media",
            attachment_id=record.id,
            filename=record.filename,
            media_type=record.media_type,
        )
    if record.media_type.startswith("text/"):
        return TextBlock(type="text", text=record.text_content or "")
    return FileBlock(
        type="file",
        attachment_id=record.id,
        filename=record.filename,
        media_type=record.media_type,
    )


class ChannelAdapter(ABC):
    """Base class for platform-specific channel adapters."""

    platform: str

    @abstractmethod
    async def start(self) -> None:
        """Start receiving inbound platform events."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop receiving inbound platform events and release resources."""

    @abstractmethod
    async def send(
        self,
        message: str | None,
        platform_target: str,
        *,
        files: list[FileData] | None = None,
    ) -> None:
        """Send one outbound message to a platform target."""

    @abstractmethod
    def ensure_outbound_session(self, platform_target: str) -> RouteFacts:
        """Resolve and ensure the Session that mirrors an outbound target chat.

        Proactive sends (e.g. the ``channel_send`` tool) record outbound context into the
        target chat's Session. This resolves that Session, creating it with channel context
        when it does not exist yet, so the model later sees what was sent proactively.
        """
