"""Channel adapter interfaces and routing fact dataclasses."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar

from core.chat.content_blocks import ContentBlock, FileBlock, MediaBlock
from core.chat.messages import GroupRole
from core.extensions import InteractionButton

if TYPE_CHECKING:
    from core.attachments import AttachmentRecord
    from core.runs import Run

# Denied inbound chats are kept for operator visibility only; the bound keeps the
# in-memory log small under spam while still covering every realistic setup flow.
DENIED_CHAT_LOG_LIMIT = 20

_Result = TypeVar("_Result")

BOUND_RUN_CALLBACK_VERSION = "v1"
BOUND_RUN_CALLBACK_PREFIX = f"run:{BOUND_RUN_CALLBACK_VERSION}:"


@dataclass(frozen=True)
class RunButtonBinding:
    """Durable origin for one outbound message's collectively one-shot Run buttons."""

    id: str
    platform_target: str
    thread_id: str | None
    origin_session_id: str
    original_button_data: tuple[str, ...]
    created_at: str
    consumed: bool = False


RunButtonClaimStatus = Literal["claimed", "consumed", "missing", "target_mismatch"]


@dataclass(frozen=True)
class RunButtonClaim:
    """Result of atomically claiming one persisted Run-button binding."""

    status: RunButtonClaimStatus
    binding: RunButtonBinding | None = None


class RunButtonBindingRegistry(Protocol):
    """Persistence seam used by the engine without importing Channel storage.

    The methods block; async callers run them through :meth:`run_async`.
    """

    def claim_run_button_binding(
        self,
        channel_id: str,
        binding_id: str,
        *,
        platform_target: str,
        thread_id: str | None,
    ) -> RunButtonClaim: ...

    def restore_run_button_binding(self, channel_id: str, binding_id: str) -> None: ...

    async def run_async(
        self, function: Callable[..., _Result], *arguments: Any, **keyword_arguments: Any
    ) -> _Result:
        """Run blocking registry work on the worker pool of the registry's database."""
        ...


# Telegram retains undelivered updates for 24 hours and may randomize update ids
# after a week idle. Keep a safety margin over replay retention, below that reset.
TELEGRAM_UPDATE_OFFSET_TTL_SECONDS = 48 * 60 * 60


class UpdateOffsetStore(Protocol):
    """Durable polling-offset seam for long-polling adapters (Telegram).

    Long-polling platforms redeliver unconfirmed updates after an adapter
    restart; without a persisted watermark every restart replays recent inbound
    messages as duplicate Runs. The adapter claims each delivered update id and
    skips ids at or below the persisted high-water mark while it is fresh.
    Storage and live adapters expire idle watermarks so randomized ids remain usable.
    Update ids count per bot, so a watermark applies only to the bot it names;
    another bot's watermark reads as 0.
    """

    def load_update_offset(self, channel_id: str, bot_id: int) -> int: ...

    def save_update_offset(self, channel_id: str, bot_id: int, update_id: int) -> None: ...


class ChannelAccessRegistry(Protocol):
    """Live, platform-neutral group access seam used by the Channel engine.

    ``snapshot_participant_role`` records a sender at ingress and runs off the
    Event Loop; ``role_for`` answers per Tool dispatch without storage I/O.
    """

    async def snapshot_participant_role(
        self,
        channel_id: str,
        access_scope_id: str,
        user_id: str,
        display_name: str,
    ) -> GroupRole: ...

    def role_for(
        self,
        channel_id: str,
        access_scope_id: str,
        user_id: str,
    ) -> GroupRole: ...


def main_conversation_id(channel_id: str) -> str:
    """Return the anchor of a Channel's shared direct conversation (``dm_scope`` "main").

    It is the only conversation anchor that names no platform id, so its pointer
    stays valid when the Channel moves to another platform.
    """
    return f"ch-{channel_id}-main"


class ConversationPointerStore(Protocol):
    """Durable routing pointers from a conversation anchor to its active Session.

    A conversation is identified by its derived anchor Session id. Without a
    pointer the anchor itself is the active Session. The methods block: async
    callers run them through :meth:`run_async`, synchronous callers are worker
    threads.
    """

    def active_session_id(self, channel_id: str, conversation_id: str) -> str | None: ...

    def point_conversation(
        self,
        channel_id: str,
        conversation_id: str,
        conversation_kind: str,
        session_id: str,
    ) -> str | None: ...

    def restore_conversation_pointer(
        self,
        channel_id: str,
        conversation_id: str,
        previous_session_id: str | None,
        *,
        expected_session_id: str,
    ) -> None: ...

    async def run_async(
        self, function: Callable[..., _Result], *arguments: Any, **keyword_arguments: Any
    ) -> _Result:
        """Run blocking pointer work on the worker pool of the store's database."""
        ...


class ReceivedMessageStore(Protocol):
    """Durable inbound receipt window for platforms that redeliver after reconnects."""

    async def has_received(self, channel_id: str, message_ref: str) -> bool: ...

    async def record_received(self, channel_id: str, message_ref: str) -> None: ...


def bound_run_callback_data(binding_id: str, button_index: int) -> str:
    """Return the private callback value for one origin-bound ``run:*`` button."""
    return f"{BOUND_RUN_CALLBACK_PREFIX}{binding_id}:{button_index}"


def parse_bound_run_callback_data(data: str) -> tuple[str, int] | None:
    """Parse the private bound callback form; ordinary ``run:<payload>`` stays unbound."""
    if not data.startswith(BOUND_RUN_CALLBACK_PREFIX):
        return None
    remainder = data[len(BOUND_RUN_CALLBACK_PREFIX) :]
    binding_id, separator, raw_index = remainder.partition(":")
    if not binding_id or not separator or not raw_index.isascii() or not raw_index.isdigit():
        return None
    return binding_id, int(raw_index)


@dataclass(frozen=True)
class ConversationFacts:
    """Facts about where one inbound platform message came from."""

    platform: str
    channel_id: str
    chat_id: str
    user_id: str
    # Stable group authorization scope supplied by the adapter. Telegram topics
    # use their parent chat id; Discord threads use their parent channel id.
    access_scope_id: str | None = None
    # Immutable role snapshot added by the engine when the event enters its Queue.
    sender_role: GroupRole | None = None
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
class QuotedMessageFacts:
    """One replied-to platform message resolved for on-demand attachment ingestion.

    ``content=None`` means the platform exposed a reply reference but the original
    message is no longer available. A non-empty content list belongs to the quoted
    sender, never to the sender of the triggering message.
    """

    user_id: str | None
    user_display_name: str | None
    content: list[ContentBlock] | None


@dataclass(frozen=True)
class FileData:
    """One outbound file payload prepared for a channel adapter send."""

    filename: str
    media_type: str
    data: bytes


def content_blocks_for_attachment(record: AttachmentRecord) -> list[ContentBlock]:
    """Classify one stored inbound attachment into its canonical content blocks.

    Shared by every channel adapter so inbound-file handling cannot drift between
    platforms. image/audio/video become a MediaBlock (the chat-layer resolver then
    decides native input vs. transcription vs. a path note). Text files remain a
    FileBlock too: the chat resolver renders them through the shared read renderer,
    keeping the original local and applying the read tool's 50 KiB, 2,000-line
    boundary. Everything else stays a generic FileBlock. This is the single
    classification point regardless of how the platform delivered the file (e.g. a
    Telegram MP3 sent as a "document" is media, not a generic file).
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
        buttons: list[list[InteractionButton]] | None = None,
    ) -> None:
        """Send one outbound message to a platform target.

        ``thread_id`` addresses a thread/topic inside the target where the platform
        models topics as sub-addresses of one chat (Telegram forum topics). Adapters
        whose threads are their own platform targets (Discord) ignore it.

        ``buttons`` attaches an inline-keyboard (rows of :class:`InteractionButton`)
        to the message so taps come back as channel interaction events. Only
        adapters that support interactive messages honor it; the rest reject a
        non-``None`` value with a clean error.
        """

    async def relay_run(
        self,
        run: Run,
        reply_plan: ReplyPlanFacts,
    ) -> None:
        """Relay one already-admitted background Run through this adapter."""
        raise NotImplementedError

    @abstractmethod
    async def ensure_outbound_session(
        self, platform_target: str, *, thread_id: str | None = None
    ) -> RouteFacts:
        """Resolve and ensure the Session that mirrors an outbound target chat.

        Proactive sends (e.g. the ``channel_send`` tool) record outbound context into the
        target chat's Session. This resolves that Session, creating it with channel context
        when it does not exist yet, so the model later sees what was sent proactively.
        Its Reply Target records the thread/topic used for this send, when provided.
        """
