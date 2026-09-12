"""Accumulate measured Session facts and build the Statistics report sections."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from core.chat.messages import ChatMessage
from core.sessions import (
    skill_context_note_name,
    skill_tool_activation_name,
)
from core.statistics._accumulators import (
    _AgentAcc,
    _CompactionSessionAcc,
    _DailyAcc,
    _ModelAcc,
    _ProviderAcc,
    _ToolAcc,
)
from core.statistics._cache import (
    _SessionCacheTracker,
)
from core.statistics._measurements import (
    UNKNOWN_MODEL_KEY,
    _count_entries,
    _date_key,
    _distinct_run_models,
    _duration_ms,
    _group_is_open,
    _is_visible_assistant_message,
    _is_visible_chat_message,
    _max_timestamp,
    _mean,
    _nearest_rank_percentile,
    _parse_envelope,
    _provider_model_key,
    _ratio,
    _read_usage,
    _timing_field,
    _usage_nonnegative_int,
)
from core.statistics._sources import (
    _session_activity_messages,
)
from core.statistics.report import (
    AgentActivity,
    AgentRunCount,
    CacheBreakIncident,
    CacheSection,
    CompactionReclaimStats,
    CompactionSessionStat,
    CompactionsSection,
    CompactionStrategyCount,
    DailyCount,
    DailyTrendPoint,
    DurationStats,
    ErrorsSection,
    HourCount,
    JsonObject,
    LongestRun,
    ModelUsage,
    OverviewSection,
    ProviderUsage,
    RunsSection,
    RunStatusCounts,
    SessionCacheUsage,
    SessionRunCount,
    StatisticsReport,
    SuspectedCacheBreaks,
    ToolSessionCount,
    ToolsSection,
    ToolStat,
    UsageDailyPoint,
    UsageSection,
    UsageTotals,
    WindowInfo,
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


# Output lists that would otherwise grow with data volume are bounded to a stable
# top-N; the WebUI does any further top-N / share selection from these.
TOP_LONGEST_RUNS = 10


TOP_SESSIONS = 20


TOP_CACHE_SESSIONS = 20


TOP_CACHE_BREAK_INCIDENTS = 20


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
