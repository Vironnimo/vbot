"""Read-only aggregation over persisted Sessions for the Statistics surface.

This domain computes a full :class:`StatisticsReport` on demand by scanning the
canonical JSONL Sessions that already exist — it adds no persistence of its own
and writes nothing. Every figure is derived from persisted ``ChatMessage`` data
(see ``.vorch/domain-maps/statistics.md``):

- **Runs** come from ``run_summary`` records, which annotate the immediately
  preceding Assistant Run. Assistant/tool messages carry no ``run_id`` of their
  own, so per-run aggregates are obtained by *segmenting* each session's message
  list at ``run_summary`` boundaries: the messages between two consecutive
  ``run_summary`` records form one run group.
- **Real vs estimated tokens are never merged into one "true" number.** ``usage``
  with ``estimated: true`` is accumulated separately from provider-reported
  usage, and the count of estimated assistant turns is reported.
- **Derived fallback** (a run group with ≥2 distinct bare models) is a
  best-effort signal, clearly labelled — it is NOT the authoritative in-memory
  ``model_fallback_activated`` event, which is not persisted.
- **Tool arguments are never read.** Only the tool ``name``, ``timing`` and the
  ``{ok, error.code}`` result envelope feed the report.
"""

from __future__ import annotations

import builtins
import json
import math
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from core.chat.messages import ChatMessage
from core.chat.model_resolution import parse_bare_model
from core.tools import is_tool_result_envelope

JsonObject = dict[str, Any]

# Canonical message roles in stable display order. Reported even when zero so the
# Overview tab never has to guess which roles exist.
MESSAGE_ROLES = (
    "system",
    "user",
    "assistant",
    "tool",
    "note",
    "error",
    "compaction_checkpoint",
    "run_summary",
)
RUN_STATUSES = ("completed", "failed", "cancelled")

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

    Beyond listing projects it enumerates, per project, the agents that own at
    least one session under the project anchor — the single discovery point for
    project-scoped sessions, so statistics never re-derives the anchor layout.
    """

    def list(self) -> builtins.list[_ProjectLike]: ...

    def session_owning_agents(self, project_id: str) -> builtins.list[str]: ...


class SessionHandle(Protocol):
    """One session whose canonical messages can be loaded in append order."""

    def load(self) -> list[ChatMessage]: ...


class SessionSource(Protocol):
    """Path-free session access (satisfied by ``ChatSessionManager``).

    ``project_id`` is the session scope: ``None`` is the global identity layout
    (``agents/<id>/sessions/``); a set value resolves the project anchor
    (``projects/<pid>/agents/<id>/sessions/``). The statistics scan passes it
    through unchanged, so a project session and an identity session never share
    a path even when they share a session id.
    """

    def list_with_metadata(
        self, agent_id: str, project_id: str | None = None
    ) -> list[JsonObject]: ...

    def get(
        self, agent_id: str, session_id: str, project_id: str | None = None
    ) -> SessionHandle: ...


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


@dataclass(frozen=True)
class AgentActivity:
    agent_id: str
    sessions: int
    runs: int
    messages: int
    errors: int
    last_activity: str | None


@dataclass(frozen=True)
class DailyTrendPoint:
    date: str
    runs: int
    errors: int


@dataclass(frozen=True)
class OverviewSection:
    total_agents: int
    total_sessions: int
    total_runs: int
    open_run_groups: int
    total_messages: int
    messages_by_role: dict[str, int]
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
    estimated_input_tokens: int
    estimated_output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int


@dataclass(frozen=True)
class ProviderUsage:
    provider: str
    runs: int
    assistant_messages: int
    measured_input_tokens: int
    measured_output_tokens: int
    estimated_input_tokens: int
    estimated_output_tokens: int
    estimated_turns: int
    errors: int
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
    estimated_input_tokens: int
    estimated_output_tokens: int
    estimated_turns: int
    errors: int
    total_tokens: int
    average_run_duration_ms: float | None


@dataclass(frozen=True)
class UsageDailyPoint:
    date: str
    runs: int
    errors: int
    measured_input_tokens: int
    measured_output_tokens: int
    estimated_input_tokens: int
    estimated_output_tokens: int


@dataclass(frozen=True)
class UsageSection:
    totals: UsageTotals
    providers: list[ProviderUsage]
    models: list[ModelUsage]
    daily: list[UsageDailyPoint]


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
    duration: DurationStats
    runs_with_tool_calls: int
    total_tool_calls: int
    average_tool_calls_per_run: float | None
    derived_fallback_runs: int
    runs_per_agent: list[AgentRunCount]
    top_sessions_by_runs: list[SessionRunCount]
    runs_per_day: list[DailyCount]
    longest_runs: list[LongestRun]


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
    """Full statistics report covering all four Statistics sub-views."""

    generated_at: str
    window: WindowInfo
    overview: OverviewSection
    usage: UsageSection
    runs: RunsSection
    errors: ErrorsSection
    tools: ToolsSection

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
    messages: int = 0
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
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    estimated_turns: int = 0
    errors: int = 0
    run_duration_total_ms: int = 0
    run_duration_count: int = 0


@dataclass
class _ProviderAcc:
    provider: str
    runs: int = 0
    assistant_messages: int = 0
    measured_input_tokens: int = 0
    measured_output_tokens: int = 0
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    estimated_turns: int = 0
    errors: int = 0


@dataclass
class _DailyAcc:
    runs: int = 0
    errors: int = 0
    measured_input_tokens: int = 0
    measured_output_tokens: int = 0
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0


@dataclass
class _ToolAcc:
    name: str
    calls: int = 0
    successes: int = 0
    failures: int = 0
    duration_total_ms: int = 0
    duration_samples: list[int] = field(default_factory=list)
    error_codes: Counter[str] = field(default_factory=Counter)


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
        self._last_activity: str | None = None

        self._run_durations: list[int] = []
        self._status_counts: Counter[str] = Counter()
        self._total_runs = 0
        self._open_run_groups = 0
        self._runs_with_tool_calls = 0
        self._run_tool_calls = 0
        self._derived_fallback_runs = 0
        self._runs_per_session: list[SessionRunCount] = []
        self._longest_runs: list[LongestRun] = []

        self._models: dict[str, _ModelAcc] = {}
        self._providers: dict[str, _ProviderAcc] = {}
        self._daily: dict[str, _DailyAcc] = {}
        self._usage_assistant_messages = 0
        self._usage_measured_turns = 0
        self._usage_estimated_turns = 0
        self._cache_read_tokens = 0
        self._cache_write_tokens = 0

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

    def process_session(self, agent_id: str, session_id: str, messages: list[ChatMessage]) -> None:
        """Accumulate every aggregate for one session's in-window messages."""
        agent = self._agent(agent_id)
        in_window = [message for message in messages if self._in_window(message.timestamp)]

        current: list[ChatMessage] = []
        current_model: str | None = None
        session_runs = 0
        session_tool_calls = 0

        for message in in_window:
            self._role_counts[message.role] += 1
            agent.messages += 1
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

    def _record_usage(self, message: ChatMessage, day: str | None) -> None:
        self._usage_assistant_messages += 1
        key = _provider_model_key(message.model)
        provider = key.split("/", 1)[0] if "/" in key else key
        model = self._model(provider, key)
        provider_acc = self._provider(provider)
        model.assistant_messages += 1
        provider_acc.assistant_messages += 1

        input_tokens, output_tokens, estimated, cache_read, cache_write = _read_usage(message.usage)
        self._cache_read_tokens += cache_read
        self._cache_write_tokens += cache_write
        daily = self._daily_bucket(day)

        if estimated:
            self._usage_estimated_turns += 1
            model.estimated_turns += 1
            provider_acc.estimated_turns += 1
            model.estimated_input_tokens += input_tokens
            model.estimated_output_tokens += output_tokens
            provider_acc.estimated_input_tokens += input_tokens
            provider_acc.estimated_output_tokens += output_tokens
            if daily is not None:
                daily.estimated_input_tokens += input_tokens
                daily.estimated_output_tokens += output_tokens
        else:
            self._usage_measured_turns += 1
            model.measured_input_tokens += input_tokens
            model.measured_output_tokens += output_tokens
            provider_acc.measured_input_tokens += input_tokens
            provider_acc.measured_output_tokens += output_tokens
            if daily is not None:
                daily.measured_input_tokens += input_tokens
                daily.measured_output_tokens += output_tokens

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

        parsed = _parse_timestamp(message.timestamp)
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
            self._daily_bucket(day).runs += 1

    # -- build -------------------------------------------------------------

    def build(self) -> StatisticsReport:
        return StatisticsReport(
            generated_at=datetime.now(UTC).isoformat(),
            window=WindowInfo(
                since=self._since.isoformat() if self._since is not None else None,
                until=self._until.isoformat() if self._until is not None else None,
            ),
            overview=self._build_overview(),
            usage=self._build_usage(),
            runs=self._build_runs(),
            errors=self._build_errors(),
            tools=self._build_tools(),
        )

    def _build_overview(self) -> OverviewSection:
        durations = sorted(self._run_durations)
        agents = [
            AgentActivity(
                agent_id=accumulator.agent_id,
                sessions=accumulator.sessions,
                runs=accumulator.runs,
                messages=accumulator.messages,
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
            total_messages=int(sum(self._role_counts.values())),
            messages_by_role={role: int(self._role_counts.get(role, 0)) for role in MESSAGE_ROLES},
            last_activity=self._last_activity,
            run_status=self._build_status(),
            average_run_duration_ms=_mean(durations),
            median_run_duration_ms=_nearest_rank_percentile(durations, 50),
            runs_with_tool_calls=self._runs_with_tool_calls,
            total_tool_calls=self._tool_total_calls,
            agents=agents,
            daily_trend=[
                DailyTrendPoint(date=date, runs=bucket.runs, errors=bucket.errors)
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
            estimated_input_tokens=sum(
                model.estimated_input_tokens for model in self._models.values()
            ),
            estimated_output_tokens=sum(
                model.estimated_output_tokens for model in self._models.values()
            ),
            cache_read_tokens=self._cache_read_tokens,
            cache_write_tokens=self._cache_write_tokens,
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
                    estimated_input_tokens=bucket.estimated_input_tokens,
                    estimated_output_tokens=bucket.estimated_output_tokens,
                )
                for date, bucket in self._sorted_daily()
            ],
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
            estimated_input_tokens=accumulator.estimated_input_tokens,
            estimated_output_tokens=accumulator.estimated_output_tokens,
            estimated_turns=accumulator.estimated_turns,
            errors=accumulator.errors,
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
            estimated_input_tokens=accumulator.estimated_input_tokens,
            estimated_output_tokens=accumulator.estimated_output_tokens,
            estimated_turns=accumulator.estimated_turns,
            errors=accumulator.errors,
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
        parsed = _parse_timestamp(timestamp)
        if parsed is None:
            return True
        if self._since is not None and parsed < self._since:
            return False
        return not (self._until is not None and parsed > self._until)


class StatisticsService:
    """Compute a full :class:`StatisticsReport` from persisted Sessions.

    Pure read side: the service only reads through the injected session source,
    agent directory, and project directory, and writes nothing. One scan walks
    every session scope — the global identity agents plus every project-scoped
    agent that owns sessions under a project anchor — and visits each session's
    messages exactly once.

    Project sessions feed the same report as identity sessions; a project agent
    appears under its outer address form ``agent@project`` so it stays distinct
    from the bare identity id and from the same agent in another project. Without
    any projects (or with an absent project directory) the report is identical to
    the identity-only scan — project scopes live under a different anchor path,
    so the same session id under both scopes is two different files, never a
    double count.
    """

    def __init__(
        self,
        chat_sessions: SessionSource,
        agents: AgentDirectory,
        projects: ProjectDirectory | None = None,
    ) -> None:
        self._sessions = chat_sessions
        self._agents = agents
        self._projects = projects

    def report(
        self, *, since: datetime | None = None, until: datetime | None = None
    ) -> StatisticsReport:
        """Scan all session scopes once and return the aggregated report."""
        aggregator = _Aggregator(since=since, until=until)
        for agent in self._agents.list():
            self._scan_scope(aggregator, project_id=None, agent_id=agent.id, display_key=agent.id)
        for project_id, agent_id in self._project_scopes():
            display_key = f"{agent_id}{PROJECT_ADDRESS_SEPARATOR}{project_id}"
            self._scan_scope(
                aggregator, project_id=project_id, agent_id=agent_id, display_key=display_key
            )
        return aggregator.build()

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

    def _scan_scope(
        self,
        aggregator: _Aggregator,
        *,
        project_id: str | None,
        agent_id: str,
        display_key: str,
    ) -> None:
        """Aggregate one session scope under its report display key."""
        summaries = self._sessions.list_with_metadata(agent_id, project_id)
        aggregator.register_agent(display_key, summaries)
        for summary in summaries:
            session_id = str(summary["id"])
            messages = self._sessions.get(agent_id, session_id, project_id).load()
            aggregator.process_session(display_key, session_id, messages)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _provider_model_key(model: str | None) -> str:
    """Return the bare ``<provider>/<model-id>`` key, ``unknown`` when absent."""
    if not model:
        return UNKNOWN_MODEL_KEY
    return parse_bare_model(model)


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


def _read_usage(usage: JsonObject | None) -> tuple[int, int, bool, int, int]:
    """Return ``(input, output, estimated, cache_read, cache_write)`` from usage.

    Canonical ``input_tokens`` already includes cached tokens, so cache figures
    are surfaced only as separate informational totals — never added on top.
    """
    if not isinstance(usage, dict):
        return 0, 0, False, 0, 0
    return (
        _non_negative_int(usage.get("input_tokens")),
        _non_negative_int(usage.get("output_tokens")),
        usage.get("estimated") is True,
        _non_negative_int(usage.get("cache_read_tokens")),
        _non_negative_int(usage.get("cache_write_tokens")),
    )


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


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        normalized = value.removesuffix("Z") + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _date_key(timestamp: str) -> str | None:
    parsed = _parse_timestamp(timestamp)
    return parsed.date().isoformat() if parsed is not None else None


def _max_timestamp(current: str | None, candidate: str) -> str:
    if current is None:
        return candidate
    current_parsed = _parse_timestamp(current)
    candidate_parsed = _parse_timestamp(candidate)
    if current_parsed is None:
        return candidate
    if candidate_parsed is None:
        return current
    return candidate if candidate_parsed > current_parsed else current


def _mean(values: list[int]) -> float | None:
    return sum(values) / len(values) if values else None


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
