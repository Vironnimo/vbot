"""Session addresses, metadata keys and immutable read records."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from typing import TYPE_CHECKING, Any, Final, Literal

from core.runs import RunExecutionOwner

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage


JsonObject = dict[str, Any]


@dataclass(frozen=True)
class ToolResultPayload:
    """One JSON payload a Tool attached to its call's result.

    ``payload_id`` is an opaque safe token; ``owner_name`` names the owner that
    alone may load the payload again: the Tool's Extension, or ``tool:<name>``
    for a built-in Tool.
    """

    payload_id: str
    owner_name: str
    payload_json: str


@dataclass(frozen=True)
class ToolResultFacts:
    """What Chat reports about one Tool Result when it persists it.

    ``status`` is the Tool call's terminal status (``completed``, ``failed`` or
    ``cancelled``); the other fields repeat the result envelope's outcome so
    normal Chat writes need no Tool-output parsing. Imported or historical
    Results without scalar facts decode a valid envelope once. ``payloads``
    and the matching ``invocation_key`` settle with the Result transaction.
    """

    status: str
    ok: bool | None = None
    error_code: str | None = None
    error_retryable: bool | None = None
    error_attempts: int | None = None
    payloads: tuple[ToolResultPayload, ...] = ()
    invocation_key: int | None = None


@dataclass(frozen=True)
class SessionToolInvocation:
    """One own Tool dispatch, including work without a stored Result."""

    invocation_key: int
    run_id: str | None
    name: str
    started_at: str
    status: str
    result_ok: bool | None
    error_code: str | None
    duration_ms: int | None
    source_revision: int


@dataclass(frozen=True)
class SessionToolInvocationBatch:
    revision: int
    invocations: tuple[SessionToolInvocation, ...]


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
    answers, at most once per Session. ``expected_generation_id`` requires that
    exact live generation; without it, admission may create a missing Session.
    """

    run_id: str
    run_kind: str
    started_at: str
    work_id: str | None = None
    contributes_to_activity: bool = True
    owner: RunExecutionOwner | None = None
    input_id: str | None = None
    expected_generation_id: str | None = None
    # The working Project a Session this admission creates starts in.
    working_project_id: str | None = None
    # An ordinary Accessor input claimed atomically with this Run's admission.
    accessor_input_id: str | None = None
    schedule_id: str | None = None
    notification_policy: str = "all"
    notification_eligible: bool = False


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
    notification_policy: str | None = None


SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
SESSION_TITLE_KEY = "title"
SESSION_AUTO_TITLE_KEY = "auto_title"
SESSION_AUTO_TITLE_INITIALIZED_KEY = "auto_title_initialized"
SESSION_TITLE_MAX_LENGTH = 200
FORK_SOURCE_META_KEY = "fork_source"
# The Sub-Agent Session marker and its Parent link (``subagent_parent``).
SUBAGENT_SESSION_META_KEY = "is_subagent_session"
SUBAGENT_PARENT_META_KEY = "subagent_parent"
# Historical metadata, retained and stripped on forks.
SUBAGENT_TAKEN_OVER_AT_META_KEY = "subagent_taken_over_at"
SUBAGENT_REPLY_TARGET_META_KEY = "subagent_reply_target"
SESSION_RUN_KINDS_META_KEY = "run_kinds"
# Marks an Ephemeral Session: set when it is created, never current, listed or
# recalled, and deleted permanently after EPHEMERAL_SESSION_LIFETIME without
# activity (owner of the expiry: ``core/archive``).
SESSION_EPHEMERAL_META_KEY = "ephemeral"
EPHEMERAL_SESSION_LIFETIME = timedelta(hours=24)
# The Session's working Project: its address Project, or for a Session of an
# Identity Agent the Project chosen when it was created (absent: the Workspace).
# Sessions manage it; a metadata write may repeat it but never change it.
SESSION_WORKING_PROJECT_META_KEY = "working_project_id"
# The Session goal the user set with /goal (owner: ``core/automation/goals.py``).
# Chat grants the ``update_goal`` Tool while its status is an open one.
SESSION_GOAL_META_KEY = "goal"
SESSION_GOAL_OPEN_STATUSES = frozenset({"active", "blocked", "paused"})
# The System Reminder that sets the goal; Chat keeps the latest one across
# Compaction while the goal is open.
SESSION_GOAL_NOTE_PREFIX = "The user set a goal for this session with /goal:"


class WorkingProjectDefault(Enum):
    """The working Project choice of a creator that names none."""

    AGENT_DEFAULT = "agent_default"


# A new Session of an Identity Agent starts in the Agent's default Project.
AGENT_DEFAULT_PROJECT: Final = WorkingProjectDefault.AGENT_DEFAULT
# The working Project a creator asks for: a Project id, ``None`` for the
# Workspace, or the Agent's default Project.
type WorkingProjectChoice = str | None | Literal[WorkingProjectDefault.AGENT_DEFAULT]
SESSION_FORK_ALWAYS_STRIP_META_KEYS = frozenset(
    {
        "source_channel_id",
        "platform",
        "platform_conv_id",
        "last_reply_target",
        SUBAGENT_SESSION_META_KEY,
        SUBAGENT_PARENT_META_KEY,
        SUBAGENT_TAKEN_OVER_AT_META_KEY,
        SUBAGENT_REPLY_TARGET_META_KEY,
        "reflection_counters",
        "scratch_announced",
        SESSION_RUN_KINDS_META_KEY,
        SESSION_GOAL_META_KEY,
        "fast_mode",
        SESSION_EPHEMERAL_META_KEY,
    }
)
# Prompt pin slots rendered from the Session's Agent: its Skill catalog, SOUL
# block, pinned-memory text, Tool definitions, Tool and Extension prompt block
# texts, thinking effort, and Workspace-less Agent body. A fork or move into another
# scope drops them, together with the seen Skills, and starts a new prompt-cache
# affinity, so the destination renders its own Agent's snapshots instead of the
# source's.
AGENT_BOUND_PROMPT_PIN_SLOTS = frozenset(
    {
        "pinned_skill_catalog",
        "pinned_soul_context",
        "pinned_memory_files",
        "pinned_tool_definitions",
        "pinned_dynamic_blocks",
        "pinned_thinking_effort",
        "pinned_agent_body",
    }
)
SKILL_CONTEXT_NOTE_PREFIX = "[skill-context] "
SKILL_TOOL_MESSAGE_NAME = "skill"
SKILL_TOOL_LOADED_STATUS = "loaded"
PROJECT_TOOL_MESSAGE_NAME = "project"
PROJECT_TOOL_LOADED_STATUS = "loaded"
CHANNEL_MESSAGE_NOTE_PREFIX = "[channel-message] "
SKILL_AVAILABLE_NOTE_PREFIX = "[skill-available] "
# A Tool that became available, was removed or changed after the Session pinned
# its Tool definitions; the JSON payload follows the prefix (``core.chat._tool_epoch``).
TOOL_CHANGE_NOTE_PREFIX = "[tool-change] "
# Tool or Extension prompt blocks that changed after the Session pinned their
# texts; the JSON payload follows the prefix (``core.chat._prompt_block_epoch``).
PROMPT_BLOCK_CHANGE_NOTE_PREFIX = "[prompt-block-change] "
# The thinking effort a Session's requests use from this point of its prompt
# epoch on; the JSON payload follows the prefix (``core.chat._effort_epoch``).
EFFORT_CHANGE_NOTE_PREFIX = "[effort-change] "
_CHAT_HISTORY_CURSOR_PREFIX = "vh1."


@dataclass(frozen=True)
class SessionAddress:
    project_id: str | None
    agent_id: str
    session_id: str


@dataclass(frozen=True)
class AccessorInputRecord:
    """An accepted ordinary Accessor input and its durable execution disposition.

    ``payload`` is opaque Chat-owned execution context; the immutable original
    request hash detects conflicting retries even after a queued input is edited.
    Receipts follow their Session generation through moves and are not inherited.
    """

    input_id: str
    address: SessionAddress
    generation_id: str
    request_hash: str
    payload: JsonObject
    queue_id: str
    state: str
    run_id: str | None
    created_at: str
    live: bool
    run_status: str | None = None
    attachment_ids: tuple[str, ...] = ()


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
class SessionRunRecord:
    """One Run the Session itself admitted, with its canonical completion facts.

    ``status`` is ``running`` until the Run finishes; the completion fields are
    ``None`` while it runs. ``timing_started_at`` is the measured start when the
    Run reported one, else ``None``.
    """

    run_id: str
    run_kind: str
    status: str
    started_at: str
    completed_at: str | None
    duration_ms: int | None
    timing_started_at: str | None
    completion_reason: str | None
    iteration_count: int | None
    changed_files: int | None
    lines_added: int | None
    lines_removed: int | None


@dataclass(frozen=True)
class RecoveredRun:
    """A Run a previous process left running, which restart recovery settled interrupted."""

    address: SessionAddress
    run_id: str
    run_kind: str
    started_at: str
    iteration_count: int


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
    output_speed: JsonObject | None = None
    output_latency: JsonObject | None = None


@dataclass(frozen=True)
class SessionStatusSnapshot:
    """Whole-Session status facts; ``context_messages`` are the Context projection inputs."""

    first_message_at: str | None
    user_message_count: int
    latest_assistant_usage: JsonObject | None
    session_usage: JsonObject
    context_messages: tuple[ChatMessage, ...]


@dataclass(frozen=True)
class SessionRunResult:
    assistant: ChatMessage | None
    summary: ChatMessage
    latest_tool_name: str | None


# How Session Recall treats one live Session: an ordinary conversation, a
# delegated Sub-Agent Session searched only on request, or an internal Session
# (reflection, system-only) that search never returns.
SessionRecallVisibility = Literal["conversation", "subagent", "hidden"]


def recall_visibilities(*, include_subagents: bool) -> tuple[SessionRecallVisibility, ...]:
    """Return the Recall visibilities one search admits."""
    return ("conversation", "subagent") if include_subagents else ("conversation",)


@dataclass(frozen=True)
class SessionWorkingProject:
    """A Session's Project and whether its original Project is still archived.

    An archived binding stays unavailable after its archive is purged, even when
    another Project reuses the public id. Only restoring that original clears it.
    """

    project_id: str | None
    archived: bool = False


@dataclass(frozen=True)
class SubagentLinkSnapshot:
    """Parent-link metadata and its validated generation from one read snapshot."""

    metadata: JsonObject
    parent_generation_id: str | None


@dataclass(frozen=True)
class SessionDescriptorSource:
    """What a Recall result needs to describe one live Session."""

    metadata: JsonObject
    recall_visibility: SessionRecallVisibility


@dataclass(frozen=True)
class SessionConversationMessage:
    """Bounded public conversation text, without Tool payloads or Reasoning."""

    sequence: int
    message_id: str
    role: str
    timestamp: str
    text: str
    truncated: bool
    sender_kind: str | None = None
    sender_id: str | None = None


@dataclass(frozen=True)
class SessionConversationPage:
    """A generation-checked current-view page and its latest task/completion."""

    generation_id: str
    history_revision: int
    next_sequence: int
    cursor_floor_sequence: int
    messages: tuple[SessionConversationMessage, ...]
    latest_user: SessionConversationMessage | None
    latest_message_sequence: int | None
    latest_response_sequence: int | None
    latest_completion_sequence: int | None
    latest_completion_status: str | None
    has_more: bool


@dataclass(frozen=True)
class SessionHistoryRevision:
    """Canonical history version and Recall visibility of one live Session.

    ``creation_order`` increases with every created Session generation, so the
    larger value is the newer Session; projections use it to report inherited
    history for the newest Session that shows it, as store search does.
    """

    address: SessionAddress
    generation_id: str
    history_revision: int
    recall_visibility: SessionRecallVisibility
    creation_order: int


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


@dataclass(frozen=True)
class SessionProjectGrouping:
    """How a Session list groups live Sessions of every Agent by Working Project.

    ``project_ids`` are the registered Projects. A Session working in none of
    them (the Workspace, or a Project no longer registered) belongs to the
    ``None`` group. Global-scope Sessions of ``excluded_agent_ids`` (the
    built-in Agents) are left out of every group.
    """

    project_ids: tuple[str, ...]
    excluded_agent_ids: tuple[str, ...] = ()
