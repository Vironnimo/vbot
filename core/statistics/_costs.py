"""Retrospective call pricing at reconcile, and cost totals with explicit coverage."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from core.models.pricing import TokenPricing
from core.statistics._projection import (
    PricingInputs,
    compact_json,
    cost_json,
    cost_source_class,
)

PricingLookup = Callable[[str], TokenPricing | None]

if TYPE_CHECKING:
    from core.statistics._rollups import RollupChanges

_PRICING_BATCH = 1000


# Every call table with the records table that carries its calls' Run ids.
_CALL_TABLES = (("stat_calls", "stat_records"), ("stat_usage_calls", "stat_usage_records"))


def refresh_retrospective_costs(
    connection: sqlite3.Connection, pricing_lookup: PricingLookup | None, changes: RollupChanges
) -> None:
    """Price calls without a cost snapshot under the current catalog pricing.

    Both call tables share one fingerprint per Model, so a Model's calls are
    always priced alike. Unchanged pricing only prices calls ingested since the
    last reconcile; changed pricing reprices that Model's calls everywhere. An
    unchanged index with unchanged pricing performs no write. Changed calls
    mark their Run and retain their old ledger contribution before repricing.
    A changed catalog fingerprint can reprice many calls, so its ledger units
    use the whole-unit path instead of staging a before-image per call.
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
    # Every priced Model keeps a fingerprint, so these are all Models with
    # retrospective calls, found without scanning the priced ones.
    for model in sorted(set(fingerprints).union(*unpriced.values())):
        pricing = pricing_lookup(model) if pricing_lookup is not None else None
        fingerprint = "null" if pricing is None else compact_json(pricing.to_dict())
        changed = fingerprints.get(model) != fingerprint
        for table, records in _CALL_TABLES:
            if changed or model in unpriced[table]:
                _price_calls(
                    connection,
                    model,
                    pricing,
                    only_unpriced=not changed,
                    table=table,
                    records=records,
                    changes=changes,
                )
        if changed:
            connection.execute(
                "INSERT OR REPLACE INTO stat_pricing (model_key, fingerprint) VALUES (?, ?)",
                (model, fingerprint),
            )


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
    changes: RollupChanges,
) -> None:
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
        if table == "stat_usage_calls":
            if only_unpriced:
                changes.usage_record(connection, int(row[1]))
            else:
                changes.units.add(int(row[0]))
            changes.usage_call(int(row[0]), row[2])
        elif row[2]:
            changes.session_runs(int(row[0]), (row[2],))
        updates.append((amount, source, serialized, row[0], row[1]))
        if len(updates) >= _PRICING_BATCH:
            _write_prices(connection, updates, table=table)
            updates = []
    _write_prices(connection, updates, table=table)


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
