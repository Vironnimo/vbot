"""Conversation transport contract and admitted Channel work payloads."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from core.channels.adapter import (
    ConversationFacts,
    MessageFacts,
    QuotedMessageFacts,
)
from core.chat.commands import (
    PreparedCommand,
)
from core.chat.content_blocks import ContentBlock
from core.runs import (
    WaitingWorkAdmission,
)

if TYPE_CHECKING:
    pass


class ConversationTransport(Protocol):
    """Platform I/O surface the engine drives.

    An adapter or its transport component implements this; the engine stays free of platform
    libraries. Raw platform
    messages are opaque to the engine and only ``build_media_blocks`` understands them.
    """

    @property
    def platform_display_name(self) -> str:
        """Human-facing platform name used verbatim in reply and reminder text."""

    async def send_text(
        self,
        platform_target: str,
        text: str,
        *,
        reply_to_message_id: str | None = None,
        thread_id: str | None = None,
    ) -> None:
        """Deliver one outbound text reply, optionally referencing a message/thread."""

    def activity_indicator(
        self,
        platform_target: str,
        thread_id: str | None = None,
    ) -> contextlib.AbstractAsyncContextManager[None]:
        """Show a best-effort activity indicator for a target until the block exits."""

    async def build_media_blocks(self, raw_message: Any) -> list[ContentBlock]:
        """Convert one raw platform message into canonical content blocks."""

    async def build_quoted_message(self, raw_message: Any) -> QuotedMessageFacts | None:
        """Resolve attachment content from the message referenced by ``raw_message``.

        Called only after the triggering message passed response gating and Queue
        admission, so adapters may perform metadata fetches and attachment downloads.
        """

    def caption_text(self, raw_message: Any) -> str | None:
        """Extract caption text from one raw platform message for gating checks."""


# Queued work carries the ConversationFacts, not a resolved route: the routed session
# is resolved in the per-conversation worker at processing time. Resolving at enqueue
# time would pin messages to a session that a queued /new ahead of them is about to
# move off the conversation anchor (observed messages always resolved late already).
@dataclass(slots=True, frozen=True)
class _QueuedInboundMessage:
    conversation: ConversationFacts
    message: MessageFacts
    # The platform trigger stays opaque until the worker may resolve a replied-to
    # attachment after response gating and waiting-work admission.
    raw_message: Any | None = None
    admission: WaitingWorkAdmission | None = None


@dataclass(slots=True, frozen=True)
class _QueuedPreparedCommand:
    conversation: ConversationFacts
    command: PreparedCommand
    admission: WaitingWorkAdmission | None = None


@dataclass(slots=True, frozen=True)
class _QueuedInboundMedia:
    conversation: ConversationFacts
    # Raw platform messages; conversion to content blocks happens in the per-conversation
    # worker via the transport so the adapter's update pipeline never blocks.
    messages: tuple[Any, ...]
    # Some platforms deliver a user's comment and the media it introduces as separate
    # transport messages. Adapters may reunite that comment with the media before queueing.
    companion_text: str | None = None
    admission: WaitingWorkAdmission | None = None


@dataclass(slots=True, frozen=True)
class _QueuedObservedMessage:
    conversation: ConversationFacts
    note: str
    admission: WaitingWorkAdmission | None = None


@dataclass(slots=True, frozen=True)
class _QueuedInternalPrompt:
    conversation: ConversationFacts
    prompt: str
    admission: WaitingWorkAdmission | None = None


_QueuedWork = (
    _QueuedInboundMessage
    | _QueuedPreparedCommand
    | _QueuedInboundMedia
    | _QueuedObservedMessage
    | _QueuedInternalPrompt
)
