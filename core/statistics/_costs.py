"""Per-call cost projection over canonical Usage, with explicit coverage."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from core.models.pricing import TokenPricing
from core.statistics._projection import (
    CALL_KIND_COMPACTION,
    COST_PROVIDER,
    COST_UNPRICED,
    PricingInputs,
    compact_json,
    cost_json,
    cost_source_class,
    day_key,
)

if TYPE_CHECKING:
    from core.statistics._units import UnitScan

PricingLookup = Callable[[str], TokenPricing | None]

TOP_COST_SESSIONS = 20
RECENT_CALLS = 50
_PRICING_BATCH = 1000


@dataclass
class CostTotals:
    calls: int = 0
    reported_calls: int = 0
    estimated_calls: int = 0
    unpriced_calls: int = 0
    retrospective_calls: int = 0
    reported_usd: float | None = None
    estimated_usd: float | None = None

    @property
    def known_total(self) -> float:
        return (self.reported_usd or 0) + (self.estimated_usd or 0)

    def add(self, cost: dict[str, Any], *, retrospective: bool) -> None:
        source, amount = cost_source_class(cost)
        self.add_classified(source, amount, retrospective=retrospective)

    def add_classified(self, source: int, amount: float | None, *, retrospective: bool) -> None:
        """Count one call whose cost source and amount are already classified."""
        self.calls += 1
        if source == COST_UNPRICED:
            self.unpriced_calls += 1
        elif source == COST_PROVIDER:
            self.reported_calls += 1
            self.reported_usd = (self.reported_usd or 0) + cast(float, amount)
        else:
            self.estimated_calls += 1
            self.estimated_usd = (self.estimated_usd or 0) + cast(float, amount)
            self.retrospective_calls += int(retrospective)

    def merge(self, other: CostTotals) -> None:
        self.calls += other.calls
        self.reported_calls += other.reported_calls
        self.estimated_calls += other.estimated_calls
        self.unpriced_calls += other.unpriced_calls
        self.retrospective_calls += other.retrospective_calls
        if other.reported_usd is not None:
            self.reported_usd = (self.reported_usd or 0) + other.reported_usd
        if other.estimated_usd is not None:
            self.estimated_usd = (self.estimated_usd or 0) + other.estimated_usd


@dataclass(frozen=True)
class ModelCosts:
    model: str
    totals: CostTotals


@dataclass(frozen=True)
class DailyCosts:
    date: str
    totals: CostTotals


@dataclass(frozen=True)
class SessionCosts:
    agent_id: str
    session_id: str
    session_title: str | None
    totals: CostTotals


@dataclass(frozen=True)
class CallCost:
    timestamp: str
    agent_id: str
    session_id: str
    session_title: str | None
    run_id: str | None
    model: str
    kind: str
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    estimated_tokens: bool
    cost: dict[str, Any]
    retrospective: bool
    status: str | None = None


@dataclass(frozen=True)
class CostsSection:
    totals: CostTotals
    models: list[ModelCosts]
    daily: list[DailyCosts]
    top_sessions: list[SessionCosts]
    recent_calls: list[CallCost]
    recent_calls_truncated: bool
    compactions: CostTotals


# Every call table with the records table that carries its calls' Run ids.
_CALL_TABLES = (("stat_calls", "stat_records"), ("stat_usage_calls", "stat_usage_records"))

# One repriced call's owner: its Session key (or ledger unit key) and Run id.
PricedCall = tuple[int, str | None]


def refresh_retrospective_costs(
    connection: sqlite3.Connection, pricing_lookup: PricingLookup | None
) -> dict[str, set[PricedCall]]:
    """Price calls without a cost snapshot under the current catalog pricing.

    Both call tables share one fingerprint per Model, so a Model's calls are
    always priced alike. Unchanged pricing only prices calls ingested since the
    last reconcile; changed pricing reprices that Model's calls everywhere. An
    unchanged index with unchanged pricing performs no write. Returns, per call
    table, the owners of the calls whose projected cost changed.
    """
    fingerprints = {
        str(model): str(fingerprint)
        for model, fingerprint in connection.execute(
            "SELECT model_key, fingerprint FROM stat_pricing"
        )
    }
    unpriced = {
        table: {
            str(row[0])
            for row in connection.execute(
                f"SELECT DISTINCT model_key FROM {table} WHERE retrospective = 1 AND priced = 0"
            )
        }
        for table, _records in _CALL_TABLES
    }
    touched: dict[str, set[PricedCall]] = {table: set() for table, _records in _CALL_TABLES}
    # Every priced Model keeps a fingerprint, so these are all Models with
    # retrospective calls, found without scanning the priced ones.
    for model in sorted(set(fingerprints).union(*unpriced.values())):
        pricing = pricing_lookup(model) if pricing_lookup is not None else None
        fingerprint = "null" if pricing is None else compact_json(pricing.to_dict())
        changed = fingerprints.get(model) != fingerprint
        for table, records in _CALL_TABLES:
            if changed or model in unpriced[table]:
                touched[table] |= _price_calls(
                    connection,
                    model,
                    pricing,
                    only_unpriced=not changed,
                    table=table,
                    records=records,
                )
        if changed:
            connection.execute(
                "INSERT OR REPLACE INTO stat_pricing (model_key, fingerprint) VALUES (?, ?)",
                (model, fingerprint),
            )
    return touched


def prune_pricing(connection: sqlite3.Connection) -> None:
    """Forget the fingerprints of Models without retrospective calls left."""
    connection.execute(
        "DELETE FROM stat_pricing WHERE "
        + " AND ".join(
            f"NOT EXISTS (SELECT 1 FROM {table} "
            "WHERE retrospective = 1 AND model_key = stat_pricing.model_key)"
            for table, _records in _CALL_TABLES
        )
    )


def _price_calls(
    connection: sqlite3.Connection,
    model: str,
    pricing: TokenPricing | None,
    *,
    only_unpriced: bool,
    table: str,
    records: str,
) -> set[PricedCall]:
    condition = " AND c.priced = 0" if only_unpriced else ""
    cursor = connection.execute(
        f"""
        SELECT c.session_key, c.seq, r.run_id, c.priced, c.cost_json, c.reported_cost_usd,
            c.input_tokens, c.output_tokens, c.cache_read_tokens, c.cache_read_present,
            c.cache_write_tokens, c.cache_write_present, c.reasoning_tokens,
            c.reasoning_present, c.price_estimated
        FROM {table} c
        LEFT JOIN {records} r ON r.session_key = c.session_key AND r.seq = c.seq
        WHERE c.model_key = ? AND c.retrospective = 1{condition}
        """,
        (model,),
    )
    touched: set[PricedCall] = set()
    updates: list[tuple[Any, ...]] = []
    for row in cursor.fetchall():
        cost = PricingInputs(
            reported_cost_usd=row[5],
            input_tokens=row[6],
            output_tokens=row[7],
            cache_read_tokens=row[8],
            cache_read_present=bool(row[9]),
            cache_write_tokens=row[10],
            cache_write_present=bool(row[11]),
            reasoning_tokens=row[12],
            reasoning_present=bool(row[13]),
            estimated=bool(row[14]),
        ).price(pricing)
        serialized = cost_json(cost)
        if row[3] and serialized == row[4]:
            continue
        source, amount = cost_source_class(cost)
        updates.append((amount, source, serialized, row[0], row[1]))
        touched.add((int(row[0]), row[2]))
        if len(updates) >= _PRICING_BATCH:
            _write_prices(connection, updates, table=table)
            updates = []
    _write_prices(connection, updates, table=table)
    return touched


def _write_prices(
    connection: sqlite3.Connection, updates: Sequence[tuple[Any, ...]], *, table: str
) -> None:
    if updates:
        connection.executemany(
            f"""
            UPDATE {table} SET cost_usd = ?, cost_source = ?, cost_json = ?, priced = 1
            WHERE session_key = ? AND seq = ?
            """,
            updates,
        )


@dataclass
class CostAccumulator:
    totals: CostTotals = field(default_factory=CostTotals)
    compactions: CostTotals = field(default_factory=CostTotals)
    models: dict[str, CostTotals] = field(default_factory=dict)
    daily: dict[str, CostTotals] = field(default_factory=dict)
    sessions: dict[tuple[str, str], SessionCosts] = field(default_factory=dict)
    recent: list[CallCost] = field(default_factory=list)

    def load(
        self,
        scan: UnitScan,
        *,
        titles: Sequence[str | None],
        slices: Sequence[CostTotals | None],
    ) -> None:
        """Accumulate every in-window call cost in processing order.

        Amounts are summed call by call in processing order, exactly like a
        sequential scan, so float totals do not depend on SQL aggregation
        order. ``slices`` receives each Extension participant unit's costs.
        """
        units = scan.units
        current_unit = -1
        session: CostTotals | None = None
        unit_slice: CostTotals | None = None
        models = self.models
        daily = self.daily
        totals = self.totals
        compactions = self.compactions
        for unit, kind, model, day, source, amount, retrospective in scan.execute(
            f"""
            SELECT u.unit, c.kind, c.model_key, c.day, c.cost_source, c.cost_usd,
                c.retrospective
            FROM {scan.source("stat_calls", "c")}
            WHERE {scan.where("c")}
            ORDER BY u.unit, c.seq
            """
        ):
            if unit != current_unit:
                current_unit = unit
                report_unit = units[unit]
                session = (
                    self.sessions.setdefault(
                        (report_unit.display_key, report_unit.session_id),
                        SessionCosts(
                            report_unit.display_key,
                            report_unit.session_id,
                            titles[unit],
                            CostTotals(),
                        ),
                    ).totals
                    if report_unit.session_id
                    else None
                )
                unit_slice = slices[unit]
            retro = bool(retrospective)
            totals.add_classified(source, amount, retrospective=retro)
            model_totals = models.get(model)
            if model_totals is None:
                model_totals = models[model] = CostTotals()
            model_totals.add_classified(source, amount, retrospective=retro)
            if session is not None:
                session.add_classified(source, amount, retrospective=retro)
            key = day_key(day)
            day_totals = daily.get(key)
            if day_totals is None:
                day_totals = daily[key] = CostTotals()
            day_totals.add_classified(source, amount, retrospective=retro)
            if kind == CALL_KIND_COMPACTION:
                compactions.add_classified(source, amount, retrospective=retro)
            if unit_slice is not None:
                unit_slice.add_classified(source, amount, retrospective=retro)
        self.recent = [
            CallCost(
                timestamp,
                units[unit].display_key,
                units[unit].session_id,
                titles[unit],
                run_id,
                model,
                purpose,
                input_tokens,
                output_tokens,
                cache_read_tokens,
                bool(estimated),
                json.loads(cost),
                bool(retrospective),
                status,
            )
            for (
                unit,
                timestamp,
                run_id,
                model,
                purpose,
                input_tokens,
                output_tokens,
                cache_read_tokens,
                estimated,
                cost,
                retrospective,
                status,
            ) in scan.execute(
                f"""
                SELECT u.unit, r.timestamp, r.run_id, c.model_key, c.purpose, c.input_tokens,
                    c.output_tokens, c.cache_read_tokens,
                    c.input_estimated OR c.output_estimated, c.cost_json, c.retrospective,
                    {scan.call_status_sql}
                FROM {scan.source("stat_calls", "c")}
                JOIN {scan.table("stat_records")} r
                    ON r.session_key = c.session_key AND r.seq = c.seq
                WHERE {scan.where("c")}
                ORDER BY c.instant DESC, u.unit, c.seq
                LIMIT {RECENT_CALLS}
                """
            )
        ]

    def build(self) -> CostsSection:
        return CostsSection(
            totals=self.totals,
            models=[
                ModelCosts(model, totals)
                for model, totals in sorted(
                    self.models.items(), key=lambda pair: (-pair[1].known_total, pair[0])
                )
            ],
            daily=[DailyCosts(day, totals) for day, totals in sorted(self.daily.items())],
            top_sessions=sorted(
                self.sessions.values(),
                key=lambda row: (-row.totals.known_total, row.agent_id, row.session_id),
            )[:TOP_COST_SESSIONS],
            recent_calls=self.recent,
            recent_calls_truncated=self.totals.calls > RECENT_CALLS,
            compactions=self.compactions,
        )
