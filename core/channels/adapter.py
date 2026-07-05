"""Channel adapter interfaces and routing fact dataclasses."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from core.chat.content_blocks import ContentBlock, FileBlock, MediaBlock, TextBlock

if TYPE_CHECKING:
    from core.attachments import AttachmentRecord

# Denied inbound chats are kept for operator visibility only; the bound keeps the
# in-memory log small under spam while still covering every realistic setup flow.
DENIED_CHAT_LOG_LIMIT = 20


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
class DeniedChatFacts:
    """One inbound chat that was rejected by a channel's allowlist.

    Recorded so operators can discover a chat's platform id without third-party
    tooling: message the bot once, read the id from channel status, allow it.
    """

    chat_id: str
    kind: Literal["direct", "group"]
    display_name: str | None
    last_seen_at: str
    count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "chat_id": self.chat_id,
            "kind": self.kind,
            "display_name": self.display_name,
            "last_seen_at": self.last_seen_at,
            "count": self.count,
        }


class DeniedChatLog:
    """Bounded in-memory record of inbound chats rejected by the allowlist.

    Purely observational: recording a denial never changes gating. The log lives
    with the adapter instance, so allowing a chat (which restarts the adapter)
    naturally clears its entry.
    """

    def __init__(self, limit: int = DENIED_CHAT_LOG_LIMIT) -> None:
        self._limit = limit
        # Dict insertion order doubles as recency order: recording an existing chat
        # re-inserts it at the end. Timestamps are display data only — ordering by
        # them would be ambiguous when the clock resolution makes two ties.
        self._entries: dict[str, DeniedChatFacts] = {}

    def record(
        self,
        *,
        chat_id: str,
        kind: Literal["direct", "group"],
        display_name: str | None,
    ) -> bool:
        """Record one denied inbound message; return True when the chat is new."""
        now = datetime.now(UTC).isoformat()
        existing = self._entries.pop(chat_id, None)
        if existing is not None:
            self._entries[chat_id] = replace(
                existing,
                # A denied chat can gain a usable name later (e.g. first denial
                # came from a payload without one); never regress to None.
                display_name=display_name or existing.display_name,
                last_seen_at=now,
                count=existing.count + 1,
            )
            return False

        if len(self._entries) >= self._limit:
            oldest_chat_id = next(iter(self._entries))
            del self._entries[oldest_chat_id]
        self._entries[chat_id] = DeniedChatFacts(
            chat_id=chat_id,
            kind=kind,
            display_name=display_name,
            last_seen_at=now,
            count=1,
        )
        return True

    def entries(self) -> list[DeniedChatFacts]:
        """Return denied chats, most recently seen first."""
        return list(reversed(self._entries.values()))


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
    # Platform thread/topic replies should land in (Telegram forum topics). Adapters
    # whose thread targets are separate platform targets (Discord) ignore it.
    thread_id: str | None = None


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


def content_blocks_for_attachment(record: AttachmentRecord) -> list[ContentBlock]:
    """Classify one stored inbound attachment into its canonical content blocks.

    Shared by every channel adapter so inbound-file handling cannot drift between
    platforms. image/audio/video become a MediaBlock (the chat-layer resolver then
    decides native input vs. transcription vs. a path note). A text file becomes a
    FileBlock reference — which the resolver renders as a path note, so the agent can
    forward or reopen the original — plus a TextBlock with the extracted content; a
    text file with no extracted content yields the reference alone. Everything else
    stays a generic FileBlock. This is the single classification point regardless of
    how the platform delivered the file (e.g. a Telegram MP3 sent as a "document" is
    media, not a generic file).
    """
    if record.media_type.startswith(("image/", "audio/", "video/")):
        return [
            MediaBlock(
                type="media",
                attachment_id=record.id,
                filename=record.filename,
                media_type=record.media_type,
            )
        ]
    file_block = FileBlock(
        type="file",
        attachment_id=record.id,
        filename=record.filename,
        media_type=record.media_type,
    )
    if record.media_type.startswith("text/") and record.text_content:
        return [file_block, TextBlock(type="text", text=record.text_content)]
    return [file_block]


class ChannelAdapter(ABC):
    """Base class for platform-specific channel adapters."""

    platform: str

    def denied_chats(self) -> list[DeniedChatFacts]:
        """Return recently denied inbound chats for status/discovery surfaces.

        Adapters that gate inbound messages by allowlist override this with their
        ``DeniedChatLog`` entries; the default keeps adapters without inbound
        gating valid.
        """
        return []

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
        thread_id: str | None = None,
    ) -> None:
        """Send one outbound message to a platform target.

        ``thread_id`` addresses a thread/topic inside the target where the platform
        models topics as sub-addresses of one chat (Telegram forum topics). Adapters
        whose threads are their own platform targets (Discord) ignore it.
        """

    @abstractmethod
    def ensure_outbound_session(self, platform_target: str) -> RouteFacts:
        """Resolve and ensure the Session that mirrors an outbound target chat.

        Proactive sends (e.g. the ``channel_send`` tool) record outbound context into the
        target chat's Session. This resolves that Session, creating it with channel context
        when it does not exist yet, so the model later sees what was sent proactively.
        """
