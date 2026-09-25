"""Runs: terminal Run summaries with their Run groups, and the Run activity read.

A Run's group is every earlier record of the same unit that carries its Run
id; a repeated Run id keeps accumulating. The report section counts only
in-window group records, while Run activity reads each returned Run's whole
group regardless of the interval.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Sequence
from datetime import datetime

from core.statistics._accumulators import ReportLedger
from core.statistics._measurements import _mean, _nearest_rank_percentile, _ratio
from core.statistics._projection import datetime_instant
from core.statistics._units import ReportUnit, UnitScan
from core.statistics._usage import (
    ESTIMATED_INPUT_SQL,
    ESTIMATED_OUTPUT_SQL,
    MEASURED_INPUT_SQL,
    MEASURED_OUTPUT_SQL,
)
from core.statistics.report import (
    AgentRunCount,
    DailyCount,
    DurationStats,
    LongestRun,
    RunActivity,
    RunsSection,
    RunStatusCounts,
    SessionRunCount,
)

# Output lists that would otherwise grow with data volume are bounded to a stable
# top-N; the WebUI does any further top-N / share selection from these.
TOP_LONGEST_RUNS = 10


TOP_RUN_SESSIONS = 20


class RunAccumulator:
    def __init__(self) -> None:
        self.durations: list[int] = []
        self.status_counts: Counter[str] = Counter()
        self.total = 0
        self.open_groups = 0
        self.with_tool_calls = 0
        self.tool_calls = 0
        self.agent_messages = 0
        self.model_steps = 0
        self.derived_fallback = 0
        self.per_session: list[SessionRunCount] = []
        self.longest: list[LongestRun] = []

    def load(self, scan: UnitScan, ledger: ReportLedger) -> None:
        """Walk in-window Run summaries in processing order with their in-window groups."""
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
            report_unit = ledger.units[unit]
            self.total += 1
            ledger.unit_agent(unit).runs += 1
            session_runs[unit] = session_runs.get(unit, 0) + 1
            self.status_counts[status] += 1
            unit_slice = ledger.slices[unit]
            if unit_slice is not None:
                unit_slice.runs += 1
                unit_slice.status[status] += 1
            if duration is not None:
                self.durations.append(duration)
            models = _json_models(models_json)
            if len(models) >= 2:
                self.derived_fallback += 1
            providers: set[str] = set()
            for model_key in models:
                model = ledger.model(model_key)
                model.runs += 1
                providers.add(model.provider)
                if duration is not None:
                    model.run_duration_total_ms += duration
                    model.run_duration_count += 1
            for provider in providers:
                ledger.provider(provider).runs += 1
            tool_calls = tool_calls or 0
            self.model_steps += model_steps or 0
            self.agent_messages += agent_messages or 0
            if tool_calls:
                self.with_tool_calls += 1
                self.tool_calls += tool_calls
            if duration is not None:
                self.longest.append(
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
            bucket = ledger.day(day)
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
            report_unit = ledger.units[unit]
            self.per_session.append(
                SessionRunCount(report_unit.display_key, report_unit.session_id, runs)
            )
        # A Run id with conversational in-window records but no in-window
        # terminal summary in its unit is a best-effort open group.
        self.open_groups += scan.execute(
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

    def status(self) -> RunStatusCounts:
        return RunStatusCounts(
            completed=self.status_counts.get("completed", 0),
            failed=self.status_counts.get("failed", 0),
            cancelled=self.status_counts.get("cancelled", 0),
            interrupted=self.status_counts.get("interrupted", 0),
        )

    def build(self, ledger: ReportLedger) -> RunsSection:
        durations = sorted(self.durations)
        total = self.total
        return RunsSection(
            total_runs=total,
            open_run_groups=self.open_groups,
            status=self.status(),
            cancel_rate=_ratio(self.status_counts.get("cancelled", 0), total),
            failure_rate=_ratio(self.status_counts.get("failed", 0), total),
            interruption_rate=_ratio(self.status_counts.get("interrupted", 0), total),
            duration=DurationStats(
                count=len(durations),
                average_ms=_mean(durations),
                p50_ms=_nearest_rank_percentile(durations, 50),
                p90_ms=_nearest_rank_percentile(durations, 90),
                p95_ms=_nearest_rank_percentile(durations, 95),
            ),
            runs_with_tool_calls=self.with_tool_calls,
            total_tool_calls=self.tool_calls,
            average_tool_calls_per_run=(self.tool_calls / total) if total else None,
            agent_messages=self.agent_messages,
            model_steps=self.model_steps,
            average_agent_messages_per_run=(self.agent_messages / total if total else None),
            average_model_steps_per_run=(self.model_steps / total if total else None),
            derived_fallback_runs=self.derived_fallback,
            runs_per_agent=[
                AgentRunCount(agent_id=agent_id, runs=ledger.agents[agent_id].runs)
                for agent_id in ledger.agent_order
                if ledger.agents[agent_id].runs
            ],
            top_sessions_by_runs=sorted(
                self.per_session, key=lambda entry: (-entry.runs, entry.session_id)
            )[:TOP_RUN_SESSIONS],
            runs_per_day=[
                DailyCount(date=date, count=bucket.runs)
                for date, bucket in ledger.sorted_daily()
                if bucket.runs
            ],
            longest_runs=sorted(self.longest, key=lambda run: (-run.duration_ms, run.run_id))[
                :TOP_LONGEST_RUNS
            ],
        )


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
    unit with the Run id, regardless of the interval. Usage and Model identity
    include both Assistant steps and committed Compaction Model calls.
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
                {MEASURED_INPUT_SQL} AS measured_input,
                {ESTIMATED_INPUT_SQL} AS estimated_input,
                {MEASURED_OUTPUT_SQL} AS measured_output,
                {ESTIMATED_OUTPUT_SQL} AS estimated_output,
                json_group_array(DISTINCT CASE WHEN c.has_model = 1 THEN c.model_key END)
                    AS models
            FROM selected m
            CROSS JOIN stat_records r
                ON r.session_key = m.session_key AND r.run_id = m.run_id AND r.seq < m.seq
            LEFT JOIN stat_calls c
                ON c.session_key = r.session_key AND c.seq = r.seq
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


def _json_models(value: str | None) -> set[str]:
    if value is None:
        return set()
    return {model for model in json.loads(value) if isinstance(model, str)}
