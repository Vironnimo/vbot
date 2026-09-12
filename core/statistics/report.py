"""Immutable Statistics report records and their JSON projection."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from core.statistics.skills import (
    SkillsSection,
)

JsonObject = dict[str, Any]


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
