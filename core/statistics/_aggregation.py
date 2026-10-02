"""Extension group usage: the owned-Run report behind ``StatisticsService.group_usage``.

``GroupReportBuilder`` registers one unit per owned Run slice in processing
order and hands the scan to the usage, Tool, Run and Compaction
accumulators, which aggregate the slice facts in SQL and walk rows in order
only where that order matters (Run groups, the prompt-cache heuristic and
Compaction recurrence). Its sections are the group usage contract of the
Extension API; the Statistics report itself is built from the aggregate tier
in ``core/statistics/_sections``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from core.statistics._accumulators import ReportLedger
from core.statistics._cache import TOP_CACHE_BREAK_INCIDENTS, load_cache_facts
from core.statistics._call_scan import AccountingScan, account_model_runs, retained_run_durations
from core.statistics._compactions import CompactionAccumulator
from core.statistics._errors import load_errors
from core.statistics._runs import RunAccumulator
from core.statistics._tools import ToolAccumulator
from core.statistics._units import ReportUnit, UnitScan
from core.statistics._usage import UsageAccumulator
from core.statistics.report import (
    CompactionsSection,
    RunsSection,
    ToolsSection,
    UsageSection,
)


@dataclass(frozen=True)
class GroupReport:
    """The usage, Tool, Compaction and Run sections of one group or participant."""

    usage: UsageSection
    tools: ToolsSection
    compactions: CompactionsSection
    runs: RunsSection


class GroupReportBuilder:
    """Aggregate registered owned-Run units, then build their group report."""

    def __init__(self) -> None:
        self._ledger = ReportLedger()
        self._usage = UsageAccumulator()
        self._runs = RunAccumulator()
        self._tools = ToolAccumulator()
        self._compactions = CompactionAccumulator()

    def add_unit(self, unit: ReportUnit) -> None:
        """Queue one owned Run slice in processing order."""
        self._ledger.add_unit(unit)

    def aggregate(self, connection: sqlite3.Connection, *, durable_usage: bool) -> None:
        """Aggregate every registered unit from the reconciled index.

        With ``durable_usage`` the usage figures come from the ledger requests
        of the owned Runs; otherwise from the usage saved in their records.
        """
        ledger = self._ledger
        scan = UnitScan(connection, ledger.units)
        titles = [unit.title for unit in ledger.units]
        if durable_usage:
            self._usage.assistant_messages += _assistant_steps(scan)
        load_errors(scan, ledger)
        self._tools.load(scan, ledger)
        self._runs.load(scan, ledger)
        self._compactions.load(scan, titles)
        durations = retained_run_durations(scan) if durable_usage else {}
        if durable_usage:
            # Cache diagnostics describe the retained conversational request
            # sequence, never auxiliary requests or failed retry attempts.
            self._usage.cache = load_cache_facts(scan, top_incidents=TOP_CACHE_BREAK_INCIDENTS)
            accounting = AccountingScan(connection, ledger.units)
            self._usage.load(accounting, ledger, include_cache=False, count_assistant=False)
            account_model_runs(accounting, ledger, durations)
        else:
            self._usage.load(scan, ledger, include_cache=True, count_assistant=True)

    def build(self) -> GroupReport:
        return GroupReport(
            usage=self._usage.build(self._ledger),
            tools=self._tools.build(),
            compactions=self._compactions.build(),
            runs=self._runs.build(self._ledger),
        )


def _assistant_steps(scan: UnitScan) -> int:
    """Saved Assistant steps of the scanned units."""
    return int(
        scan.execute(
            f"SELECT COUNT(*) FROM {scan.source('stat_calls', 'c')} "
            f"WHERE {scan.where('c')} AND c.kind = 0"
        ).fetchone()[0]
    )
