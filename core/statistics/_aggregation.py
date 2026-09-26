"""Aggregate report units over the typed index and assemble the Statistics report.

``ReportBuilder`` registers units in processing order and hands the scan to
one accumulator per report section. Each section aggregates in SQL (window
filters and grouping run on indexed columns) and walks rows in order only
where it is order-dependent: Run groups, the prompt-cache heuristic,
Compaction recurrence and sequential cost sums. The builder itself owns the
overview's per-unit record and message counts, Extension slice activity and
the Skill tally.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime

from core.statistics._accumulators import ReportLedger
from core.statistics._cache import load_cache_facts
from core.statistics._call_scan import AccountingScan, account_model_runs, retained_run_durations
from core.statistics._compactions import CompactionAccumulator
from core.statistics._costs import (
    CostAccumulator,
    PricingLookup,
    refresh_retrospective_costs,
)
from core.statistics._errors import ErrorAccumulator
from core.statistics._extensions import (
    EXTENSION_ACTOR_PREFIX,
    ExtensionUsageAccumulator,
)
from core.statistics._measurements import (
    _max_timestamp,
    _mean,
    _nearest_rank_percentile,
)
from core.statistics._runs import RunAccumulator
from core.statistics._tools import ToolAccumulator
from core.statistics._units import ReportUnit, UnitScan, max_timestamp_sql
from core.statistics._usage import (
    ESTIMATED_INPUT_SQL,
    ESTIMATED_OUTPUT_SQL,
    MEASURED_INPUT_SQL,
    MEASURED_OUTPUT_SQL,
    TOP_CACHE_BREAK_INCIDENTS,
    UsageAccumulator,
)
from core.statistics.report import (
    AgentActivity,
    DailyTrendPoint,
    JsonObject,
    OverviewSection,
    StatisticsReport,
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

        self._ledger = ReportLedger()
        self._skill_facts: list[tuple[str | None, list[str]]] = []

        self._usage = UsageAccumulator()
        self._runs = RunAccumulator()
        self._errors = ErrorAccumulator()
        self._tools = ToolAccumulator()
        self._costs = CostAccumulator()
        self._compactions = CompactionAccumulator()
        self._extensions = ExtensionUsageAccumulator(
            windowed=since is not None or until is not None
        )

        self._total_sessions = 0
        self._total_records = 0
        self._role_counts: Counter[str] = Counter()
        self._chat_message_role_counts: Counter[str] = Counter()
        self._last_activity: str | None = None

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
        self._ledger.add_unit(
            unit, None if unit.extension is None else self._extensions.slice(unit.extension)
        )
        self._skill_facts.append((created_at, list(offered_skills)))

    def register_agent(self, agent_id: str, summaries: Sequence[JsonObject]) -> None:
        """Record an agent and its session-level structural facts."""
        accumulator = self._ledger.agent(agent_id)
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

    def aggregate(
        self,
        connection: sqlite3.Connection,
        *,
        durable_usage: bool = False,
        group_usage: bool = False,
    ) -> None:
        """Aggregate every registered unit from the reconciled index."""
        ledger = self._ledger
        scan = UnitScan(connection, ledger.units, since=self._since, until=self._until)
        titles = [unit.title for unit in ledger.units]
        self._load_records(scan)
        self._load_visible_calls(scan, durable_usage=durable_usage)
        self._errors.load(scan, ledger)
        self._tools.load(scan, ledger)
        self._runs.load(scan, ledger)
        self._compactions.load(scan, titles)
        self._load_slice_activity(scan)
        if self._include_skills:
            self._load_skills(scan)
        slices = [None if value is None else value.costs for value in ledger.slices]
        durations = retained_run_durations(scan) if durable_usage else {}
        if durable_usage:
            # Cache diagnostics describe the retained conversational request
            # sequence, never auxiliary requests or failed retry attempts.
            self._usage.cache = load_cache_facts(scan, top_incidents=TOP_CACHE_BREAK_INCIDENTS)
            accounting = AccountingScan(
                connection,
                ledger.units,
                since=self._since,
                until=self._until,
                group=group_usage,
            )
            self._load_accounting_slices(accounting)
            scan = accounting
            titles = [unit.title for unit in scan.units]
            slices = [
                None if position is None else slices[position]
                for position in accounting.live_positions
            ]
        self._usage.load(
            scan, ledger, include_cache=not durable_usage, count_assistant=not durable_usage
        )
        if isinstance(scan, AccountingScan):
            account_model_runs(scan, ledger, durations)
        if self._include_costs:
            refresh_retrospective_costs(
                connection, self._pricing_lookup, table=scan.table("stat_calls")
            )
            self._costs.load(
                scan,
                titles=titles,
                slices=slices,
            )

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
            agent = self._ledger.unit_agent(unit)
            self._total_records += total
            agent.session_records += total
            for role, count in zip(SESSION_RECORD_ROLES, row[2:], strict=True):
                if count:
                    self._role_counts[role] += count
            users = row[user_column]
            self._chat_message_role_counts["user"] += users
            agent.chat_messages += users
            unit_slice = self._ledger.slices[unit]
            if unit_slice is not None:
                unit_slice.records += total

    def _load_visible_calls(self, scan: UnitScan, *, durable_usage: bool) -> None:
        """Count visible Assistant messages per unit and fill Extension slice call totals."""
        for (
            unit,
            visible,
            assistant,
            calls,
            measured_input,
            estimated_input,
            measured_output,
            estimated_output,
        ) in scan.execute(
            f"""
            SELECT u.unit, SUM(c.kind = 0 AND c.visible = 1), SUM(c.kind = 0), COUNT(*),
                {MEASURED_INPUT_SQL}, {ESTIMATED_INPUT_SQL},
                {MEASURED_OUTPUT_SQL}, {ESTIMATED_OUTPUT_SQL}
            FROM {scan.source("stat_calls", "c")}
            WHERE {scan.where("c")}
            GROUP BY u.unit
            """
        ):
            self._chat_message_role_counts["assistant"] += visible
            self._ledger.unit_agent(unit).chat_messages += visible
            if durable_usage:
                self._usage.assistant_messages += assistant
            unit_slice = self._ledger.slices[unit]
            if unit_slice is not None and not durable_usage:
                unit_slice.model_calls += calls
                unit_slice.measured_input_tokens += measured_input
                unit_slice.estimated_input_tokens += estimated_input
                unit_slice.measured_output_tokens += measured_output
                unit_slice.estimated_output_tokens += estimated_output

    def _load_accounting_slices(self, scan: AccountingScan) -> None:
        """Fill live Extension slices from the same durable calls as global usage."""
        for (
            unit,
            calls,
            measured_input,
            estimated_input,
            measured_output,
            estimated_output,
            timestamp,
        ) in scan.execute(
            f"""
            SELECT u.unit, COUNT(*), {MEASURED_INPUT_SQL}, {ESTIMATED_INPUT_SQL},
                {MEASURED_OUTPUT_SQL}, {ESTIMATED_OUTPUT_SQL}, MAX(r.timestamp)
            FROM {scan.source("stat_calls", "c")}
            JOIN {scan.table("stat_records")} r
                ON r.session_key = c.session_key AND r.seq = c.seq
            WHERE {scan.where("c")}
            GROUP BY u.unit
            """
        ):
            position = scan.live_positions[unit]
            unit_slice = self._ledger.slices[position] if position is not None else None
            if unit_slice is not None:
                unit_slice.model_calls += calls
                unit_slice.measured_input_tokens += measured_input
                unit_slice.estimated_input_tokens += estimated_input
                unit_slice.measured_output_tokens += measured_output
                unit_slice.estimated_output_tokens += estimated_output
                unit_slice.last_activity = _max_timestamp(unit_slice.last_activity, timestamp)

    def _load_slice_activity(self, scan: UnitScan) -> None:
        slices = self._ledger.slices
        if all(value is None for value in slices):
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
            unit_slice = slices[unit]
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
        for unit, report_unit in enumerate(self._ledger.units):
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
            usage=self._usage.build(self._ledger),
            runs=self._runs.build(self._ledger),
            compactions=self._compactions.build(),
            errors=self._errors.build(self._ledger),
            tools=self._tools.build(),
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

    def _build_overview(self) -> OverviewSection:
        ledger = self._ledger
        runs = self._runs
        durations = sorted(runs.durations)
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
            for accumulator in (ledger.agents[agent_id] for agent_id in ledger.agent_order)
        ]
        return OverviewSection(
            # Extension actor rows attribute activity; an Extension is not an Agent.
            total_agents=sum(
                not agent_id.startswith(EXTENSION_ACTOR_PREFIX) for agent_id in ledger.agent_order
            ),
            total_sessions=self._total_sessions,
            total_runs=runs.total,
            open_run_groups=runs.open_groups,
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
            run_status=runs.status(),
            average_run_duration_ms=_mean(durations),
            median_run_duration_ms=_nearest_rank_percentile(durations, 50),
            runs_with_tool_calls=runs.with_tool_calls,
            total_tool_calls=self._tools.total_calls,
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
                for date, bucket in ledger.sorted_daily()
            ],
        )
