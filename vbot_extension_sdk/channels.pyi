# Public authoring declarations; runtime identities come from the installed host.
from __future__ import annotations

import asyncio
import contextlib
import typing
from abc import ABC, abstractmethod
from collections.abc import (
    Callable,
    Mapping,
    Sequence,
)
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Literal,
)

from . import InteractionButton as InteractionButton
from . import InteractionEvent as InteractionEvent
from . import InteractionResponder as InteractionResponder

class ChannelAdapter(ABC):
    platform: str

    def observe_connection(self, observer: Callable[[], None]) -> None: ...
    def _report_connected(self) -> None: ...
    def denied_chats(self) -> list[DeniedChatFacts]: ...
    @abstractmethod
    async def start(self) -> None: ...
    @abstractmethod
    async def stop(self) -> None: ...
    @abstractmethod
    async def send(
        self,
        message: str | None,
        platform_target: str,
        *,
        files: list[FileData] | None = None,
        thread_id: str | None = None,
        buttons: list[list[InteractionButton]] | None = None,
    ) -> None: ...
    @abstractmethod
    async def ensure_outbound_session(
        self, platform_target: str, *, thread_id: str | None = None
    ) -> RouteFacts: ...

@dataclass(frozen=True)
class ConversationFacts:
    platform: str
    channel_id: str
    chat_id: str
    user_id: str
    access_scope_id: str | None = None
    sender_role: GroupRole | None = None
    member_tools: ToolAccess | None = None
    thread_id: str | None = None
    kind: Literal["direct", "group"] = "direct"
    user_display_name: str | None = None
    message_id: str | None = None
    mentioned_bot: bool = False
    is_reply_to_bot: bool = False

@dataclass(frozen=True)
class MessageFacts:
    content: str

@dataclass(frozen=True)
class QuotedMessageFacts:
    user_id: str | None
    user_display_name: str | None
    content: list[ContentBlock] | None

@dataclass(frozen=True)
class RouteFacts:
    agent_id: str
    session_id: str

@dataclass(frozen=True)
class ReplyPlanFacts:
    channel_id: str
    platform_target: str
    reply_to_message_id: str | None = None
    thread_id: str | None = None

@dataclass(frozen=True)
class FileData:
    filename: str
    media_type: str
    data: bytes

class DeliveryProgress:
    def __init__(self) -> None: ...
    def delivered(self) -> None: ...
    def __enter__(self) -> DeliveryProgress: ...
    def __exit__(self, _type: object, error: BaseException | None, _traceback: object) -> None: ...

@dataclass(frozen=True)
class DeniedChatFacts:
    chat_id: str
    kind: Literal["direct", "group"]
    display_name: str | None
    last_seen_at: str
    count: int

    def to_dict(self) -> dict[str, object]: ...

class DeniedChatLog:
    def __init__(self, limit: int = 20) -> None: ...
    def record(
        self, *, chat_id: str, kind: Literal["direct", "group"], display_name: str | None
    ) -> bool: ...
    def entries(self) -> list[DeniedChatFacts]: ...

def content_blocks_for_attachment(record: AttachmentRecord) -> list[ContentBlock]: ...

@dataclass(slots=True)
class ChannelConfig:
    id: str
    platform: str
    agent_id: str
    dm_scope: str = "per_conversation"
    allowed_chat_ids: list[str] = ...
    token_env_var: str = ""
    enabled: bool = True
    response_mode: str = "mention"
    mention_patterns: list[str] = ...
    observe_unaddressed: bool = False
    app_token_env_var: str = ""
    server_url: str = ""
    email: EmailSettings | None = None
    settings: dict[str, Any] = ...

    def to_dict(self) -> dict[str, Any]: ...
    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ChannelConfig: ...
    def validate(self) -> None: ...

@dataclass(frozen=True, slots=True)
class EmailSettings:
    address: str
    imap_host: str
    smtp_host: str
    username: str = ""
    imap_port: int = 993
    smtp_port: int = 465
    smtp_security: str = ""
    folders: tuple[str, ...] = ("INBOX",)
    require_sender_authentication: bool = True
    trusted_authserv_ids: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EmailSettings: ...
    def to_dict(self) -> dict[str, Any]: ...
    def updated(self, changes: Mapping[str, Any]) -> dict[str, Any]: ...

class ChannelError(Exception):
    retryable: bool
    retry_after: float | None
    possibly_delivered: bool
    user_message: str

    def __init__(
        self,
        message: str = "",
        *,
        retryable: bool = False,
        retry_after: float | None = None,
        possibly_delivered: bool = False,
        user_message: str | None = None,
    ) -> None: ...

class ChannelConfigError(ChannelError): ...
class ChannelNotFoundError(ChannelError): ...
class ChannelPlatformUnavailableError(ChannelConfigError): ...

@dataclass(frozen=True)
class ChannelAdapterContext:
    engine: ChannelConversationEngine
    credential_resolver: Callable[[str], str]
    attachment_store: AttachmentStore | None
    state_dir: Path

type ChannelAdapterFactory = Callable[[ChannelConfig, ChannelAdapterContext], ChannelAdapter]

class ChannelConversationEngine:
    def __init__(
        self,
        config: ChannelConfig,
        trigger_service: Any,
        chat_sessions: Any,
        *,
        command_dispatcher: Any,
        conversation_pointers: Any,
        pending_replies: Any,
        observation_inbox: Any,
        previous_observations: tuple[asyncio.Task[None], ...] = (),
        run_button_binding_registry: Any | None = None,
        access_registry: Any | None = None,
        tool_catalog: Callable[[], Sequence[Any]] = ...,
    ) -> None: ...
    @property
    def config(self) -> ChannelConfig: ...
    @property
    def observation_owner(self) -> str: ...
    def attach(self, transport: ConversationTransport) -> None: ...
    def set_connected(self, connected: bool) -> None: ...
    def start(self) -> None: ...
    async def ensure_channel_session(self, conversation: ConversationFacts) -> RouteFacts: ...
    async def migrate_group_conversation(self, old_chat_id: str, new_chat_id: str) -> bool: ...
    def should_respond(
        self, conversation: ConversationFacts, gating_texts: Sequence[str | None] = ()
    ) -> bool: ...
    def is_command(self, message_text: str) -> bool: ...
    async def handle_inbound_text(
        self,
        conversation: ConversationFacts,
        message_text: str,
        *,
        raw_message: Any | None = None,
        observed_context: Sequence[tuple[ConversationFacts, str]] = (),
    ) -> bool: ...
    async def handle_inbound_media(
        self,
        conversation: ConversationFacts,
        raw_messages: tuple[Any, ...],
        *,
        companion_text: str | None = None,
        observed_context: Sequence[tuple[ConversationFacts, str]] = (),
    ) -> bool: ...
    async def trigger_internal_reply(
        self, conversation: ConversationFacts, prompt: str
    ) -> bool: ...
    async def trigger_interaction_reply(
        self, conversation: ConversationFacts, event: InteractionEvent
    ) -> Any: ...
    async def relay_run(self, run: Any, reply_plan: ReplyPlanFacts) -> None: ...
    async def stop(self) -> None: ...
    async def recover_observations(self) -> None: ...

class ConversationTransport(typing.Protocol):
    @property
    def platform_display_name(self) -> str: ...
    async def send_text(
        self,
        platform_target: str,
        text: str,
        *,
        reply_to_message_id: str | None = None,
        thread_id: str | None = None,
        buttons: list[list[InteractionButton]] | None = None,
    ) -> None: ...
    def activity_indicator(
        self, platform_target: str, thread_id: str | None = None
    ) -> contextlib.AbstractAsyncContextManager[None]: ...
    async def build_media_blocks(self, raw_message: Any) -> list[ContentBlock]: ...
    async def build_quoted_message(self, raw_message: Any) -> QuotedMessageFacts | None: ...
    def caption_text(self, raw_message: Any) -> str | None: ...
    async def delete_message(self, platform_target: str, message_id: str) -> bool: ...

type ContentBlock = TextBlock | MediaBlock | FileBlock | FileMentionBlock

@dataclass(frozen=True)
class TextBlock:
    type: Literal["text"]
    text: str

@dataclass(frozen=True)
class MediaBlock:
    type: Literal["media"]
    attachment_id: str
    filename: str
    media_type: str
    image_reference: int | None = None
    transcription: str | None = None

@dataclass(frozen=True)
class FileBlock:
    type: Literal["file"]
    attachment_id: str
    filename: str
    media_type: str

@dataclass(frozen=True)
class FileMentionBlock:
    type: Literal["file_mention"]
    path: str
    status: str
    text: str | None
    size_bytes: int | None

class AttachmentStore:
    def __init__(self, data_dir: Path, *, max_size_bytes: int = 20971520) -> None: ...
    def collect_session_orphans(self, sessions: Any) -> int: ...
    @property
    def max_size_bytes(self) -> int: ...
    def ensure_within_limit(self, reported_size_bytes: int | None) -> None: ...
    def store(self, filename: str, data: bytes) -> AttachmentRecord: ...
    async def store_async(self, filename: str, data: bytes) -> AttachmentRecord: ...
    def get(self, attachment_id: str) -> AttachmentRecord: ...
    async def get_async(self, attachment_id: str) -> AttachmentRecord: ...
    def set_transcription(self, attachment_id: str, transcription: str) -> AttachmentRecord: ...
    def delete(self, attachment_id: str) -> None: ...

@dataclass(frozen=True)
class AttachmentRecord:
    id: str
    filename: str
    media_type: str
    size_bytes: int
    stored_at: str
    file_path: str
    transcription: str | None = None

type GroupRole = Literal["admin", "member"]

@dataclass(frozen=True, slots=True)
class ToolAccess:
    mode: str = "all"
    allowed: tuple[str, ...] = ()
    denied: tuple[str, ...] = ()
    granted: tuple[str, ...] = ()
    fixed: bool = False
    live_call: bool = False

    def to_dict(self) -> dict[str, Any]: ...
