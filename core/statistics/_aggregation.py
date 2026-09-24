"""Aggregate report units in SQL and build the Statistics report sections.

The builder registers units in processing order, lets SQL aggregate their
typed facts (window filters and grouping run on indexed columns), and walks
rows in order only where the report is order-dependent: Run groups, the
prompt-cache heuristic, Compaction recurrence and sequential cost sums.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime

from core.statistics._accumulators import (
    _AgentAcc,
    _DailyAcc,
    _ModelAcc,
    _ProviderAcc,
    _ToolAcc,
)
from core.statistics._cache import CacheFacts, load_cache_facts
from core.statistics._compactions import CompactionAccumulator
from core.statistics._costs import (
    CostAccumulator,
    PricingLookup,
    refresh_retrospective_costs,
)
from core.statistics._extensions import (
    EXTENSION_ACTOR_PREFIX,
    ExtensionSlice,
    ExtensionUsageAccumulator,
)
from core.statistics._measurements import (
    UNKNOWN_MODEL_KEY,
    _count_entries,
    _max_timestamp,
    _mean,
    _nearest_rank_percentile,
    _ratio,
)
from core.statistics._projection import (
    CALL_KIND_COMPACTION,
    MICROSECONDS_PER_DAY,
    MICROSECONDS_PER_HOUR,
    datetime_instant,
    day_key,
)
from core.statistics._units import ReportUnit, UnitScan, max_timestamp_sql
from core.statistics.report import (
    AgentActivity,
    AgentRunCount,
    CacheSection,
    CompactionsSection,
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
    RunActivity,
    RunsSection,
    RunStatusCounts,
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
    resolve_inventory,
)

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


_TOOL_P95 = 95

# Floor-correct UTC hour of a microsecond instant, also before the epoch.
_HOUR_SQL = (
    f"(((e.instant % {MICROSECONDS_PER_DAY}) + {MICROSECONDS_PER_DAY}) "
    f"% {MICROSECONDS_PER_DAY}) / {MICROSECONDS_PER_HOUR}"
)

_MEASURED_INPUT = "SUM(CASE WHEN c.input_estimated = 1 THEN 0 ELSE COALESCE(c.input_tokens, 0) END)"
_ESTIMATED_INPUT = (
    "SUM(CASE WHEN c.input_estimated = 1 THEN COALESCE(c.input_tokens, 0) ELSE 0 END)"
)
_MEASURED_OUTPUT = (
    "SUM(CASE WHEN c.output_estimated = 1 THEN 0 ELSE COALESCE(c.output_tokens, 0) END)"
)
_ESTIMATED_OUTPUT = (
    "SUM(CASE WHEN c.output_estimated = 1 THEN COALESCE(c.output_tokens, 0) ELSE 0 END)"
)
# Reasoning counts only for a measured output with a valid reported breakdown.
_REASONING = "(c.output_estimated = 0 AND c.reasoning_tokens IS NOT NULL)"
# Cache fields count only for a measured prompt that reported them.
_CACHE = "(c.input_estimated = 0 AND c.has_cache = 1)"


class ReportBuilder:
    """Aggregate one report over registered units, then build its sections."""

    def __init__(
        self,
        *,
        since: datetime | None,
        until: datetime | None,
        pricing_lookup: PricingLookup | None = None,
        include_costs: bool = True,
        include_skills: bool = True,
    ) -> None:
        self._since = since
        self._until = until
        self._pricing_lookup = pricing_lookup
        self._include_costs = include_costs
        self._include_skills = include_skills

        self._units: list[ReportUnit] = []
        self._skill_facts: list[tuple[str | None, list[str]]] = []
        self._slices: list[ExtensionSlice | None] = []

        self._costs = CostAccumulator()
        self._compactions = CompactionAccumulator()
        self._cache = CacheFacts()
        self._compaction_calls = 0
        self._unreported_calls = 0
        self._extensions = ExtensionUsageAccumulator(
            windowed=since is not None or until is not None
        )

        self._agent_order: list[str] = []
        self._agents: dict[str, _AgentAcc] = {}
        self._total_sessions = 0
        self._total_records = 0
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

    # -- registration ------------------------------------------------------

    def add_unit(
        self,
        unit: ReportUnit,
        *,
        created_at: str | None = None,
        offered_skills: Sequence[str] = (),
    ) -> None:
        """Queue one surviving unit in processing order.

        ``created_at`` and ``offered_skills`` feed the skills tally, which
        windows offers by Session start rather than by record timestamp.
        """
        self._agent(unit.display_key)
        self._units.append(unit)
        self._skill_facts.append((created_at, list(offered_skills)))
        self._slices.append(
            None if unit.extension is None else self._extensions.slice(unit.extension)
        )

    def register_agent(self, agent_id: str, summaries: Sequence[JsonObject]) -> None:
        """Record an agent and its session-level structural facts."""
        accumulator = self._agent(agent_id)
        accumulator.sessions = len(summaries)
        self._total_sessions += len(summaries)
        for summary in summaries:
            last_active = summary.get("last_active_at")
            if isinstance(last_active, str):
                accumulator.last_activity = _max_timestamp(accumulator.last_activity, last_active)
                self._last_activity = _max_timestamp(self._last_activity, last_active)

    def register_scope(self, *, agent_id: str | None, project_id: str | None) -> None:
        """Record a scanned scope's bare ids for the inventory join at build time.

        Extension-owned Sessions pass no Agent id: their synthetic participant
        Agents own no private Skills, but a Project scope contributes its Skills.
        """
        if agent_id is not None:
            self._scanned_agent_ids.add(agent_id)
        if project_id is not None:
            self._scanned_project_ids.add(project_id)

    # -- aggregation -------------------------------------------------------

    def aggregate(self, connection: sqlite3.Connection) -> None:
        """Aggregate every registered unit from the reconciled index."""
        scan = UnitScan(connection, self._units, since=self._since, until=self._until)
        titles = [unit.title for unit in self._units]
        self._load_records(scan)
        self._load_calls(scan)
        self._load_errors(scan)
        self._load_tools(scan)
        self._load_runs(scan)
        self._compactions.load(scan, titles)
        self._cache = load_cache_facts(scan, top_incidents=TOP_CACHE_BREAK_INCIDENTS)
        self._load_slice_activity(scan)
        if self._include_costs:
            refresh_retrospective_costs(connection, self._pricing_lookup)
            self._costs.load(
                scan,
                titles=titles,
                slices=[None if value is None else value.costs for value in self._slices],
            )
        if self._include_skills:
            self._load_skills(scan)

    def _load_records(self, scan: UnitScan) -> None:
        role_columns = ", ".join(f"SUM(r.role = '{role}')" for role in SESSION_RECORD_ROLES)
        user_column = 2 + SESSION_RECORD_ROLES.index("user")
        for row in scan.execute(
            f"""
            SELECT u.unit, COUNT(*), {role_columns}
            FROM {scan.source("stat_records", "r")}
            WHERE {scan.where("r")}
            GROUP BY u.unit
            """
        ):
            unit, total = row[0], row[1]
            agent = self._agents[self._units[unit].display_key]
            self._total_records += total
            agent.session_records += total
            for role, count in zip(SESSION_RECORD_ROLES, row[2:], strict=True):
                if count:
                    self._role_counts[role] += count
            users = row[user_column]
            self._chat_message_role_counts["user"] += users
            agent.chat_messages += users
            unit_slice = self._slices[unit]
            if unit_slice is not None:
                unit_slice.records += total

    def _load_calls(self, scan: UnitScan) -> None:
        for (
            unit,
            visible,
            calls,
            measured_input,
            estimated_input,
            measured_output,
            estimated_output,
        ) in scan.execute(
            f"""
            SELECT u.unit, SUM(c.kind = 0 AND c.visible = 1), COUNT(*),
                {_MEASURED_INPUT}, {_ESTIMATED_INPUT}, {_MEASURED_OUTPUT}, {_ESTIMATED_OUTPUT}
            FROM {scan.source("stat_calls", "c")}
            WHERE {scan.where("c")}
            GROUP BY u.unit
            """
        ):
            self._chat_message_role_counts["assistant"] += visible
            self._agents[self._units[unit].display_key].chat_messages += visible
            unit_slice = self._slices[unit]
            if unit_slice is not None:
                unit_slice.model_calls += calls
                unit_slice.measured_input_tokens += measured_input
                unit_slice.estimated_input_tokens += estimated_input
                unit_slice.measured_output_tokens += measured_output
                unit_slice.estimated_output_tokens += estimated_output

        for (
            kind,
            model_key,
            day,
            calls,
            estimated_turns,
            measured_turns,
            measured_input,
            estimated_input,
            measured_output,
            estimated_output,
            reasoning_tokens,
            reasoning_turns,
            cache_turns,
            cache_input,
            cache_read,
            cache_write,
        ) in scan.execute(
            f"""
            SELECT c.kind, c.model_key, c.day, COUNT(*),
                SUM(c.input_estimated = 1 OR c.output_estimated = 1),
                SUM(c.input_estimated = 0 AND c.output_estimated = 0
                    AND c.input_tokens IS NOT NULL AND c.output_tokens IS NOT NULL),
                {_MEASURED_INPUT}, {_ESTIMATED_INPUT}, {_MEASURED_OUTPUT}, {_ESTIMATED_OUTPUT},
                SUM(CASE WHEN {_REASONING} THEN c.reasoning_tokens ELSE 0 END),
                SUM({_REASONING}),
                SUM({_CACHE}),
                SUM(CASE WHEN {_CACHE} THEN COALESCE(c.input_tokens, 0) ELSE 0 END),
                SUM(CASE WHEN {_CACHE} THEN COALESCE(c.cache_read_tokens, 0) ELSE 0 END),
                SUM(CASE WHEN {_CACHE} THEN COALESCE(c.cache_write_tokens, 0) ELSE 0 END)
            FROM {scan.source("stat_calls", "c")}
            WHERE {scan.where("c")}
            GROUP BY c.kind, c.model_key, c.day
            """
        ):
            if kind == CALL_KIND_COMPACTION:
                self._compaction_calls += calls
            else:
                self._usage_assistant_messages += calls
            provider = _provider_of(model_key)
            model = self._model(provider, model_key)
            provider_acc = self._provider(provider)
            self._usage_estimated_turns += estimated_turns
            self._usage_measured_turns += measured_turns
            self._unreported_calls += calls - estimated_turns - measured_turns
            self._reasoning_tokens += reasoning_tokens
            self._reasoning_turns += reasoning_turns
            self._cache_read_tokens += cache_read
            self._cache_write_tokens += cache_write
            self._cache_turns += cache_turns
            self._cache_input_tokens += cache_input
            for accumulator in (model, provider_acc):
                accumulator.assistant_messages += calls
                accumulator.estimated_turns += estimated_turns
                accumulator.measured_input_tokens += measured_input
                accumulator.estimated_input_tokens += estimated_input
                accumulator.measured_output_tokens += measured_output
                accumulator.estimated_output_tokens += estimated_output
                accumulator.reasoning_tokens += reasoning_tokens
                accumulator.reasoning_turns += reasoning_turns
                accumulator.cache_turns += cache_turns
                accumulator.cache_input_tokens += cache_input
                accumulator.cache_read_tokens += cache_read
                accumulator.cache_write_tokens += cache_write
            if day is None:
                continue
            daily = self._daily_bucket(day_key(day))
            daily.measured_input_tokens += measured_input
            daily.estimated_input_tokens += estimated_input
            daily.measured_output_tokens += measured_output
            daily.estimated_output_tokens += estimated_output
            daily.reasoning_tokens += reasoning_tokens
            daily.reasoning_turns += reasoning_turns
            daily.cache_input_tokens += cache_input
            daily.cache_read_tokens += cache_read
            daily.cache_write_tokens += cache_write

    def _load_errors(self, scan: UnitScan) -> None:
        # An error is attributed to the Model of the latest in-window
        # Assistant step before it in the same unit.
        for unit, kind, day, hour, current_model, count in scan.execute(
            f"""
            SELECT u.unit, e.kind, e.day,
                CASE WHEN e.instant IS NULL THEN NULL ELSE {_HOUR_SQL} END AS hour,
                (
                    SELECT c.model_key FROM stat_calls c
                    WHERE c.session_key = e.session_key AND c.seq < e.seq AND c.kind = 0
                        AND {scan.in_window("c")}
                    ORDER BY c.seq DESC LIMIT 1
                ) AS current_model,
                COUNT(*)
            FROM {scan.source("stat_errors", "e")}
            WHERE {scan.where("e")}
            GROUP BY u.unit, e.kind, e.day, hour, current_model
            ORDER BY u.unit
            """
        ):
            display_key = self._units[unit].display_key
            self._total_errors += count
            self._agents[display_key].errors += count
            unit_slice = self._slices[unit]
            if unit_slice is not None:
                unit_slice.errors += count
            self._error_by_kind[kind] += count
            self._error_by_agent[display_key] += count
            model_key = current_model or UNKNOWN_MODEL_KEY
            provider = _provider_of(model_key)
            self._error_by_model[model_key] += count
            self._error_by_provider[provider] += count
            if model_key != UNKNOWN_MODEL_KEY:
                self._model(provider, model_key).errors += count
                self._provider(provider).errors += count
            if hour is not None:
                self._error_by_hour[hour] += count
            if day is not None:
                self._daily_bucket(day_key(day)).errors += count

    def _load_tools(self, scan: UnitScan) -> None:
        source, where = scan.source("stat_tools", "t"), scan.where("t")
        for name, calls, successes, failures, duration_total, duration_count in scan.execute(
            f"""
            SELECT t.name, COUNT(*), SUM(t.outcome IS 1), SUM(t.outcome IS 0),
                SUM(t.duration_ms), COUNT(t.duration_ms)
            FROM {source}
            WHERE {where}
            GROUP BY t.name
            """
        ):
            tool = self._tool(name)
            tool.calls += calls
            tool.successes += successes
            tool.failures += failures
            tool.duration_total_ms += duration_total or 0
            tool.duration_count += duration_count
        percentiles = scan.nearest_rank(
            f"""
            SELECT t.name AS key, t.duration_ms AS value
            FROM {source}
            WHERE {where} AND t.duration_ms IS NOT NULL
            """,
            {
                name: tool.duration_count
                for name, tool in self._tools.items()
                if tool.duration_count
            },
            _TOOL_P95,
        )
        for name, value in percentiles.items():
            self._tools[str(name)].p95_duration_ms = value
        # Failure codes are counted in processing order: the first-seen code
        # wins a tie for the most frequent one.
        for name, code in scan.execute(
            f"""
            SELECT t.name, t.error_code
            FROM {source}
            WHERE {where} AND t.outcome = 0
            ORDER BY u.unit, t.seq
            """
        ):
            self._tools[name].error_codes[code] += 1
        for unit, calls in scan.execute(
            f"""
            SELECT u.unit, COUNT(*)
            FROM {source}
            WHERE {where}
            GROUP BY u.unit
            ORDER BY u.unit
            """
        ):
            report_unit = self._units[unit]
            self._tool_total_calls += calls
            self._tool_by_agent[report_unit.display_key] += calls
            self._tool_by_session[(report_unit.display_key, report_unit.session_id)] += calls
            unit_slice = self._slices[unit]
            if unit_slice is not None:
                unit_slice.tool_calls += calls

    def _load_runs(self, scan: UnitScan) -> None:
        # A Run's group is every earlier in-window record of the same unit
        # that carries its Run id; a repeated Run id keeps accumulating.
        session_runs: dict[int, int] = {}
        for (
            unit,
            day,
            run_id,
            status,
            duration,
            started_at,
            completed_at,
            tool_calls,
            model_steps,
            agent_messages,
            models_json,
        ) in scan.execute(
            f"""
            WITH summaries AS (
                SELECT u.unit, s.session_key, s.seq, s.day, s.run_id, s.status,
                    s.duration_ms, s.timing_started_at, s.timing_completed_at
                FROM {scan.source("stat_runs", "s")}
                WHERE {scan.where("s")}
            ),
            groups AS (
                SELECT m.unit, m.seq,
                    SUM(r.role = 'tool') AS tool_calls,
                    SUM(r.role = 'assistant') AS model_steps,
                    SUM(COALESCE(c.visible, 0)) AS agent_messages,
                    json_group_array(DISTINCT CASE WHEN c.has_model = 1 THEN c.model_key END)
                        AS models
                FROM summaries m
                CROSS JOIN stat_records r
                    ON r.session_key = m.session_key AND r.run_id = m.run_id AND r.seq < m.seq
                LEFT JOIN stat_calls c
                    ON c.session_key = r.session_key AND c.seq = r.seq
                    AND r.role = 'assistant' AND c.kind = 0
                WHERE r.run_id IS NOT NULL AND r.run_id <> '' AND r.role <> 'run_summary'
                    AND {scan.in_window("r")}
                GROUP BY m.unit, m.seq
            )
            SELECT m.unit, m.day, m.run_id, m.status, m.duration_ms, m.timing_started_at,
                m.timing_completed_at, g.tool_calls, g.model_steps, g.agent_messages, g.models
            FROM summaries m
            LEFT JOIN groups g ON g.unit = m.unit AND g.seq = m.seq
            ORDER BY m.unit, m.seq
            """
        ):
            report_unit = self._units[unit]
            agent = self._agents[report_unit.display_key]
            self._total_runs += 1
            agent.runs += 1
            session_runs[unit] = session_runs.get(unit, 0) + 1
            self._status_counts[status] += 1
            unit_slice = self._slices[unit]
            if unit_slice is not None:
                unit_slice.runs += 1
                unit_slice.status[status] += 1
            if duration is not None:
                self._run_durations.append(duration)
            models = _json_models(models_json)
            if len(models) >= 2:
                self._derived_fallback_runs += 1
            for model_key in models:
                provider = _provider_of(model_key)
                model = self._model(provider, model_key)
                model.runs += 1
                self._provider(provider).runs += 1
                if duration is not None:
                    model.run_duration_total_ms += duration
                    model.run_duration_count += 1
            tool_calls = tool_calls or 0
            self._run_model_steps += model_steps or 0
            self._run_agent_messages += agent_messages or 0
            if tool_calls:
                self._runs_with_tool_calls += 1
                self._run_tool_calls += tool_calls
            if duration is not None:
                self._longest_runs.append(
                    LongestRun(
                        agent_id=report_unit.display_key,
                        session_id=report_unit.session_id,
                        run_id=run_id or "",
                        status=status,
                        duration_ms=duration,
                        started_at=started_at,
                        completed_at=completed_at,
                        models=sorted(models),
                    )
                )
            if day is not None:
                bucket = self._daily_bucket(day_key(day))
                bucket.runs += 1
                if status == "completed":
                    bucket.completed += 1
                elif status == "failed":
                    bucket.failed += 1
                elif status == "cancelled":
                    bucket.cancelled += 1
                elif status == "interrupted":
                    bucket.interrupted += 1
        for unit, runs in session_runs.items():
            report_unit = self._units[unit]
            self._runs_per_session.append(
                SessionRunCount(report_unit.display_key, report_unit.session_id, runs)
            )
        # A Run id with conversational in-window records but no in-window
        # terminal summary in its unit is a best-effort open group.
        self._open_run_groups += scan.execute(
            f"""
            SELECT COUNT(*) FROM (
                SELECT DISTINCT u.unit, r.run_id
                FROM {scan.source("stat_records", "r")}
                WHERE {scan.where("r")}
                    AND r.run_id IS NOT NULL AND r.run_id <> ''
                    AND r.role IN ('user', 'assistant')
                    AND NOT EXISTS (
                        SELECT 1 FROM stat_runs s
                        WHERE s.session_key = r.session_key AND s.run_id = r.run_id
                            AND {scan.in_window("s")}
                    )
            )
            """
        ).fetchone()[0]

    def _load_slice_activity(self, scan: UnitScan) -> None:
        if all(value is None for value in self._slices):
            return
        for unit, timestamp in scan.execute(
            f"""
            SELECT unit, timestamp FROM (
                SELECT u.unit, r.timestamp, ROW_NUMBER() OVER (
                    PARTITION BY u.unit ORDER BY {max_timestamp_sql("r")}
                ) AS position
                FROM {scan.source("stat_records", "r")}
                WHERE u.extension = 1 AND {scan.where("r")}
            ) WHERE position = 1
            """
        ):
            unit_slice = self._slices[unit]
            if unit_slice is not None:
                unit_slice.last_activity = timestamp

    def _load_skills(self, scan: UnitScan) -> None:
        # Skills apply their own windows (offers by Session start, activations
        # by record time) and count activations ever, so every unit is read.
        activations: dict[int, list[tuple[str, str | None]]] = {}
        for unit, name, timestamp in scan.execute(
            f"""
            SELECT u.unit, k.name, r.timestamp
            FROM {scan.source("stat_skills", "k")}
            JOIN stat_records r ON r.session_key = k.session_key AND r.seq = k.seq
            ORDER BY u.unit, k.seq
            """
        ):
            activations.setdefault(unit, []).append((name, timestamp))
        for unit, report_unit in enumerate(self._units):
            created_at, offered = self._skill_facts[unit]
            self._skill_usage.observe_session(
                display_key=report_unit.display_key,
                created_at=created_at,
                offered_names=offered,
                activations=activations.get(unit, []),
            )

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
            costs=self._costs.build(),
            extensions=self._extensions.build(),
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
        return self._compactions.build()

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
            # Extension actor rows attribute activity; an Extension is not an Agent.
            total_agents=sum(
                not agent_id.startswith(EXTENSION_ACTOR_PREFIX) for agent_id in self._agent_order
            ),
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
            total_session_records=self._total_records,
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
            model_calls=self._usage_assistant_messages + self._compaction_calls,
            compaction_calls=self._compaction_calls,
            unreported_calls=self._unreported_calls,
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
            self._cache.sessions,
            key=lambda record: (record.hit_rate, -record.input_tokens, record.session_id),
        )[:TOP_CACHE_SESSIONS]
        return CacheSection(
            lowest_hit_rate_sessions=sessions,
            suspected_breaks=SuspectedCacheBreaks(
                evaluated_turns=self._cache.evaluated_turns,
                suspected_turns=self._cache.suspected_turns,
                incidents=self._cache.incidents,
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
        top_error = accumulator.error_codes.most_common(1)
        return ToolStat(
            name=accumulator.name,
            calls=accumulator.calls,
            successes=accumulator.successes,
            failures=accumulator.failures,
            success_rate=_ratio(accumulator.successes, accumulator.calls),
            error_rate=_ratio(accumulator.failures, accumulator.calls),
            average_duration_ms=(
                accumulator.duration_total_ms / accumulator.duration_count
                if accumulator.duration_count
                else None
            ),
            p95_duration_ms=accumulator.p95_duration_ms,
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

    def _daily_bucket(self, day: str) -> _DailyAcc:
        bucket = self._daily.get(day)
        if bucket is None:
            bucket = _DailyAcc()
            self._daily[day] = bucket
        return bucket

    def _sorted_daily(self) -> list[tuple[str, _DailyAcc]]:
        return sorted(self._daily.items(), key=lambda item: item[0])


def load_run_activity(
    connection: sqlite3.Connection,
    units: Sequence[ReportUnit],
    *,
    since: datetime,
    until: datetime,
    limit: int,
) -> tuple[int, list[RunActivity]]:
    """Return the overlap count and the latest-started Runs overlapping the interval.

    A Run's execution spans its timed start and completion, falling back to its
    summary timestamp; a Run without a parseable span never overlaps. Runs are
    ordered by that start text, newest first, ties in processing order, and
    only the returned Runs read their groups: every earlier record of the same
    unit with the Run id, regardless of the interval.
    """
    scan = UnitScan(connection, units)
    params = {"since": datetime_instant(since), "until": datetime_instant(until), "limit": limit}
    # Overlapping Runs come from the activity index, so a narrow interval never
    # visits every Run of every unit.
    overlapping = """
        FROM stat_runs s
        CROSS JOIN temp.units u ON u.session_key = s.session_key
        JOIN stat_records sr ON sr.session_key = s.session_key AND sr.seq = s.seq
        WHERE s.activity_end >= :since AND s.activity_start <= :until
            AND s.activity_start IS NOT NULL AND s.activity_end IS NOT NULL
    """
    total = scan.execute(f"SELECT COUNT(*) {overlapping}", params).fetchone()[0]
    runs: list[RunActivity] = []
    for (
        unit,
        run_id,
        status,
        duration,
        started_at,
        completed_at,
        tool_calls,
        measured_input,
        estimated_input,
        measured_output,
        estimated_output,
        models_json,
    ) in scan.execute(
        f"""
        WITH selected AS (
            SELECT u.unit, s.session_key, s.seq, s.run_id, s.status, s.duration_ms,
                COALESCE(NULLIF(s.timing_started_at, ''), sr.timestamp) AS started_at,
                COALESCE(NULLIF(s.timing_completed_at, ''), sr.timestamp) AS completed_at
            {overlapping}
            ORDER BY started_at DESC, u.unit, s.seq
            LIMIT :limit
        ),
        groups AS (
            SELECT m.unit, m.seq,
                SUM(r.role = 'tool') AS tool_calls,
                {_MEASURED_INPUT} AS measured_input,
                {_ESTIMATED_INPUT} AS estimated_input,
                {_MEASURED_OUTPUT} AS measured_output,
                {_ESTIMATED_OUTPUT} AS estimated_output,
                json_group_array(DISTINCT CASE WHEN c.has_model = 1 THEN c.model_key END)
                    AS models
            FROM selected m
            CROSS JOIN stat_records r
                ON r.session_key = m.session_key AND r.run_id = m.run_id AND r.seq < m.seq
            LEFT JOIN stat_calls c
                ON c.session_key = r.session_key AND c.seq = r.seq
                AND r.role = 'assistant' AND c.kind = 0
            WHERE r.run_id IS NOT NULL AND r.run_id <> '' AND r.role <> 'run_summary'
            GROUP BY m.unit, m.seq
        )
        SELECT m.unit, m.run_id, m.status, m.duration_ms, m.started_at, m.completed_at,
            COALESCE(g.tool_calls, 0), COALESCE(g.measured_input, 0),
            COALESCE(g.estimated_input, 0), COALESCE(g.measured_output, 0),
            COALESCE(g.estimated_output, 0), g.models
        FROM selected m
        LEFT JOIN groups g ON g.unit = m.unit AND g.seq = m.seq
        ORDER BY m.started_at DESC, m.unit, m.seq
        """,
        params,
    ):
        report_unit = units[unit]
        runs.append(
            RunActivity(
                agent_id=report_unit.display_key,
                session_id=report_unit.session_id,
                session_title=report_unit.title or None,
                run_id=run_id or "",
                status=status,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=duration or 0,
                models=sorted(_json_models(models_json)),
                tool_calls=tool_calls,
                measured_input_tokens=measured_input,
                measured_output_tokens=measured_output,
                estimated_input_tokens=estimated_input,
                estimated_output_tokens=estimated_output,
            )
        )
    return total, runs


def _provider_of(model_key: str) -> str:
    return model_key.split("/", 1)[0] if "/" in model_key else model_key


def _json_models(value: str | None) -> set[str]:
    if value is None:
        return set()
    return {model for model in json.loads(value) if isinstance(model, str)}
