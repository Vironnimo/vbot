"""Error records by kind, Provider, Model, Agent, UTC hour and day."""

from __future__ import annotations

from collections import Counter

from core.statistics._accumulators import ReportLedger
from core.statistics._measurements import UNKNOWN_MODEL_KEY, _count_entries, _provider_of
from core.statistics._projection import MICROSECONDS_PER_DAY, MICROSECONDS_PER_HOUR
from core.statistics._units import UnitScan
from core.statistics.report import DailyCount, ErrorsSection, HourCount

# Floor-correct UTC hour of a microsecond instant, also before the epoch.
_HOUR_SQL = (
    f"(((e.instant % {MICROSECONDS_PER_DAY}) + {MICROSECONDS_PER_DAY}) "
    f"% {MICROSECONDS_PER_DAY}) / {MICROSECONDS_PER_HOUR}"
)


class ErrorAccumulator:
    def __init__(self) -> None:
        self.total = 0
        self.by_kind: Counter[str] = Counter()
        self.by_provider: Counter[str] = Counter()
        self.by_model: Counter[str] = Counter()
        self.by_agent: Counter[str] = Counter()
        self.by_hour: Counter[int] = Counter()

    def load(self, scan: UnitScan, ledger: ReportLedger) -> None:
        """Count in-window errors of every scanned unit.

        An error is attributed to the Model of the latest in-window Assistant
        step before it in the same unit.
        """
        for unit, kind, day, hour, current_model, count in scan.execute(
            f"""
            SELECT u.unit, e.kind, e.day,
                {_HOUR_SQL} AS hour,
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
            display_key = ledger.units[unit].display_key
            self.total += count
            ledger.agents[display_key].errors += count
            unit_slice = ledger.slices[unit]
            if unit_slice is not None:
                unit_slice.errors += count
            self.by_kind[kind] += count
            self.by_agent[display_key] += count
            model_key = current_model or UNKNOWN_MODEL_KEY
            provider = _provider_of(model_key)
            self.by_model[model_key] += count
            self.by_provider[provider] += count
            if model_key != UNKNOWN_MODEL_KEY:
                ledger.model(model_key).errors += count
                ledger.provider(provider).errors += count
            self.by_hour[hour] += count
            ledger.day(day).errors += count

    def build(self, ledger: ReportLedger) -> ErrorsSection:
        return ErrorsSection(
            total_errors=self.total,
            by_kind=_count_entries(self.by_kind),
            by_provider=_count_entries(self.by_provider),
            by_model=_count_entries(self.by_model),
            by_agent=_count_entries(self.by_agent),
            by_hour=[HourCount(hour=hour, count=self.by_hour.get(hour, 0)) for hour in range(24)],
            daily=[
                DailyCount(date=date, count=bucket.errors)
                for date, bucket in ledger.sorted_daily()
                if bucket.errors
            ],
        )
