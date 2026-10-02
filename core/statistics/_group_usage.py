"""Extension group usage: the aggregates of one owner group's exact owned Runs.

A group report sums the ``agg_runs`` and ``agg_run_models`` rows of the owned
Runs it is given, overall and per participant. Requests are therefore exactly
the owned Runs' own (ledger requests at the Run's Session address and Run id,
else the usage saved in the Run's records); group-level requests without a Run
stay in global accounting only.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field

from core.statistics._rollups import USAGE_MEASURES
from core.statistics._sections.common import RUN_STATUSES, JsonObject, Totals, cost_order

# Run-level measures of ``agg_runs`` a group activity sums, in output order.
_RUN_MEASURES = ("model_steps", "tool_calls", "tool_rejected", "tool_ms", "compactions", "errors")


@dataclass(frozen=True)
class OwnedRun:
    """One owned Run in the index: its Session key, Run id and participant."""

    session_key: int
    run_id: str
    participant_id: str


@dataclass
class _Activity:
    statuses: dict[str, int] = field(default_factory=lambda: dict.fromkeys(RUN_STATUSES, 0))
    measures: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_RUN_MEASURES, 0))
    totals: Totals = field(default_factory=Totals)
    models: dict[str, tuple[int, Totals]] = field(default_factory=dict)

    def add_model(self, model: str, runs: int, totals: Totals) -> None:
        current_runs, current = self.models.get(model, (0, Totals()))
        current.merge(totals)
        self.models[model] = (current_runs + runs, current)
        self.totals.merge(totals)

    def merge(self, other: _Activity) -> None:
        for status, count in other.statuses.items():
            self.statuses[status] = self.statuses.get(status, 0) + count
        for name, value in other.measures.items():
            self.measures[name] += value
        for model, (runs, totals) in other.models.items():
            self.add_model(model, runs, totals)

    def json(self) -> JsonObject:
        models = sorted(
            self.models.items(),
            key=lambda item: cost_order(item[1][1].cost_usd, item[1][1]["calls"], item[0]),
        )
        return {
            "runs": {"total": sum(self.statuses.values()), **self.statuses},
            **self.measures,
            "totals": self.totals.json(),
            "models": [
                {"model": model, "runs": runs, **totals.json()} for model, (runs, totals) in models
            ],
        }


def group_usage(
    connection: sqlite3.Connection, owned: Sequence[OwnedRun]
) -> tuple[JsonObject, dict[str, JsonObject]]:
    """Return the activity of ``owned`` overall and per participant, in first-seen order.

    An activity is ``{runs: {total, <status>...}, model_steps, tool_calls,
    tool_rejected, tool_ms, compactions, errors, totals, models}``; ``totals``
    has the report Totals shape and ``models`` lists ``{model, runs,
    ...Totals}`` by cost. An owned Run not yet in the aggregates (admitted, no
    record saved) contributes nothing.
    """
    connection.execute("DROP TABLE IF EXISTS temp.group_runs")
    connection.execute(
        "CREATE TEMP TABLE group_runs (session_key INTEGER NOT NULL, run_id TEXT NOT NULL, "
        "participant TEXT NOT NULL, PRIMARY KEY (session_key, run_id)) WITHOUT ROWID"
    )
    try:
        connection.executemany(
            "INSERT OR IGNORE INTO temp.group_runs VALUES (?, ?, ?)",
            [(run.session_key, run.run_id, run.participant_id) for run in owned],
        )
        participants = {run.participant_id: _Activity() for run in owned}
        for participant, status, count, *measures in connection.execute(
            f"""
            SELECT g.participant, r.status, COUNT(*),
                {", ".join(f"SUM(r.{name})" for name in _RUN_MEASURES)}
            FROM temp.group_runs g
            JOIN agg_runs r ON r.session_key = g.session_key AND r.run_id = g.run_id
            GROUP BY g.participant, r.status
            """
        ):
            activity = participants[participant]
            activity.statuses[status] = activity.statuses.get(status, 0) + count
            for name, value in zip(_RUN_MEASURES, measures, strict=True):
                activity.measures[name] += value or 0
        for participant, model, runs, *measures in connection.execute(
            f"""
            SELECT g.participant, m.model_key, COUNT(*),
                {", ".join(f"SUM(m.{name})" for name in USAGE_MEASURES)}
            FROM temp.group_runs g
            JOIN agg_run_models m ON m.session_key = g.session_key AND m.run_id = g.run_id
            GROUP BY g.participant, m.model_key
            """
        ):
            participants[participant].add_model(model, runs, Totals(measures))
    finally:
        connection.execute("DROP TABLE temp.group_runs")
    overall = _Activity()
    for activity in participants.values():
        overall.merge(activity)
    return overall.json(), {key: activity.json() for key, activity in participants.items()}
