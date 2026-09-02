"""Statistics aggregation over a disposable projection of persisted Sessions.

Every figure remains derivable from canonical ``ChatMessage`` Session history, while
an incrementally reconciled SQLite read model keeps unchanged Session content off
the report path (see ``.vorch/domain-maps/statistics.md``):

- **Runs** come from ``run_summary`` records, which annotate the immediately
  preceding Assistant Run. Assistant/tool messages carry no ``run_id`` of their
  own, so per-run aggregates are obtained by *segmenting* each session's message
  list at ``run_summary`` boundaries: the messages between two consecutive
  ``run_summary`` records form one run group.
- **Real vs estimated tokens are never merged into one "true" number.** Each
  input/output field is accumulated by its own provenance; a partially reported
  turn can therefore contribute measured output and estimated input separately.
- **Derived fallback** (a run group with ≥2 distinct bare models) is a
  best-effort signal, clearly labelled — it is NOT the authoritative in-memory
  ``model_fallback_activated`` event, which is not persisted.
- **Fork copies are not new activity.** A forked Session contributes only the
  messages appended after its recorded ``fork_source.message_count`` prefix.
- **Tool arguments are never read.** Only the tool ``name``, ``timing`` and the
  ``{ok, error.code}`` result envelope feed the report.
"""

from __future__ import annotations

import builtins
import json
import math
import sqlite3
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from core.chat.messages import ChatMessage, usage_token_is_estimated
from core.chat.model_resolution import parse_bare_model
from core.sessions import (
    FORK_SOURCE_META_KEY,
    ChatSession,
    SessionAddress,
    SessionNotFoundError,
    skill_context_note_name,
    skill_tool_activation_name,
)
from core.statistics.index import (
    IndexedStatisticsSession,
    StatisticsIndex,
    StatisticsIndexError,
    StatisticsScope,
    statistics_session_key,
)
from core.statistics.skills import (
    SkillInventorySource,
    SkillsSection,
    SkillUsageAccumulator,
    empty_inventory,
    offered_skill_names,
    resolve_inventory,
)
from core.statistics.timestamps import parse_timestamp
from core.tools import is_tool_result_envelope
from core.utils.logging import get_logger

JsonObject = dict[str, Any]
_LOGGER = get_logger("statistics")

# Visible conversation roles stay separate from the full persisted Session
# record vocabulary. User records always count; Assistant records count only
# when they carry non-blank text, excluding Thinking-/Tool-only Model steps.
CHAT_MESSAGE_ROLES = (
    "user",
    "assistant",
)
SESSION_RECORD_ROLES = (
    "system",
    "user",
    "assistant",
    "tool",
    "note",
    "error",
    "compaction_checkpoint",
    "run_summary",
    "agent_takeover",
    "history_edit",
)

UNKNOWN_MODEL_KEY = "unknown"

# Outer address form for a project-scoped agent in the report (GLOSSARY → Project,
# plan requirement: ``agent@projekt`` is the outside spelling). Identity agents
# stay under their bare id; only project sessions get qualified, so "builder"
# stays unambiguous across projects in every per-agent figure.
PROJECT_ADDRESS_SEPARATOR = "@"

# Output lists that would otherwise grow with data volume are bounded to a stable
# top-N; the WebUI does any further top-N / share selection from these.
TOP_LONGEST_RUNS = 10
TOP_SESSIONS = 20
TOP_CACHE_SESSIONS = 20
TOP_CACHE_BREAK_INCIDENTS = 20
MAX_RUN_ACTIVITY = 200

# Prompt-cache-break heuristic (best-effort, derived — the cache-side sibling of
# ``derived_fallback_runs``). A measured turn is evaluated against its
# predecessor only when no legitimate prefix change explains a cache miss; the
# thresholds below keep false positives low rather than catching every break.
CACHE_BREAK_READ_RATIO = 0.5
"""A cache read below this share of the previous turn's prompt is a suspected break."""
CACHE_BREAK_MAX_GAP_SECONDS = 300
"""Provider prompt caches expire after ~5 idle minutes; longer gaps are expected misses."""
CACHE_BREAK_MIN_PREVIOUS_INPUT_TOKENS = 2048
"""Below provider minimum cacheable prompt sizes an empty cache read is legitimate."""
MIN_CACHE_SESSION_TURNS = 2
"""A session needs two cache-reporting turns before its hit rate means anything."""


# ---------------------------------------------------------------------------
# Injected collaborators (minimal Protocols — no service locator, no globals)
# ---------------------------------------------------------------------------


class _AgentLike(Protocol):
    @property
    def id(self) -> str: ...


class AgentDirectory(Protocol):
    """The agent-id source for the scan (satisfied by ``AgentStore``)."""

    def list(self) -> list[_AgentLike]: ...


class _ProjectLike(Protocol):
    @property
    def project_id(self) -> str: ...


class ProjectDirectory(Protocol):
    """The project-scope source for the scan (satisfied by ``ProjectStore``).

    Beyond listing Projects it enumerates, per Project, the Agents that own at
    least one canonical Session — the single discovery point for Project-scoped
    Sessions, so Statistics never knows the storage layout.
    """

    def list(self) -> builtins.list[_ProjectLike]: ...

    def session_owning_agents(self, project_id: str) -> builtins.list[str]: ...


class SessionSource(Protocol):
    """Path-free session access (satisfied by ``ChatSessionManager``).

    ``project_id`` is the Session scope: ``None`` addresses an Identity Agent;
    a set value addresses a Project Agent. Statistics passes the address through
    unchanged, so Project and identity Sessions remain distinct even when their
    Agent and Session ids match.
    """

    data_dir: Path

    def list_summaries(
        self,
        agent_id: str,
        project_id: str | None = None,
        *,
        metadata_keys: Sequence[str] = (),
    ) -> list[JsonObject]: ...

    def get(self, address: SessionAddress) -> ChatSession: ...

    def history_version(self, address: SessionAddress) -> tuple[str, int]: ...

    def list_history_versions(
        self, addresses: Sequence[SessionAddress]
    ) -> dict[SessionAddress, tuple[str, int]]: ...


# ---------------------------------------------------------------------------
# Report tree — frozen dataclasses with only JSON-native field types, so
# ``dataclasses.asdict`` yields a fully JSON-serializable payload.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WindowInfo:
    """Echo of the optional time window applied to time-derived aggregates."""

    since: str | None
    until: str | None


@dataclass(frozen=True)
class RunStatusCounts:
    completed: int
    failed: int
    cancelled: int
    interrupted: int


@dataclass(frozen=True)
class AgentActivity:
    agent_id: str
    sessions: int
    runs: int
    chat_messages: int
    session_records: int
    errors: int
    last_activity: str | None


@dataclass(frozen=True)
class DailyTrendPoint:
    date: str
    runs: int
    completed: int
    failed: int
    cancelled: int
    interrupted: int


@dataclass(frozen=True)
class OverviewSection:
    total_agents: int
    total_sessions: int
    total_runs: int
    open_run_groups: int
    total_chat_messages: int
    chat_messages_by_role: dict[str, int]
    total_session_records: int
    session_records_by_role: dict[str, int]
    last_activity: str | None
    run_status: RunStatusCounts
    average_run_duration_ms: float | None
    median_run_duration_ms: float | None
    runs_with_tool_calls: int
    total_tool_calls: int
    agents: list[AgentActivity]
    daily_trend: list[DailyTrendPoint]


@dataclass(frozen=True)
class UsageTotals:
    assistant_messages: int
    measured_turns: int
    estimated_turns: int
    measured_input_tokens: int
    measured_output_tokens: int
    reasoning_tokens: int
    reasoning_turns: int
    estimated_input_tokens: int
    estimated_output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    # Cache figures are meaningful only against turns that reported cache
    # fields at all: ``cache_turns`` counts those measured turns and
    # ``cache_input_tokens`` sums their input, so a hit rate never paints a
    # provider without cache reporting as 0%.
    cache_turns: int
    cache_input_tokens: int


@dataclass(frozen=True)
class ProviderUsage:
    provider: str
    runs: int
    assistant_messages: int
    measured_input_tokens: int
    measured_output_tokens: int
    reasoning_tokens: int
    reasoning_turns: int
    estimated_input_tokens: int
    estimated_output_tokens: int
    estimated_turns: int
    errors: int
    cache_read_tokens: int
    cache_write_tokens: int
    cache_turns: int
    cache_input_tokens: int
    # measured + estimated, for ranking and share-of-total only — never present
    # this as an authoritative measured figure.
    total_tokens: int


@dataclass(frozen=True)
class ModelUsage:
    provider: str
    model: str
    runs: int
    assistant_messages: int
    measured_input_tokens: int
    measured_output_tokens: int
    reasoning_tokens: int
    reasoning_turns: int
    estimated_input_tokens: int
    estimated_output_tokens: int
    estimated_turns: int
    errors: int
    cache_read_tokens: int
    cache_write_tokens: int
    cache_turns: int
    cache_input_tokens: int
    total_tokens: int
    average_run_duration_ms: float | None


@dataclass(frozen=True)
class UsageDailyPoint:
    date: str
    runs: int
    errors: int
    measured_input_tokens: int
    measured_output_tokens: int
    reasoning_tokens: int
    reasoning_turns: int
    estimated_input_tokens: int
    estimated_output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cache_input_tokens: int


@dataclass(frozen=True)
class SessionCacheUsage:
    """One session's prompt-cache effectiveness over its cache-reporting turns."""

    agent_id: str
    session_id: str
    cache_turns: int
    input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    hit_rate: float
    last_activity: str | None


@dataclass(frozen=True)
class CacheBreakIncident:
    """One suspected prompt-cache break: a turn whose cache read collapsed."""

    agent_id: str
    session_id: str
    timestamp: str
    model: str
    previous_input_tokens: int
    cache_read_tokens: int


@dataclass(frozen=True)
class SuspectedCacheBreaks:
    """Derived cache-break signal — best-effort, never authoritative."""

    evaluated_turns: int
    suspected_turns: int
    incidents: list[CacheBreakIncident]


@dataclass(frozen=True)
class CacheSection:
    """Prompt-cache effectiveness view over measured, cache-reporting turns."""

    lowest_hit_rate_sessions: list[SessionCacheUsage]
    suspected_breaks: SuspectedCacheBreaks


@dataclass(frozen=True)
class UsageSection:
    totals: UsageTotals
    providers: list[ProviderUsage]
    models: list[ModelUsage]
    daily: list[UsageDailyPoint]
    cache: CacheSection


@dataclass(frozen=True)
class DurationStats:
    count: int
    average_ms: float | None
    p50_ms: float | None
    p90_ms: float | None
    p95_ms: float | None


@dataclass(frozen=True)
class LongestRun:
    agent_id: str
    session_id: str
    run_id: str
    status: str
    duration_ms: int
    started_at: str | None
    completed_at: str | None
    models: list[str]


@dataclass(frozen=True)
class RunActivity:
    """One persisted Run overlapping a selected Provider-limit interval."""

    agent_id: str
    session_id: str
    session_title: str | None
    run_id: str
    status: str
    started_at: str
    completed_at: str
    duration_ms: int
    models: list[str]
    tool_calls: int
    measured_input_tokens: int
    measured_output_tokens: int
    estimated_input_tokens: int
    estimated_output_tokens: int


@dataclass(frozen=True)
class RunActivityReport:
    """Bounded Run details for one inspected historical limit interval."""

    generated_at: str
    window: WindowInfo
    total_runs: int
    truncated: bool
    runs: list[RunActivity]

    def to_dict(self) -> JsonObject:
        return asdict(self)


@dataclass(frozen=True)
class AgentRunCount:
    agent_id: str
    runs: int


@dataclass(frozen=True)
class SessionRunCount:
    agent_id: str
    session_id: str
    runs: int


@dataclass(frozen=True)
class DailyCount:
    date: str
    count: int


@dataclass(frozen=True)
class RunsSection:
    total_runs: int
    open_run_groups: int
    status: RunStatusCounts
    cancel_rate: float
    failure_rate: float
    interruption_rate: float
    duration: DurationStats
    runs_with_tool_calls: int
    total_tool_calls: int
    average_tool_calls_per_run: float | None
    agent_messages: int
    model_steps: int
    average_agent_messages_per_run: float | None
    average_model_steps_per_run: float | None
    derived_fallback_runs: int
    runs_per_agent: list[AgentRunCount]
    top_sessions_by_runs: list[SessionRunCount]
    runs_per_day: list[DailyCount]
    longest_runs: list[LongestRun]


@dataclass(frozen=True)
class CompactionStrategyCount:
    strategy: str
    compactions: int


@dataclass(frozen=True)
class CompactionReclaimStats:
    observations: int
    total_tokens: int
    average_tokens: float | None
    p50_tokens: float | None
    p95_tokens: float | None


@dataclass(frozen=True)
class CompactionSessionStat:
    agent_id: str
    session_id: str
    compactions: int
    estimated_reclaimed_tokens: int
    last_compaction: str


@dataclass(frozen=True)
class CompactionsSection:
    total_compactions: int
    sessions_with_compactions: int
    average_per_compacted_session: float | None
    p50_per_compacted_session: float | None
    p95_per_compacted_session: float | None
    max_per_session: int
    by_strategy: list[CompactionStrategyCount]
    reclaim: CompactionReclaimStats
    top_sessions: list[CompactionSessionStat]


@dataclass(frozen=True)
class CountEntry:
    key: str
    count: int


@dataclass(frozen=True)
class HourCount:
    hour: int
    count: int


@dataclass(frozen=True)
class ErrorsSection:
    total_errors: int
    by_kind: list[CountEntry]
    by_provider: list[CountEntry]
    by_model: list[CountEntry]
    by_agent: list[CountEntry]
    by_hour: list[HourCount]
    daily: list[DailyCount]


@dataclass(frozen=True)
class ToolStat:
    name: str
    calls: int
    successes: int
    failures: int
    success_rate: float
    error_rate: float
    average_duration_ms: float | None
    p95_duration_ms: float | None
    top_error_code: str | None
    error_codes: list[CountEntry]


@dataclass(frozen=True)
class ToolSessionCount:
    agent_id: str
    session_id: str
    calls: int


@dataclass(frozen=True)
class ToolsSection:
    total_calls: int
    tools: list[ToolStat]
    by_agent: list[CountEntry]
    top_sessions: list[ToolSessionCount]


@dataclass(frozen=True)
class StatisticsReport:
    """Full statistics report covering every Statistics sub-view."""

    generated_at: str
    window: WindowInfo
    overview: OverviewSection
    usage: UsageSection
    runs: RunsSection
    compactions: CompactionsSection
    errors: ErrorsSection
    tools: ToolsSection
    skills: SkillsSection

    def to_dict(self) -> JsonObject:
        """Return a JSON-serializable dictionary of the whole report."""
        return asdict(self)


# ---------------------------------------------------------------------------
# Mutable per-key accumulators used during the single scan
# ---------------------------------------------------------------------------


@dataclass
class _AgentAcc:
    agent_id: str
    sessions: int = 0
    runs: int = 0
    chat_messages: int = 0
    session_records: int = 0
    errors: int = 0
    last_activity: str | None = None


@dataclass
class _ModelAcc:
    provider: str
    model: str
    runs: int = 0
    assistant_messages: int = 0
    measured_input_tokens: int = 0
    measured_output_tokens: int = 0
    reasoning_tokens: int = 0
    reasoning_turns: int = 0
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    estimated_turns: int = 0
    errors: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_turns: int = 0
    cache_input_tokens: int = 0
    run_duration_total_ms: int = 0
    run_duration_count: int = 0


@dataclass
class _ProviderAcc:
    provider: str
    runs: int = 0
    assistant_messages: int = 0
    measured_input_tokens: int = 0
    measured_output_tokens: int = 0
    reasoning_tokens: int = 0
    reasoning_turns: int = 0
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    estimated_turns: int = 0
    errors: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_turns: int = 0
    cache_input_tokens: int = 0


@dataclass
class _DailyAcc:
    runs: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    interrupted: int = 0
    errors: int = 0
    measured_input_tokens: int = 0
    measured_output_tokens: int = 0
    reasoning_tokens: int = 0
    reasoning_turns: int = 0
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_input_tokens: int = 0


@dataclass
class _PreviousCacheTurn:
    """The break heuristic's expectation baseline: the last measured turn."""

    input_tokens: int
    timestamp: datetime | None
    model_key: str
    has_cache_data: bool


class _SessionCacheTracker:
    """Per-session prompt-cache accumulation and break detection.

    Walks one session's in-window messages in order. Cache totals cover only
    measured turns that reported cache fields; the break heuristic compares
    each such turn's cache read against the previous turn's prompt size and
    skips every turn with a legitimate reason for a miss — first turn,
    compaction checkpoint, agent takeover, model switch, expired-cache idle
    gap, or a previous prompt below provider minimum cacheable sizes.
    """

    def __init__(self, *, agent_id: str, session_id: str) -> None:
        self._agent_id = agent_id
        self._session_id = session_id
        self.cache_turns = 0
        self.input_tokens = 0
        self.cache_read_tokens = 0
        self.cache_write_tokens = 0
        self.evaluated_turns = 0
        self.incidents: list[CacheBreakIncident] = []
        self._last_activity: str | None = None
        self._previous: _PreviousCacheTurn | None = None

    def observe(self, message: ChatMessage) -> None:
        if message.role in ("compaction_checkpoint", "agent_takeover"):
            # Both legitimately rebuild the prompt prefix — no expectation
            # carries across the boundary.
            self._previous = None
            return
        if message.role != "assistant":
            return
        facts = _read_usage(message.usage)
        if message.usage is None or facts.estimated:
            # Estimated turns give no reliable expectation baseline.
            self._previous = None
            return

        timestamp = parse_timestamp(message.timestamp)
        model_key = _provider_model_key(message.model)
        if facts.has_cache_data:
            self.cache_turns += 1
            self.input_tokens += facts.input_tokens
            self.cache_read_tokens += facts.cache_read
            self.cache_write_tokens += facts.cache_write
            self._last_activity = _max_timestamp(self._last_activity, message.timestamp)
            self._evaluate_break(facts, timestamp, model_key, message.timestamp)
        self._previous = _PreviousCacheTurn(
            input_tokens=facts.input_tokens,
            timestamp=timestamp,
            model_key=model_key,
            has_cache_data=facts.has_cache_data,
        )

    def _evaluate_break(
        self,
        facts: _UsageFacts,
        timestamp: datetime | None,
        model_key: str,
        raw_timestamp: str,
    ) -> None:
        previous = self._previous
        if (
            previous is None
            or not previous.has_cache_data
            or previous.model_key != model_key
            or previous.input_tokens < CACHE_BREAK_MIN_PREVIOUS_INPUT_TOKENS
            or not _within_cache_gap(previous.timestamp, timestamp)
        ):
            return
        self.evaluated_turns += 1
        if facts.cache_read < previous.input_tokens * CACHE_BREAK_READ_RATIO:
            self.incidents.append(
                CacheBreakIncident(
                    agent_id=self._agent_id,
                    session_id=self._session_id,
                    timestamp=raw_timestamp,
                    model=model_key,
                    previous_input_tokens=previous.input_tokens,
                    cache_read_tokens=facts.cache_read,
                )
            )

    def session_record(self) -> SessionCacheUsage | None:
        if self.cache_turns < MIN_CACHE_SESSION_TURNS or self.input_tokens <= 0:
            return None
        return SessionCacheUsage(
            agent_id=self._agent_id,
            session_id=self._session_id,
            cache_turns=self.cache_turns,
            input_tokens=self.input_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens,
            hit_rate=self.cache_read_tokens / self.input_tokens,
            last_activity=self._last_activity,
        )


@dataclass
class _ToolAcc:
    name: str
    calls: int = 0
    successes: int = 0
    failures: int = 0
    duration_total_ms: int = 0
    duration_samples: list[int] = field(default_factory=list)
    error_codes: Counter[str] = field(default_factory=Counter)


@dataclass
class _CompactionSessionAcc:
    agent_id: str
    session_id: str
    compactions: int = 0
    estimated_reclaimed_tokens: int = 0
    last_compaction: str | None = None


# ---------------------------------------------------------------------------
# Aggregator — accumulates over one full scan, then builds the frozen report
# ---------------------------------------------------------------------------


class _Aggregator:
    """Mutable accumulator for one statistics scan."""

    def __init__(self, *, since: datetime | None, until: datetime | None) -> None:
        self._since = since
        self._until = until

        self._agent_order: list[str] = []
        self._agents: dict[str, _AgentAcc] = {}
        self._total_sessions = 0
        self._role_counts: Counter[str] = Counter()
        self._chat_message_role_counts: Counter[str] = Counter()
        self._last_activity: str | None = None

        self._run_durations: list[int] = []
        self._status_counts: Counter[str] = Counter()
        self._total_runs = 0
        self._open_run_groups = 0
        self._runs_with_tool_calls = 0
        self._run_tool_calls = 0
        self._run_agent_messages = 0
        self._run_model_steps = 0
        self._derived_fallback_runs = 0
        self._runs_per_session: list[SessionRunCount] = []
        self._longest_runs: list[LongestRun] = []

        self._total_compactions = 0
        self._compactions_by_strategy: Counter[str] = Counter()
        self._compaction_reclaim_samples: list[int] = []
        self._compactions_by_session: dict[tuple[str, str], _CompactionSessionAcc] = {}

        self._models: dict[str, _ModelAcc] = {}
        self._providers: dict[str, _ProviderAcc] = {}
        self._daily: dict[str, _DailyAcc] = {}
        self._usage_assistant_messages = 0
        self._usage_measured_turns = 0
        self._usage_estimated_turns = 0
        self._reasoning_tokens = 0
        self._reasoning_turns = 0
        self._cache_read_tokens = 0
        self._cache_write_tokens = 0
        self._cache_turns = 0
        self._cache_input_tokens = 0
        self._session_cache_records: list[SessionCacheUsage] = []
        self._cache_break_evaluated_turns = 0
        self._cache_break_incidents: list[CacheBreakIncident] = []

        self._total_errors = 0
        self._error_by_kind: Counter[str] = Counter()
        self._error_by_provider: Counter[str] = Counter()
        self._error_by_model: Counter[str] = Counter()
        self._error_by_agent: Counter[str] = Counter()
        self._error_by_hour: Counter[int] = Counter()

        self._tool_total_calls = 0
        self._tools: dict[str, _ToolAcc] = {}
        self._tool_by_agent: Counter[str] = Counter()
        self._tool_by_session: Counter[tuple[str, str]] = Counter()

        self._skill_usage = SkillUsageAccumulator(since=since, until=until)
        # Bare scope ids the scan actually visited, so the inventory join at build
        # time enumerates only the agents/projects that own sessions — not the
        # whole store.
        self._scanned_agent_ids: set[str] = set()
        self._scanned_project_ids: set[str] = set()

    # -- ingest ------------------------------------------------------------

    def register_agent(self, agent_id: str, summaries: list[JsonObject]) -> None:
        """Record an agent and its session-level structural facts."""
        accumulator = self._agent(agent_id)
        accumulator.sessions = len(summaries)
        self._total_sessions += len(summaries)
        for summary in summaries:
            last_active = summary.get("last_active_at")
            if isinstance(last_active, str):
                accumulator.last_activity = _max_timestamp(accumulator.last_activity, last_active)
                self._last_activity = _max_timestamp(self._last_activity, last_active)

    def process_session(
        self,
        agent_id: str,
        session_id: str,
        messages: list[ChatMessage],
        summary: JsonObject,
    ) -> None:
        """Accumulate every aggregate for one session.

        ``agent_id`` is the report display key (bare id, or ``agent@projekt`` for
        a project Session). ``summary`` is the Session's merged canonical metadata
        (from the narrow Session summary projection), carrying ``created_at`` and
        ``seen_skills``. All non-skills aggregates run over the in-window
        messages; the skills tally is fed the full activation notes and applies
        its own window (offered by session ``created_at``, activated by note
        timestamp).
        """
        agent = self._agent(agent_id)
        activity_messages = _session_activity_messages(messages, summary)
        in_window = [message for message in activity_messages if self._in_window(message.timestamp)]

        current: list[ChatMessage] = []
        current_model: str | None = None
        session_runs = 0
        session_tool_calls = 0
        cache_tracker = _SessionCacheTracker(agent_id=agent_id, session_id=session_id)

        for message in in_window:
            self._role_counts[message.role] += 1
            agent.session_records += 1
            if _is_visible_chat_message(message):
                self._chat_message_role_counts[message.role] += 1
                agent.chat_messages += 1
            cache_tracker.observe(message)
            if message.role == "compaction_checkpoint":
                self._record_compaction(agent_id, session_id, message)
            if message.role == "run_summary":
                self._record_run(agent, agent_id, session_id, current, message)
                current = []
                session_runs += 1
                continue

            self._record_message(agent, current_model, message)
            if message.role == "assistant":
                current_model = _provider_model_key(message.model)
            if message.role == "tool":
                session_tool_calls += 1
            current.append(message)

        if _group_is_open(current):
            self._open_run_groups += 1

        if session_runs:
            self._runs_per_session.append(SessionRunCount(agent_id, session_id, session_runs))
        if session_tool_calls:
            self._tool_by_session[(agent_id, session_id)] += session_tool_calls

        session_cache_record = cache_tracker.session_record()
        if session_cache_record is not None:
            self._session_cache_records.append(session_cache_record)
        self._cache_break_evaluated_turns += cache_tracker.evaluated_turns
        self._cache_break_incidents.extend(cache_tracker.incidents)

        self._record_skill_usage(agent_id, summary, activity_messages)

    def _record_skill_usage(
        self, display_key: str, summary: JsonObject, messages: list[ChatMessage]
    ) -> None:
        created_at = summary.get("created_at")
        # Both activation carriers count: user-trigger notes and loading ``skill``
        # tool results (a (session, skill) pair still counts at most once — the
        # accumulator dedups).
        activations: list[tuple[str, str | None]] = [
            (name, message.timestamp)
            for message in messages
            if (name := skill_context_note_name(message) or skill_tool_activation_name(message))
            is not None
        ]
        self._skill_usage.observe_session(
            display_key=display_key,
            created_at=created_at if isinstance(created_at, str) else None,
            offered_names=offered_skill_names(summary),
            activations=activations,
        )

    def register_scope(self, *, agent_id: str, project_id: str | None) -> None:
        """Record a scanned scope's bare ids for the inventory join at build time."""
        self._scanned_agent_ids.add(agent_id)
        if project_id is not None:
            self._scanned_project_ids.add(project_id)

    # -- per-message accumulation -----------------------------------------

    def _record_message(
        self, agent: _AgentAcc, current_model: str | None, message: ChatMessage
    ) -> None:
        day = _date_key(message.timestamp)

        if message.role == "assistant":
            self._record_usage(message, day)
        elif message.role == "error":
            self._record_error(agent, current_model, message, day)
        elif message.role == "tool":
            self._record_tool(agent, message)

    def _record_compaction(self, agent_id: str, session_id: str, message: ChatMessage) -> None:
        self._total_compactions += 1
        self._compactions_by_strategy[message.compaction_strategy or UNKNOWN_MODEL_KEY] += 1

        key = (agent_id, session_id)
        session = self._compactions_by_session.get(key)
        if session is None:
            session = _CompactionSessionAcc(agent_id=agent_id, session_id=session_id)
            self._compactions_by_session[key] = session
        session.compactions += 1
        session.last_compaction = _max_timestamp(session.last_compaction, message.timestamp)

        before = _usage_nonnegative_int(message.usage, "context_tokens_before")
        after = _usage_nonnegative_int(message.usage, "context_tokens_after")
        if before is None or after is None:
            return
        reclaimed = max(0, before - after)
        session.estimated_reclaimed_tokens += reclaimed
        self._compaction_reclaim_samples.append(reclaimed)

    def _record_usage(self, message: ChatMessage, day: str | None) -> None:
        self._usage_assistant_messages += 1
        key = _provider_model_key(message.model)
        provider = key.split("/", 1)[0] if "/" in key else key
        model = self._model(provider, key)
        provider_acc = self._provider(provider)
        model.assistant_messages += 1
        provider_acc.assistant_messages += 1

        facts = _read_usage(message.usage)
        daily = self._daily_bucket(day)

        if facts.estimated:
            self._usage_estimated_turns += 1
            model.estimated_turns += 1
            provider_acc.estimated_turns += 1
        else:
            self._usage_measured_turns += 1

        if facts.input_estimated:
            model.estimated_input_tokens += facts.input_tokens
            provider_acc.estimated_input_tokens += facts.input_tokens
            if daily is not None:
                daily.estimated_input_tokens += facts.input_tokens
        else:
            model.measured_input_tokens += facts.input_tokens
            provider_acc.measured_input_tokens += facts.input_tokens
            if daily is not None:
                daily.measured_input_tokens += facts.input_tokens

        if facts.output_estimated:
            model.estimated_output_tokens += facts.output_tokens
            provider_acc.estimated_output_tokens += facts.output_tokens
            if daily is not None:
                daily.estimated_output_tokens += facts.output_tokens
        else:
            model.measured_output_tokens += facts.output_tokens
            provider_acc.measured_output_tokens += facts.output_tokens
            if daily is not None:
                daily.measured_output_tokens += facts.output_tokens

        if not facts.output_estimated and facts.has_reasoning_data:
            self._reasoning_tokens += facts.reasoning
            self._reasoning_turns += 1
            for reasoning_acc in (model, provider_acc):
                reasoning_acc.reasoning_tokens += facts.reasoning
                reasoning_acc.reasoning_turns += 1
            if daily is not None:
                daily.reasoning_tokens += facts.reasoning
                daily.reasoning_turns += 1
        if not facts.input_estimated and facts.has_cache_data:
            self._cache_read_tokens += facts.cache_read
            self._cache_write_tokens += facts.cache_write
            self._cache_turns += 1
            self._cache_input_tokens += facts.input_tokens
            for cache_acc in (model, provider_acc):
                cache_acc.cache_turns += 1
                cache_acc.cache_input_tokens += facts.input_tokens
                cache_acc.cache_read_tokens += facts.cache_read
                cache_acc.cache_write_tokens += facts.cache_write
            if daily is not None:
                daily.cache_input_tokens += facts.input_tokens
                daily.cache_read_tokens += facts.cache_read
                daily.cache_write_tokens += facts.cache_write

    def _record_error(
        self,
        agent: _AgentAcc,
        current_model: str | None,
        message: ChatMessage,
        day: str | None,
    ) -> None:
        self._total_errors += 1
        agent.errors += 1
        self._error_by_kind[message.error_kind or UNKNOWN_MODEL_KEY] += 1
        self._error_by_agent[agent.agent_id] += 1

        model_key = current_model or UNKNOWN_MODEL_KEY
        provider = model_key.split("/", 1)[0] if "/" in model_key else model_key
        self._error_by_model[model_key] += 1
        self._error_by_provider[provider] += 1
        if model_key != UNKNOWN_MODEL_KEY:
            self._model(provider, model_key).errors += 1
            self._provider(provider).errors += 1

        parsed = parse_timestamp(message.timestamp)
        if parsed is not None:
            self._error_by_hour[parsed.hour] += 1
        if day is not None:
            self._daily_bucket(day).errors += 1

    def _record_tool(self, agent: _AgentAcc, message: ChatMessage) -> None:
        self._tool_total_calls += 1
        name = message.name or UNKNOWN_MODEL_KEY
        self._tool_by_agent[agent.agent_id] += 1
        tool = self._tool(name)
        tool.calls += 1

        duration = _duration_ms(message.timing)
        if duration is not None:
            tool.duration_total_ms += duration
            tool.duration_samples.append(duration)

        envelope = _parse_envelope(message.content)
        if envelope is None:
            return
        if envelope["ok"]:
            tool.successes += 1
        else:
            tool.failures += 1
            code = envelope["error"]["code"]
            tool.error_codes[code] += 1

    # -- per-run accumulation ---------------------------------------------

    def _record_run(
        self,
        agent: _AgentAcc,
        agent_id: str,
        session_id: str,
        group: list[ChatMessage],
        summary: ChatMessage,
    ) -> None:
        self._total_runs += 1
        agent.runs += 1
        status = summary.status or "completed"
        self._status_counts[status] += 1

        duration = _duration_ms(summary.timing)
        if duration is not None:
            self._run_durations.append(duration)

        models = _distinct_run_models(group)
        if len(models) >= 2:
            self._derived_fallback_runs += 1
        for model_key in models:
            provider = model_key.split("/", 1)[0] if "/" in model_key else model_key
            model = self._model(provider, model_key)
            model.runs += 1
            self._provider(provider).runs += 1
            if duration is not None:
                model.run_duration_total_ms += duration
                model.run_duration_count += 1

        tool_calls = sum(1 for message in group if message.role == "tool")
        self._run_model_steps += sum(1 for message in group if message.role == "assistant")
        self._run_agent_messages += sum(
            1 for message in group if _is_visible_assistant_message(message)
        )
        if tool_calls:
            self._runs_with_tool_calls += 1
            self._run_tool_calls += tool_calls

        if duration is not None:
            self._longest_runs.append(
                LongestRun(
                    agent_id=agent_id,
                    session_id=session_id,
                    run_id=summary.run_id or "",
                    status=status,
                    duration_ms=duration,
                    started_at=_timing_field(summary.timing, "started_at"),
                    completed_at=_timing_field(summary.timing, "completed_at"),
                    models=sorted(models),
                )
            )

        day = _date_key(summary.timestamp)
        if day is not None:
            bucket = self._daily_bucket(day)
            bucket.runs += 1
            if status == "completed":
                bucket.completed += 1
            elif status == "failed":
                bucket.failed += 1
            elif status == "cancelled":
                bucket.cancelled += 1
            elif status == "interrupted":
                bucket.interrupted += 1

    # -- build -------------------------------------------------------------

    def build(self, skill_inventory: SkillInventorySource | None = None) -> StatisticsReport:
        return StatisticsReport(
            generated_at=datetime.now(UTC).isoformat(),
            window=WindowInfo(
                since=self._since.isoformat() if self._since is not None else None,
                until=self._until.isoformat() if self._until is not None else None,
            ),
            overview=self._build_overview(),
            usage=self._build_usage(),
            runs=self._build_runs(),
            compactions=self._build_compactions(),
            errors=self._build_errors(),
            tools=self._build_tools(),
            skills=self._build_skills(skill_inventory),
        )

    def _build_skills(self, skill_inventory: SkillInventorySource | None) -> SkillsSection:
        # No inventory source (existing constructions/tests) → an empty inventory:
        # the section still builds, every observed usage is dropped, and all
        # counts are zero, so the report always carries a valid ``skills`` block.
        if skill_inventory is None:
            return self._skill_usage.build(empty_inventory())
        inventory = resolve_inventory(
            skill_inventory,
            agent_ids=frozenset(self._scanned_agent_ids),
            project_ids=frozenset(self._scanned_project_ids),
        )
        return self._skill_usage.build(inventory)

    def _build_compactions(self) -> CompactionsSection:
        sessions = list(self._compactions_by_session.values())
        counts = sorted(session.compactions for session in sessions)
        reclaim = sorted(self._compaction_reclaim_samples)
        top_sessions = sorted(
            sessions,
            key=lambda session: (
                -session.compactions,
                -session.estimated_reclaimed_tokens,
                session.agent_id,
                session.session_id,
            ),
        )[:TOP_SESSIONS]
        return CompactionsSection(
            total_compactions=self._total_compactions,
            sessions_with_compactions=len(sessions),
            average_per_compacted_session=_mean(counts),
            p50_per_compacted_session=_nearest_rank_percentile(counts, 50),
            p95_per_compacted_session=_nearest_rank_percentile(counts, 95),
            max_per_session=max(counts, default=0),
            by_strategy=[
                CompactionStrategyCount(strategy=strategy, compactions=count)
                for strategy, count in sorted(
                    self._compactions_by_strategy.items(),
                    key=lambda item: (-item[1], item[0]),
                )
            ],
            reclaim=CompactionReclaimStats(
                observations=len(reclaim),
                total_tokens=sum(reclaim),
                average_tokens=_mean(reclaim),
                p50_tokens=_nearest_rank_percentile(reclaim, 50),
                p95_tokens=_nearest_rank_percentile(reclaim, 95),
            ),
            top_sessions=[
                CompactionSessionStat(
                    agent_id=session.agent_id,
                    session_id=session.session_id,
                    compactions=session.compactions,
                    estimated_reclaimed_tokens=session.estimated_reclaimed_tokens,
                    last_compaction=session.last_compaction or "",
                )
                for session in top_sessions
            ],
        )

    def _build_overview(self) -> OverviewSection:
        durations = sorted(self._run_durations)
        agents = [
            AgentActivity(
                agent_id=accumulator.agent_id,
                sessions=accumulator.sessions,
                runs=accumulator.runs,
                chat_messages=accumulator.chat_messages,
                session_records=accumulator.session_records,
                errors=accumulator.errors,
                last_activity=accumulator.last_activity,
            )
            for accumulator in (self._agents[agent_id] for agent_id in self._agent_order)
        ]
        return OverviewSection(
            total_agents=len(self._agent_order),
            total_sessions=self._total_sessions,
            total_runs=self._total_runs,
            open_run_groups=self._open_run_groups,
            total_chat_messages=int(
                sum(self._chat_message_role_counts.get(role, 0) for role in CHAT_MESSAGE_ROLES)
            ),
            chat_messages_by_role={
                role: int(self._chat_message_role_counts.get(role, 0))
                for role in CHAT_MESSAGE_ROLES
            },
            total_session_records=int(sum(self._role_counts.values())),
            session_records_by_role={
                role: int(self._role_counts.get(role, 0)) for role in SESSION_RECORD_ROLES
            },
            last_activity=self._last_activity,
            run_status=self._build_status(),
            average_run_duration_ms=_mean(durations),
            median_run_duration_ms=_nearest_rank_percentile(durations, 50),
            runs_with_tool_calls=self._runs_with_tool_calls,
            total_tool_calls=self._tool_total_calls,
            agents=agents,
            daily_trend=[
                DailyTrendPoint(
                    date=date,
                    runs=bucket.runs,
                    completed=bucket.completed,
                    failed=bucket.failed,
                    cancelled=bucket.cancelled,
                    interrupted=bucket.interrupted,
                )
                for date, bucket in self._sorted_daily()
            ],
        )

    def _build_usage(self) -> UsageSection:
        totals = UsageTotals(
            assistant_messages=self._usage_assistant_messages,
            measured_turns=self._usage_measured_turns,
            estimated_turns=self._usage_estimated_turns,
            measured_input_tokens=sum(
                model.measured_input_tokens for model in self._models.values()
            ),
            measured_output_tokens=sum(
                model.measured_output_tokens for model in self._models.values()
            ),
            reasoning_tokens=self._reasoning_tokens,
            reasoning_turns=self._reasoning_turns,
            estimated_input_tokens=sum(
                model.estimated_input_tokens for model in self._models.values()
            ),
            estimated_output_tokens=sum(
                model.estimated_output_tokens for model in self._models.values()
            ),
            cache_read_tokens=self._cache_read_tokens,
            cache_write_tokens=self._cache_write_tokens,
            cache_turns=self._cache_turns,
            cache_input_tokens=self._cache_input_tokens,
        )
        providers = sorted(
            (self._provider_usage(accumulator) for accumulator in self._providers.values()),
            key=lambda usage: (-usage.total_tokens, usage.provider),
        )
        models = sorted(
            (self._model_usage(accumulator) for accumulator in self._models.values()),
            key=lambda usage: (-usage.total_tokens, usage.model),
        )
        return UsageSection(
            totals=totals,
            providers=providers,
            models=models,
            daily=[
                UsageDailyPoint(
                    date=date,
                    runs=bucket.runs,
                    errors=bucket.errors,
                    measured_input_tokens=bucket.measured_input_tokens,
                    measured_output_tokens=bucket.measured_output_tokens,
                    reasoning_tokens=bucket.reasoning_tokens,
                    reasoning_turns=bucket.reasoning_turns,
                    estimated_input_tokens=bucket.estimated_input_tokens,
                    estimated_output_tokens=bucket.estimated_output_tokens,
                    cache_read_tokens=bucket.cache_read_tokens,
                    cache_write_tokens=bucket.cache_write_tokens,
                    cache_input_tokens=bucket.cache_input_tokens,
                )
                for date, bucket in self._sorted_daily()
            ],
            cache=self._build_cache(),
        )

    def _build_cache(self) -> CacheSection:
        # Worst hit rate first; equal rates surface the bigger session (more
        # tokens paid) before the smaller one.
        sessions = sorted(
            self._session_cache_records,
            key=lambda record: (record.hit_rate, -record.input_tokens, record.session_id),
        )[:TOP_CACHE_SESSIONS]
        incidents = sorted(
            self._cache_break_incidents,
            key=lambda incident: (
                -(incident.previous_input_tokens - incident.cache_read_tokens),
                incident.session_id,
                incident.timestamp,
            ),
        )[:TOP_CACHE_BREAK_INCIDENTS]
        return CacheSection(
            lowest_hit_rate_sessions=sessions,
            suspected_breaks=SuspectedCacheBreaks(
                evaluated_turns=self._cache_break_evaluated_turns,
                suspected_turns=len(self._cache_break_incidents),
                incidents=incidents,
            ),
        )

    def _build_runs(self) -> RunsSection:
        durations = sorted(self._run_durations)
        total = self._total_runs
        runs_per_agent = [
            AgentRunCount(agent_id=agent_id, runs=self._agents[agent_id].runs)
            for agent_id in self._agent_order
            if self._agents[agent_id].runs
        ]
        top_sessions = sorted(
            self._runs_per_session, key=lambda entry: (-entry.runs, entry.session_id)
        )[:TOP_SESSIONS]
        longest = sorted(self._longest_runs, key=lambda run: (-run.duration_ms, run.run_id))[
            :TOP_LONGEST_RUNS
        ]
        return RunsSection(
            total_runs=total,
            open_run_groups=self._open_run_groups,
            status=self._build_status(),
            cancel_rate=_ratio(self._status_counts.get("cancelled", 0), total),
            failure_rate=_ratio(self._status_counts.get("failed", 0), total),
            interruption_rate=_ratio(self._status_counts.get("interrupted", 0), total),
            duration=DurationStats(
                count=len(durations),
                average_ms=_mean(durations),
                p50_ms=_nearest_rank_percentile(durations, 50),
                p90_ms=_nearest_rank_percentile(durations, 90),
                p95_ms=_nearest_rank_percentile(durations, 95),
            ),
            runs_with_tool_calls=self._runs_with_tool_calls,
            total_tool_calls=self._run_tool_calls,
            average_tool_calls_per_run=(self._run_tool_calls / total) if total else None,
            agent_messages=self._run_agent_messages,
            model_steps=self._run_model_steps,
            average_agent_messages_per_run=(self._run_agent_messages / total if total else None),
            average_model_steps_per_run=(self._run_model_steps / total if total else None),
            derived_fallback_runs=self._derived_fallback_runs,
            runs_per_agent=runs_per_agent,
            top_sessions_by_runs=top_sessions,
            runs_per_day=[
                DailyCount(date=date, count=bucket.runs)
                for date, bucket in self._sorted_daily()
                if bucket.runs
            ],
            longest_runs=longest,
        )

    def _build_errors(self) -> ErrorsSection:
        return ErrorsSection(
            total_errors=self._total_errors,
            by_kind=_count_entries(self._error_by_kind),
            by_provider=_count_entries(self._error_by_provider),
            by_model=_count_entries(self._error_by_model),
            by_agent=_count_entries(self._error_by_agent),
            by_hour=[
                HourCount(hour=hour, count=self._error_by_hour.get(hour, 0)) for hour in range(24)
            ],
            daily=[
                DailyCount(date=date, count=bucket.errors)
                for date, bucket in self._sorted_daily()
                if bucket.errors
            ],
        )

    def _build_tools(self) -> ToolsSection:
        tools = sorted(
            (self._tool_stat(accumulator) for accumulator in self._tools.values()),
            key=lambda stat: (-stat.calls, stat.name),
        )
        top_sessions = [
            ToolSessionCount(agent_id=agent_id, session_id=session_id, calls=calls)
            for (agent_id, session_id), calls in self._tool_by_session.most_common(TOP_SESSIONS)
        ]
        return ToolsSection(
            total_calls=self._tool_total_calls,
            tools=tools,
            by_agent=_count_entries(self._tool_by_agent),
            top_sessions=top_sessions,
        )

    def _build_status(self) -> RunStatusCounts:
        return RunStatusCounts(
            completed=self._status_counts.get("completed", 0),
            failed=self._status_counts.get("failed", 0),
            cancelled=self._status_counts.get("cancelled", 0),
            interrupted=self._status_counts.get("interrupted", 0),
        )

    def _provider_usage(self, accumulator: _ProviderAcc) -> ProviderUsage:
        total_tokens = (
            accumulator.measured_input_tokens
            + accumulator.measured_output_tokens
            + accumulator.estimated_input_tokens
            + accumulator.estimated_output_tokens
        )
        return ProviderUsage(
            provider=accumulator.provider,
            runs=accumulator.runs,
            assistant_messages=accumulator.assistant_messages,
            measured_input_tokens=accumulator.measured_input_tokens,
            measured_output_tokens=accumulator.measured_output_tokens,
            reasoning_tokens=accumulator.reasoning_tokens,
            reasoning_turns=accumulator.reasoning_turns,
            estimated_input_tokens=accumulator.estimated_input_tokens,
            estimated_output_tokens=accumulator.estimated_output_tokens,
            estimated_turns=accumulator.estimated_turns,
            errors=accumulator.errors,
            cache_read_tokens=accumulator.cache_read_tokens,
            cache_write_tokens=accumulator.cache_write_tokens,
            cache_turns=accumulator.cache_turns,
            cache_input_tokens=accumulator.cache_input_tokens,
            total_tokens=total_tokens,
        )

    def _model_usage(self, accumulator: _ModelAcc) -> ModelUsage:
        total_tokens = (
            accumulator.measured_input_tokens
            + accumulator.measured_output_tokens
            + accumulator.estimated_input_tokens
            + accumulator.estimated_output_tokens
        )
        average = (
            accumulator.run_duration_total_ms / accumulator.run_duration_count
            if accumulator.run_duration_count
            else None
        )
        return ModelUsage(
            provider=accumulator.provider,
            model=accumulator.model,
            runs=accumulator.runs,
            assistant_messages=accumulator.assistant_messages,
            measured_input_tokens=accumulator.measured_input_tokens,
            measured_output_tokens=accumulator.measured_output_tokens,
            reasoning_tokens=accumulator.reasoning_tokens,
            reasoning_turns=accumulator.reasoning_turns,
            estimated_input_tokens=accumulator.estimated_input_tokens,
            estimated_output_tokens=accumulator.estimated_output_tokens,
            estimated_turns=accumulator.estimated_turns,
            errors=accumulator.errors,
            cache_read_tokens=accumulator.cache_read_tokens,
            cache_write_tokens=accumulator.cache_write_tokens,
            cache_turns=accumulator.cache_turns,
            cache_input_tokens=accumulator.cache_input_tokens,
            total_tokens=total_tokens,
            average_run_duration_ms=average,
        )

    def _tool_stat(self, accumulator: _ToolAcc) -> ToolStat:
        samples = sorted(accumulator.duration_samples)
        top_error = accumulator.error_codes.most_common(1)
        return ToolStat(
            name=accumulator.name,
            calls=accumulator.calls,
            successes=accumulator.successes,
            failures=accumulator.failures,
            success_rate=_ratio(accumulator.successes, accumulator.calls),
            error_rate=_ratio(accumulator.failures, accumulator.calls),
            average_duration_ms=(accumulator.duration_total_ms / len(samples) if samples else None),
            p95_duration_ms=_nearest_rank_percentile(samples, 95),
            top_error_code=top_error[0][0] if top_error else None,
            error_codes=_count_entries(accumulator.error_codes),
        )

    # -- accessors ---------------------------------------------------------

    def _agent(self, agent_id: str) -> _AgentAcc:
        accumulator = self._agents.get(agent_id)
        if accumulator is None:
            accumulator = _AgentAcc(agent_id=agent_id)
            self._agents[agent_id] = accumulator
            self._agent_order.append(agent_id)
        return accumulator

    def _model(self, provider: str, model_key: str) -> _ModelAcc:
        accumulator = self._models.get(model_key)
        if accumulator is None:
            accumulator = _ModelAcc(provider=provider, model=model_key)
            self._models[model_key] = accumulator
        return accumulator

    def _provider(self, provider: str) -> _ProviderAcc:
        accumulator = self._providers.get(provider)
        if accumulator is None:
            accumulator = _ProviderAcc(provider=provider)
            self._providers[provider] = accumulator
        return accumulator

    def _tool(self, name: str) -> _ToolAcc:
        accumulator = self._tools.get(name)
        if accumulator is None:
            accumulator = _ToolAcc(name=name)
            self._tools[name] = accumulator
        return accumulator

    def _daily_bucket(self, day: str | None) -> _DailyAcc:
        # Callers guard ``day is None``; the empty-string key keeps the helper
        # total but never appears in the sorted output.
        key = day or ""
        bucket = self._daily.get(key)
        if bucket is None:
            bucket = _DailyAcc()
            self._daily[key] = bucket
        return bucket

    def _sorted_daily(self) -> list[tuple[str, _DailyAcc]]:
        return sorted(
            ((date, bucket) for date, bucket in self._daily.items() if date),
            key=lambda item: item[0],
        )

    def _in_window(self, timestamp: str) -> bool:
        if self._since is None and self._until is None:
            return True
        parsed = parse_timestamp(timestamp)
        if parsed is None:
            return True
        if self._since is not None and parsed < self._since:
            return False
        return not (self._until is not None and parsed > self._until)


class StatisticsService:
    """Compute a full :class:`StatisticsReport` from a disposable Session index.

    The service reconciles canonical Sessions into a compact SQLite projection
    before every read. Unchanged transcripts are never loaded; append-only growth
    validates and ingests only the new tail. A failed or incompatible projection
    is discarded and rebuilt once, with the canonical live scan retained as a
    final availability fallback.

    Project sessions feed the same report as identity sessions; a project agent
    appears under its outer address form ``agent@project`` so it stays distinct
    from the bare identity id and from the same agent in another project. Without
    any projects (or with an absent project directory) the report is identical to
    the identity-only scan — project scopes live under a different anchor path,
    so the same session id under both scopes is two different files, never a
    double count.

    The optional ``skill_inventory`` joins observed skill usage against the
    current skill set (see ``core/statistics/skills.py``). When omitted, the
    skills section still builds — against an empty inventory, so every observed
    usage drops and all counts are zero — keeping existing constructions valid.
    """

    def __init__(
        self,
        chat_sessions: SessionSource,
        agents: AgentDirectory,
        projects: ProjectDirectory | None = None,
        skill_inventory: SkillInventorySource | None = None,
    ) -> None:
        self._sessions = chat_sessions
        self._agents = agents
        self._projects = projects
        self._skill_inventory = skill_inventory
        self._index = StatisticsIndex(Path(chat_sessions.data_dir))

    def warm_index(self) -> None:
        """Reconcile the disposable index without building a report."""
        self._indexed_snapshot(self._statistics_scopes())

    def report(
        self, *, since: datetime | None = None, until: datetime | None = None
    ) -> StatisticsReport:
        """Reconcile all Session scopes and return the aggregated report."""
        aggregator = _Aggregator(since=since, until=until)
        scopes = self._statistics_scopes()
        snapshot = self._indexed_snapshot(scopes)
        if snapshot is None:
            self._scan_live_report(aggregator, scopes)
        else:
            for scope in scopes:
                self._scan_indexed_scope(aggregator, scope, snapshot)
        return aggregator.build(self._skill_inventory)

    def run_activity(
        self,
        *,
        since: datetime,
        until: datetime,
    ) -> RunActivityReport:
        """Return persisted Runs whose execution overlaps the selected interval."""

        runs: list[RunActivity] = []
        scopes = self._statistics_scopes()
        snapshot = self._indexed_snapshot(scopes)
        if snapshot is None:
            for scope in scopes:
                self._scan_run_activity_scope(
                    runs,
                    project_id=scope.project_id,
                    agent_id=scope.agent_id,
                    display_key=scope.display_key,
                    since=since,
                    until=until,
                    summaries=list(scope.summaries),
                )
        else:
            for scope in scopes:
                self._scan_indexed_run_activity_scope(
                    runs,
                    scope,
                    snapshot,
                    since=since,
                    until=until,
                )

        runs.sort(key=lambda run: run.started_at, reverse=True)
        total_runs = len(runs)
        return RunActivityReport(
            generated_at=datetime.now(UTC).isoformat(),
            window=WindowInfo(since=since.isoformat(), until=until.isoformat()),
            total_runs=total_runs,
            truncated=total_runs > MAX_RUN_ACTIVITY,
            runs=runs[:MAX_RUN_ACTIVITY],
        )

    def _project_scopes(self) -> list[tuple[str, str]]:
        """Return ``(project_id, agent_id)`` for every session-owning project agent."""
        if self._projects is None:
            return []
        scopes: list[tuple[str, str]] = []
        for project in self._projects.list():
            project_id = project.project_id
            for agent_id in self._projects.session_owning_agents(project_id):
                scopes.append((project_id, agent_id))
        return scopes

    def _statistics_scopes(self) -> tuple[StatisticsScope, ...]:
        scopes = [
            StatisticsScope(
                project_id=None,
                agent_id=agent.id,
                display_key=agent.id,
                summaries=tuple(
                    self._sessions.list_summaries(
                        agent.id,
                        None,
                        metadata_keys=("seen_skills",),
                    )
                ),
            )
            for agent in self._agents.list()
        ]
        for project_id, agent_id in self._project_scopes():
            scopes.append(
                StatisticsScope(
                    project_id=project_id,
                    agent_id=agent_id,
                    display_key=f"{agent_id}{PROJECT_ADDRESS_SEPARATOR}{project_id}",
                    summaries=tuple(
                        self._sessions.list_summaries(
                            agent_id,
                            project_id,
                            metadata_keys=("seen_skills",),
                        )
                    ),
                )
            )
        return tuple(scopes)

    def _indexed_snapshot(
        self,
        scopes: tuple[StatisticsScope, ...],
    ) -> dict[tuple[str, str, str], IndexedStatisticsSession] | None:
        for attempt in range(2):
            try:
                return self._index.snapshot(self._sessions, scopes)
            except (OSError, sqlite3.DatabaseError, StatisticsIndexError) as error:
                if attempt == 0:
                    _LOGGER.warning(
                        "Statistics index failed; rebuilding once: %s",
                        error,
                    )
                    try:
                        self._index.discard()
                    except OSError as discard_error:
                        _LOGGER.warning(
                            "Could not discard failed Statistics index: %s",
                            discard_error,
                        )
                else:
                    _LOGGER.warning(
                        "Statistics index rebuild failed; using canonical Session scan: %s",
                        error,
                    )
        return None

    def _scan_live_report(
        self,
        aggregator: _Aggregator,
        scopes: tuple[StatisticsScope, ...],
    ) -> None:
        for scope in scopes:
            self._scan_scope(
                aggregator,
                project_id=scope.project_id,
                agent_id=scope.agent_id,
                display_key=scope.display_key,
                summaries=list(scope.summaries),
            )

    @staticmethod
    def _scan_indexed_scope(
        aggregator: _Aggregator,
        scope: StatisticsScope,
        snapshot: dict[tuple[str, str, str], IndexedStatisticsSession],
    ) -> None:
        indexed_sessions: list[IndexedStatisticsSession] = []
        for summary in scope.summaries:
            indexed = snapshot.get(
                statistics_session_key(
                    scope.project_id,
                    scope.agent_id,
                    str(summary["id"]),
                )
            )
            if indexed is not None:
                indexed_sessions.append(indexed)
        summaries = [session.summary for session in indexed_sessions]
        aggregator.register_agent(scope.display_key, summaries)
        aggregator.register_scope(agent_id=scope.agent_id, project_id=scope.project_id)
        for session in indexed_sessions:
            summary = _indexed_activity_summary(session.summary)
            aggregator.process_session(
                scope.display_key,
                str(summary["id"]),
                list(session.messages),
                summary,
            )

    def _scan_scope(
        self,
        aggregator: _Aggregator,
        *,
        project_id: str | None,
        agent_id: str,
        display_key: str,
        summaries: list[JsonObject] | None = None,
    ) -> None:
        """Aggregate one session scope under its report display key."""
        resolved_summaries = (
            self._sessions.list_summaries(
                agent_id,
                project_id,
                metadata_keys=("seen_skills",),
            )
            if summaries is None
            else summaries
        )
        live_sessions: list[tuple[JsonObject, list[ChatMessage]]] = []
        for summary in resolved_summaries:
            session_id = str(summary["id"])
            address = SessionAddress(
                project_id=project_id, agent_id=agent_id, session_id=session_id
            )
            try:
                messages = self._sessions.get(address).load()
            except SessionNotFoundError:
                continue
            live_sessions.append((summary, messages))

        aggregator.register_agent(display_key, [summary for summary, _messages in live_sessions])
        aggregator.register_scope(agent_id=agent_id, project_id=project_id)
        for summary, messages in live_sessions:
            session_id = str(summary["id"])
            aggregator.process_session(display_key, session_id, messages, summary)

    def _scan_run_activity_scope(
        self,
        runs: list[RunActivity],
        *,
        project_id: str | None,
        agent_id: str,
        display_key: str,
        since: datetime,
        until: datetime,
        summaries: list[JsonObject] | None = None,
    ) -> None:
        resolved_summaries = (
            self._sessions.list_summaries(
                agent_id,
                project_id,
                metadata_keys=("seen_skills",),
            )
            if summaries is None
            else summaries
        )
        for summary in resolved_summaries:
            session_id = str(summary["id"])
            title = summary.get("title")
            session_title = title if isinstance(title, str) and title else None
            address = SessionAddress(
                project_id=project_id, agent_id=agent_id, session_id=session_id
            )
            try:
                messages = self._sessions.get(address).load()
            except SessionNotFoundError:
                continue
            activity_messages = _session_activity_messages(messages, summary)
            group: list[ChatMessage] = []
            for message in activity_messages:
                if message.role != "run_summary":
                    group.append(message)
                    continue
                activity = _run_activity_record(
                    display_key,
                    session_id,
                    session_title,
                    group,
                    message,
                )
                if _run_overlaps(activity, since=since, until=until):
                    runs.append(activity)
                group = []

    @staticmethod
    def _scan_indexed_run_activity_scope(
        runs: list[RunActivity],
        scope: StatisticsScope,
        snapshot: dict[tuple[str, str, str], IndexedStatisticsSession],
        *,
        since: datetime,
        until: datetime,
    ) -> None:
        for raw_summary in scope.summaries:
            session_id = str(raw_summary["id"])
            indexed = snapshot[statistics_session_key(scope.project_id, scope.agent_id, session_id)]
            summary = _indexed_activity_summary(indexed.summary)
            title = summary.get("title")
            session_title = title if isinstance(title, str) and title else None
            group: list[ChatMessage] = []
            for message in indexed.messages:
                if message.role != "run_summary":
                    group.append(message)
                    continue
                activity = _run_activity_record(
                    scope.display_key,
                    session_id,
                    session_title,
                    group,
                    message,
                )
                if _run_overlaps(activity, since=since, until=until):
                    runs.append(activity)
                group = []


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _indexed_activity_summary(summary: JsonObject) -> JsonObject:
    """Remove fork slicing metadata after the index already omitted that prefix."""
    projected = dict(summary)
    projected.pop(FORK_SOURCE_META_KEY, None)
    return projected


def _run_activity_record(
    agent_id: str,
    session_id: str,
    session_title: str | None,
    group: list[ChatMessage],
    summary: ChatMessage,
) -> RunActivity:
    measured_input = 0
    measured_output = 0
    estimated_input = 0
    estimated_output = 0
    for message in group:
        if message.role != "assistant":
            continue
        facts = _read_usage(message.usage)
        if facts.input_estimated:
            estimated_input += facts.input_tokens
        else:
            measured_input += facts.input_tokens
        if facts.output_estimated:
            estimated_output += facts.output_tokens
        else:
            measured_output += facts.output_tokens

    started_at = _timing_field(summary.timing, "started_at") or summary.timestamp
    completed_at = _timing_field(summary.timing, "completed_at") or summary.timestamp
    return RunActivity(
        agent_id=agent_id,
        session_id=session_id,
        session_title=session_title,
        run_id=summary.run_id or "",
        status=summary.status or "completed",
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=_duration_ms(summary.timing) or 0,
        models=sorted(_distinct_run_models(group)),
        tool_calls=sum(1 for message in group if message.role == "tool"),
        measured_input_tokens=measured_input,
        measured_output_tokens=measured_output,
        estimated_input_tokens=estimated_input,
        estimated_output_tokens=estimated_output,
    )


def _run_overlaps(activity: RunActivity, *, since: datetime, until: datetime) -> bool:
    started_at = parse_timestamp(activity.started_at)
    completed_at = parse_timestamp(activity.completed_at)
    if started_at is None or completed_at is None:
        return False
    return completed_at >= since and started_at <= until


def _provider_model_key(model: str | None) -> str:
    """Return the bare ``<provider>/<model-id>`` key, ``unknown`` when absent."""
    if not model:
        return UNKNOWN_MODEL_KEY
    return parse_bare_model(model)


def _session_activity_messages(
    messages: list[ChatMessage], summary: JsonObject
) -> list[ChatMessage]:
    """Exclude the copied transcript prefix from a fork's activity aggregates.

    ``ChatSessionManager.fork`` records the exact number of copied complete
    messages in ``fork_source.message_count``. Those records remain part of the
    fork's visible history, but their Runs, usage, errors, and Tool calls already
    belong to the source Session. Invalid hand-edited metadata fails open and
    leaves the transcript unchanged rather than silently hiding activity.
    """
    fork_source = summary.get(FORK_SOURCE_META_KEY)
    if not isinstance(fork_source, dict):
        return messages
    copied_message_count = fork_source.get("message_count")
    if (
        isinstance(copied_message_count, bool)
        or not isinstance(copied_message_count, int)
        or copied_message_count < 0
        or copied_message_count > len(messages)
    ):
        return messages
    return messages[copied_message_count:]


def _distinct_run_models(group: list[ChatMessage]) -> set[str]:
    """Return the distinct bare models that produced output in a run group."""
    return {
        _provider_model_key(message.model)
        for message in group
        if message.role == "assistant" and message.model
    }


def _group_is_open(group: list[ChatMessage]) -> bool:
    """Best-effort: a trailing group with conversational activity is unterminated."""
    return any(message.role in ("user", "assistant") for message in group)


def _is_visible_assistant_message(message: ChatMessage) -> bool:
    """Return whether an Assistant record contains user-visible Agent text."""
    return (
        message.role == "assistant"
        and isinstance(message.content, str)
        and bool(message.content.strip())
    )


def _is_visible_chat_message(message: ChatMessage) -> bool:
    """Count visible User messages and Assistant text, not internal Model steps."""
    return message.role == "user" or _is_visible_assistant_message(message)


@dataclass(frozen=True)
class _UsageFacts:
    """Normalized view of one assistant turn's ``usage`` payload."""

    input_tokens: int
    output_tokens: int
    reasoning: int
    input_estimated: bool
    output_estimated: bool
    estimated: bool
    cache_read: int
    cache_write: int
    # Field *presence*, not value: distinguishes "provider reported zero cache"
    # from "provider does not report caching at all".
    has_cache_data: bool
    # Like cache data, field presence distinguishes a reported zero from a
    # Provider that supplied no reasoning-token breakdown.
    has_reasoning_data: bool


def _read_usage(usage: JsonObject | None) -> _UsageFacts:
    """Normalize an assistant turn's usage payload.

    Canonical ``input_tokens`` already includes cached tokens, so cache figures
    are surfaced only as separate informational totals — never added on top.
    """
    if not isinstance(usage, dict):
        return _UsageFacts(0, 0, 0, False, False, False, 0, 0, False, False)
    input_estimated = usage_token_is_estimated(usage, "input_tokens")
    output_estimated = usage_token_is_estimated(usage, "output_tokens")
    return _UsageFacts(
        input_tokens=_non_negative_int(usage.get("input_tokens")),
        output_tokens=_non_negative_int(usage.get("output_tokens")),
        reasoning=_non_negative_int(usage.get("reasoning_tokens")),
        input_estimated=input_estimated,
        output_estimated=output_estimated,
        estimated=input_estimated or output_estimated,
        cache_read=_non_negative_int(usage.get("cache_read_tokens")),
        cache_write=_non_negative_int(usage.get("cache_write_tokens")),
        has_cache_data="cache_read_tokens" in usage or "cache_write_tokens" in usage,
        has_reasoning_data=(
            "reasoning_tokens" in usage
            and _optional_non_negative_int(usage.get("reasoning_tokens")) is not None
        ),
    )


def _within_cache_gap(previous: datetime | None, current: datetime | None) -> bool:
    """Whether two turns are close enough for the cache to still be warm."""
    if previous is None or current is None:
        return False
    gap_seconds = (current - previous).total_seconds()
    return 0 <= gap_seconds <= CACHE_BREAK_MAX_GAP_SECONDS


def _parse_envelope(content: Any) -> JsonObject | None:
    """Parse a tool result envelope; return ``None`` when it is not one.

    Only ``ok`` and ``error.code`` are consumed — tool arguments and result data
    never enter the report.
    """
    if not isinstance(content, str):
        return None
    try:
        parsed = json.loads(content)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict) or not is_tool_result_envelope(parsed):
        return None
    return parsed


def _duration_ms(timing: JsonObject | None) -> int | None:
    if not isinstance(timing, dict):
        return None
    value = timing.get("duration_ms")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _timing_field(timing: JsonObject | None, key: str) -> str | None:
    if not isinstance(timing, dict):
        return None
    value = timing.get(key)
    return value if isinstance(value, str) else None


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _optional_non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _date_key(timestamp: str) -> str | None:
    parsed = parse_timestamp(timestamp)
    return parsed.date().isoformat() if parsed is not None else None


def _max_timestamp(current: str | None, candidate: str) -> str:
    if current is None:
        return candidate
    current_parsed = parse_timestamp(current)
    candidate_parsed = parse_timestamp(candidate)
    if current_parsed is None:
        return candidate
    if candidate_parsed is None:
        return current
    return candidate if candidate_parsed > current_parsed else current


def _mean(values: list[int]) -> float | None:
    return sum(values) / len(values) if values else None


def _usage_nonnegative_int(usage: JsonObject | None, field_name: str) -> int | None:
    if not isinstance(usage, dict):
        return None
    value = usage.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _nearest_rank_percentile(sorted_values: list[int], percentile: float) -> float | None:
    """Nearest-rank percentile of an ascending list (``None`` when empty).

    Rank = ``ceil(p/100 * n)`` (1-indexed), so P50 of ten values is the fifth,
    P90 the ninth, and P95 the tenth — easy to reason about and to test.
    """
    if not sorted_values:
        return None
    count = len(sorted_values)
    rank = math.ceil((percentile / 100) * count)
    index = min(max(rank - 1, 0), count - 1)
    return float(sorted_values[index])


def _ratio(part: int, total: int) -> float:
    return part / total if total else 0.0


def _count_entries(counter: Counter[str]) -> list[CountEntry]:
    return [
        CountEntry(key=key, count=count)
        for key, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    ]
