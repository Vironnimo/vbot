"""Error records of Extension group units, attributed to Models, Providers and days."""

from __future__ import annotations

from core.statistics._accumulators import ReportLedger
from core.statistics._measurements import UNKNOWN_MODEL_KEY, _provider_of
from core.statistics._units import UnitScan


def load_errors(scan: UnitScan, ledger: ReportLedger) -> None:
    """Count in-window errors of every scanned unit per Model, Provider and day.

    An error is attributed to the Model of the latest in-window Assistant
    step before it in the same unit.
    """
    for day, current_model, count in scan.execute(
        f"""
        SELECT e.day,
            (
                SELECT c.model_key FROM stat_calls c
                WHERE c.session_key = e.session_key AND c.seq < e.seq AND c.kind = 0
                    AND {scan.in_window("c")}
                ORDER BY c.seq DESC LIMIT 1
            ) AS current_model,
            COUNT(*)
        FROM {scan.source("stat_errors", "e")}
        WHERE {scan.where("e")}
        GROUP BY e.day, current_model
        """
    ):
        model_key = current_model or UNKNOWN_MODEL_KEY
        if model_key != UNKNOWN_MODEL_KEY:
            ledger.model(model_key).errors += count
            ledger.provider(_provider_of(model_key)).errors += count
        ledger.day(day).errors += count
