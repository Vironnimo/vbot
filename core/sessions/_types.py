"""Session addresses, metadata keys and immutable read records."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from core.runs import RunExecutionOwner

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage


JsonObject = dict[str, Any]


@dataclass(frozen=True)
class ToolResultPayload:
    """One JSON payload an Extension attached to its Tool call's result.

    ``payload_id`` is an opaque safe token the Extension hands to the Agent;
    ``owner_name`` names the Extension that alone may load the payload again.
    """

    payload_id: str
    owner_name: str
    payload_json: str


@dataclass(frozen=True)
class ToolResultFacts:
    """What Chat reports about one Tool Result when it persists it.

    ``status`` is the Tool call's terminal status (``completed``, ``failed`` or
    ``cancelled``); the other fields repeat the result envelope's outcome so
    the store never parses Tool output. ``payloads`` are stored with the result
    in the same transaction.
    """

    status: str
    ok: bool | None = None
    error_code: str | None = None
    error_retryable: bool | None = None
    error_attempts: int | None = None
    payloads: tuple[ToolResultPayload, ...] = ()


@dataclass(frozen=True)
class SeenSkillsUpdate:
    """One change to the Skills a Session has seen.

    A Session without a recorded set starts from ``baseline`` (every Skill
    offered so far, announced or not); afterwards only ``added`` is merged in.
    """

    baseline: tuple[str, ...]
    added: tuple[str, ...] = ()


@dataclass(frozen=True)
class PromptEpoch:
    """The prompt state a committed Compaction checkpoint starts.

    ``pins`` sets each named pin slot, ``None`` removing it; slots not named
    keep their value. ``seen_skills``, when given, replaces the seen Skills.
    """

    pins: Mapping[str, JsonObject | None]
    seen_skills: tuple[str, ...] | None = None


@dataclass(frozen=True)
class SessionRunAdmission:
    """One Run to admit into a Session before it writes output.

    ``owner`` binds an Extension-owned execution to the Session's temporary
    binding in the same transaction; ``input_id`` names the owner input the Run
    answers, at most once per Session.
    """

    run_id: str
    run_kind: str
    started_at: str
    work_id: str | None = None
    contributes_to_activity: bool = True
    owner: RunExecutionOwner | None = None
    input_id: str | None = None


@dataclass(frozen=True)
class SessionRunCompletion:
    run_id: str
    status: str
    timing: JsonObject
    iteration_count: int
    work_id: str | None = None
    change_stats: JsonObject | None = None
    completion_reason: str | None = None
    contributes_to_activity: bool = True


SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
SESSION_TITLE_KEY = "title"
SESSION_AUTO_TITLE_KEY = "auto_title"
SESSION_AUTO_TITLE_INITIALIZED_KEY = "auto_title_initialized"
SESSION_TITLE_MAX_LENGTH = 200
SESSION_TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})
FORK_SOURCE_META_KEY = "fork_source"
SESSION_RUN_KINDS_META_KEY = "run_kinds"
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
# Prompt pin slots rendered from the Session's Agent: its Skill catalog, SOUL
# block and pinned-memory text. A fork or move into another scope drops them,
# together with the seen Skills, and starts a new prompt-cache affinity, so the
# destination renders its own Agent's snapshots instead of the source's.
AGENT_BOUND_PROMPT_PIN_SLOTS = frozenset(
    {"pinned_skill_catalog", "pinned_soul_context", "pinned_memory_files"}
)
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
    previous_parent: JsonObject
    updated_parent: JsonObject


@dataclass(frozen=True)
class TemporarySessionBinding:
    address: SessionAddress
    generation_id: str
    owner_name: str
    group_id: str
    participant_id: str
    config: JsonObject


@dataclass(frozen=True)
class OwnedSessionSummary:
    """One live owner-managed Session projected for derived consumers.

    ``summary`` has the same normalized shape as ``list_summaries``. The group
    title and participant labels are display values; they never grant access.
    """

    address: SessionAddress
    owner_name: str
    group_id: str
    group_title: str | None
    participant_id: str
    participant_name: str | None
    model: str | None
    summary: JsonObject


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
class SessionReadCursor:
    """Where a reader stopped: the next sequence and the entry just before it."""

    generation_id: str
    history_revision: int
    next_seq: int
    last_message_id: str | None


@dataclass(frozen=True)
class SessionReadBatch:
    """Records read after a cursor, from one snapshot.

    ``messages`` are the Session's own new entries in seq order, superseded
    ones included (the audit view). ``active_messages`` is the current view
    after the cursor: all of it on a full load. Its first ``inherited_count``
    Messages precede the Session's fork point, so a fork's own conversation is
    ``active_messages[inherited_count:]``.
    """

    messages: tuple[ChatMessage, ...]
    cursor: SessionReadCursor
    active_messages: tuple[ChatMessage, ...] = ()
    inherited_count: int = 0


@dataclass(frozen=True)
class SessionEditResult:
    """What one committed history edit produced.

    ``batch`` is the Session's complete own audit and current view after the
    edit; ``prompt_cache_affinity_id`` names the cache lineage the edit started.
    """

    batch: SessionReadBatch
    prompt_cache_affinity_id: str


@dataclass(frozen=True)
class SessionMessagePage:
    messages: tuple[ChatMessage, ...]
    has_more: bool
    editable_message_ids: frozenset[str] = frozenset()
    before_cursor: str | None = None
    record_sequences: tuple[int, ...] = ()
    record_run_ids: tuple[str | None, ...] = ()


@dataclass(frozen=True)
class SessionBackgroundRecord:
    """One active Note or Tool Result, reduced to what a status fold reads."""

    role: str
    name: str | None
    content: str | None


@dataclass(frozen=True)
class SessionChatHistorySnapshot:
    """One History page and whole-Session facts from a single read.

    ``background_records`` hold the requested background candidates in
    sequence order; an incremental snapshot holds only those appended in its
    page range. ``unchanged`` marks an ``after`` read already at the Session's
    end when the caller asked to skip it: the page is empty, and usage, context
    and background candidates were not read.
    """

    page: SessionMessagePage
    session_usage: JsonObject
    context_messages: tuple[ChatMessage, ...]
    background_records: tuple[SessionBackgroundRecord, ...]
    generation_id: str = ""
    after_cursor: str = ""
    incremental: bool = False
    has_newer: bool = False
    runs: tuple[JsonObject, ...] = ()
    unchanged: bool = False


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
class SessionContinuationStep:
    """Readable output recorded for one Model step, in first-recorded order."""

    run_id: str
    step: int
    reasoning: str
    content: str
    assistant_message_id: str | None
    interrupted: bool


@dataclass(frozen=True)
class SessionContinuationOperation:
    """One recorded Tool Call; ``ok`` is set only once its result completed."""

    tool_call_id: str
    name: str
    run_id: str
    completed: bool
    ok: bool | None


@dataclass(frozen=True)
class SessionContinuationState:
    """The current Continuation state as the store folded its records.

    ``cause`` is set exactly when the chain is inactive (interrupted).
    """

    checkpoint_id: str
    origin_run_id: str
    latest_run_id: str
    cause: str | None
    active: bool
    requests: tuple[Any, ...]
    steps: tuple[SessionContinuationStep, ...]
    operations: tuple[SessionContinuationOperation, ...]


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


# How Session Recall treats one live Session: an ordinary conversation, a
# delegated Sub-Agent Session searched only on request, or an internal Session
# (reflection, system-only) that search never returns.
SessionRecallVisibility = Literal["conversation", "subagent", "hidden"]


def recall_visibilities(*, include_subagents: bool) -> tuple[SessionRecallVisibility, ...]:
    """Return the Recall visibilities one search admits."""
    return ("conversation", "subagent") if include_subagents else ("conversation",)


@dataclass(frozen=True)
class SessionDescriptorSource:
    """What a Recall result needs to describe one live Session."""

    metadata: JsonObject
    recall_visibility: SessionRecallVisibility


@dataclass(frozen=True)
class SessionHistoryRevision:
    """Canonical history version and Recall visibility of one live Session."""

    address: SessionAddress
    generation_id: str
    history_revision: int
    recall_visibility: SessionRecallVisibility


SessionSearchOrder = Literal["relevance", "newest", "oldest"]


@dataclass(frozen=True)
class SessionSearchHit:
    """One active Message whose conversation text matched a Session search.

    ``text`` is the Message's conversation text; ``rank`` is its bm25 rank for
    relevance-ordered FTS searches and ``0.0`` otherwise.
    """

    address: SessionAddress
    message_id: str
    role: str
    timestamp: str
    text: str
    rank: float


@dataclass(frozen=True)
class SessionSearchResult:
    """Exactly matching Messages of one Session search, in the requested order.

    ``complete`` is false when the candidate budget ended before the search
    found its limit or ran out of candidates, so more matches may exist.
    ``method`` names what enumerated candidates; ``fallback_reason`` says why a
    requested FTS search scanned instead: ``fts_unavailable``, ``fts_error``, or
    ``tool_inclusive`` (a Tool-inclusive FTS search found nothing and retried by
    substring).
    """

    hits: tuple[SessionSearchHit, ...]
    complete: bool
    method: Literal["fts", "scan"]
    fallback_reason: str | None = None


@dataclass(frozen=True)
class SessionListFilters:
    include_subagents: bool = True
    include_memory_reflections: bool = True
    include_skill_reflections: bool = True
    include_cron: bool = True
    include_channels: bool = True


@dataclass(frozen=True)
class SessionListCursor:
    last_activity_at: str
    project_id: str | None
    agent_id: str
    session_id: str


@dataclass(frozen=True)
class SessionListPage:
    sessions: tuple[JsonObject, ...]
    next_cursor: SessionListCursor | None
    total_count: int
