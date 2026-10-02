"""Retrospective call pricing at reconcile, and cost totals with explicit coverage."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from core.models.pricing import TokenPricing
from core.statistics._projection import (
    PricingInputs,
    compact_json,
    cost_json,
    cost_source_class,
)

PricingLookup = Callable[[str], TokenPricing | None]

_PRICING_BATCH = 1000


@dataclass
class CostTotals:
    """Calls by cost coverage; amounts are ``None`` until a call of their source counts."""

    calls: int = 0
    reported_calls: int = 0
    estimated_calls: int = 0
    unpriced_calls: int = 0
    retrospective_calls: int = 0
    reported_usd: float | None = None
    estimated_usd: float | None = None

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
