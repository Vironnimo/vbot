"""Tool calls: outcomes, durations, failure codes, and calls per Agent and Session."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from core.statistics._accumulators import ReportLedger
from core.statistics._measurements import _count_entries, _ratio
from core.statistics._units import UnitScan
from core.statistics.report import ToolSessionCount, ToolsSection, ToolStat

TOP_TOOL_SESSIONS = 20


_TOOL_P95 = 95


@dataclass
class _ToolAcc:
    name: str
    calls: int = 0
    successes: int = 0
    failures: int = 0
    duration_total_ms: int = 0
    duration_count: int = 0
    p95_duration_ms: float | None = None
    # Insertion order is first-failure order, which breaks top-code ties.
    error_codes: Counter[str] = field(default_factory=Counter)


class ToolAccumulator:
    def __init__(self) -> None:
        self.total_calls = 0
        self.tools: dict[str, _ToolAcc] = {}
        self.by_agent: Counter[str] = Counter()
        self.by_session: Counter[tuple[str, str]] = Counter()

    def load(self, scan: UnitScan, ledger: ReportLedger) -> None:
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
            {name: tool.duration_count for name, tool in self.tools.items() if tool.duration_count},
            _TOOL_P95,
        )
        for name, value in percentiles.items():
            self.tools[str(name)].p95_duration_ms = value
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
            self.tools[name].error_codes[code] += 1
        # Per-unit counts arrive in processing order, which breaks top-Session ties.
        for unit, calls in scan.execute(
            f"""
            SELECT u.unit, COUNT(*)
            FROM {source}
            WHERE {where}
            GROUP BY u.unit
            ORDER BY u.unit
            """
        ):
            report_unit = ledger.units[unit]
            self.total_calls += calls
            self.by_agent[report_unit.display_key] += calls
            self.by_session[(report_unit.display_key, report_unit.session_id)] += calls
            unit_slice = ledger.slices[unit]
            if unit_slice is not None:
                unit_slice.tool_calls += calls

    def build(self) -> ToolsSection:
        return ToolsSection(
            total_calls=self.total_calls,
            tools=sorted(
                (_tool_stat(accumulator) for accumulator in self.tools.values()),
                key=lambda stat: (-stat.calls, stat.name),
            ),
            by_agent=_count_entries(self.by_agent),
            top_sessions=[
                ToolSessionCount(agent_id=agent_id, session_id=session_id, calls=calls)
                for (agent_id, session_id), calls in self.by_session.most_common(TOP_TOOL_SESSIONS)
            ],
        )

    def _tool(self, name: str) -> _ToolAcc:
        accumulator = self.tools.get(name)
        if accumulator is None:
            accumulator = _ToolAcc(name=name)
            self.tools[name] = accumulator
        return accumulator


def _tool_stat(accumulator: _ToolAcc) -> ToolStat:
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
