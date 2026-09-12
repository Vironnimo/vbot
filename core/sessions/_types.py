"""Session addresses, metadata keys and immutable read records."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.runs import RunExecutionOwner

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage


JsonObject = dict[str, Any]
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
SESSION_TITLE_KEY = "title"
SESSION_AUTO_TITLE_KEY = "auto_title"
SESSION_AUTO_TITLE_INITIALIZED_KEY = "auto_title_initialized"
SESSION_TITLE_MAX_LENGTH = 200
SESSION_TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})
FORK_SOURCE_META_KEY = "fork_source"
SESSION_RUN_KINDS_META_KEY = "run_kinds"
PROMPT_CACHE_AFFINITY_META_KEY = "prompt_cache_affinity_id"
SESSION_FORK_ALWAYS_STRIP_META_KEYS = frozenset(
    {
        "source_channel_id",
        "platform",
        "platform_conv_id",
        "last_reply_target",
        "is_subagent_session",
        "subagent_parent",
        "reflection_counters",
        SESSION_RUN_KINDS_META_KEY,
    }
)
SESSION_FORK_CROSS_AGENT_STRIP_META_KEYS = frozenset(
    {"pinned_skill_catalog", "seen_skills", PROMPT_CACHE_AFFINITY_META_KEY}
)
SESSION_MOVE_STRIP_META_KEYS = SESSION_FORK_CROSS_AGENT_STRIP_META_KEYS
SKILL_CONTEXT_NOTE_PREFIX = "[skill-context] "
SKILL_TOOL_MESSAGE_NAME = "skill"
SKILL_TOOL_LOADED_STATUS = "loaded"
PROJECT_TOOL_MESSAGE_NAME = "project"
PROJECT_TOOL_LOADED_STATUS = "loaded"
CHANNEL_MESSAGE_NOTE_PREFIX = "[channel-message] "
SKILL_AVAILABLE_NOTE_PREFIX = "[skill-available] "
_CHAT_HISTORY_CURSOR_PREFIX = "vh1."


@dataclass(frozen=True)
class SessionAddress:
    project_id: str | None
    agent_id: str
    session_id: str


@dataclass(frozen=True)
class SessionIdentityReferenceUpdate:
    address: SessionAddress
    previous_metadata: JsonObject


@dataclass(frozen=True)
class TemporarySessionBinding:
    address: SessionAddress
    generation_id: str
    owner_name: str
    group_id: str
    participant_id: str
    config: JsonObject


@dataclass(frozen=True)
class DeliveryReceipt:
    receipt_id: str
    content_hash: str
    effect_kind: str
    carrier_location: JsonObject


@dataclass(frozen=True)
class OwnedRunRecord:
    """Canonical execution attribution; never part of copied Message context."""

    record_key: int
    address: SessionAddress
    generation_id: str
    run_id: str
    owner: RunExecutionOwner
    start_sequence: int
    terminal_status: str | None
    terminal_sequence: int | None
    input_id: str | None = None


@dataclass(frozen=True)
class RunStartBoundary:
    """One canonical owner-backed Run start for exact derived projections."""

    address: SessionAddress
    generation_id: str
    run_id: str
    start_sequence: int


@dataclass(frozen=True)
class SessionReadCursor:
    generation_id: str
    history_revision: int
    next_seq: int
    message_count: int
    last_message_id: str | None


@dataclass(frozen=True)
class SessionReadBatch:
    messages: tuple[ChatMessage, ...]
    cursor: SessionReadCursor


@dataclass(frozen=True)
class SessionMessagePage:
    messages: tuple[ChatMessage, ...]
    has_more: bool
    editable_message_ids: frozenset[str] = frozenset()
    before_cursor: str | None = None


@dataclass(frozen=True)
class SessionChatHistorySnapshot:
    page: SessionMessagePage
    session_usage: JsonObject
    context_messages: tuple[ChatMessage, ...]
    background_messages: tuple[ChatMessage, ...]


@dataclass(frozen=True)
class SessionStatusSnapshot:
    first_message_at: str | None
    user_message_count: int
    latest_assistant_usage: JsonObject | None
    session_usage: JsonObject
    cache_input_tokens: int


@dataclass(frozen=True)
class SessionRunResult:
    assistant: ChatMessage | None
    summary: ChatMessage
    latest_tool_name: str | None


@dataclass(frozen=True)
class SessionHistoryCheckpoint:
    ordinal: int
    sequence: int
    message_id: str
    timestamp: str
    summary: str


@dataclass(frozen=True)
class SessionHistorySnapshot:
    generation_id: str
    checkpoints: tuple[SessionHistoryCheckpoint, ...]

    @property
    def latest(self) -> SessionHistoryCheckpoint:
        return self.checkpoints[-1]


@dataclass(frozen=True)
class SessionHistoryRecord:
    sequence: int
    message: ChatMessage


@dataclass(frozen=True)
class SessionHistorySectionStats:
    eligible_count: int
    start_timestamp: str | None
    end_timestamp: str | None


@dataclass(frozen=True)
class SessionDescriptorSource:
    metadata: JsonObject
    message_count: int
    first_user_message: ChatMessage | None


@dataclass(frozen=True)
class SessionListFilters:
    include_subagents: bool = True
    include_memory_reflections: bool = True
    include_skill_reflections: bool = True
    include_cron: bool = True


@dataclass(frozen=True)
class SessionListCursor:
    active_sort: float
    project_id: str | None
    agent_id: str
    session_id: str


@dataclass(frozen=True)
class SessionListPage:
    sessions: tuple[JsonObject, ...]
    next_cursor: SessionListCursor | None
    total_count: int
