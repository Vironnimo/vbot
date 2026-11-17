"""Transactionally maintained owned-group totals and their coalesced change feed.

Triggers cover normal settlement, enrichment, history import and older writers.
Each group keeps one current revision, rather than another growing usage log.
"""

from __future__ import annotations

import sqlite3
from typing import Any

FIELDS = (
    "calls",
    "input_tokens",
    "output_tokens",
    "unreported_calls",
    "unpriced_calls",
    "cost_nano_usd",
)


def _amounts(prefix: str = "") -> tuple[str, ...]:
    usage = f"{prefix}usage_json"
    settled = f"{prefix}status != 'started'"
    inputs = f"json_extract({usage}, '$.input_tokens')"
    outputs = f"json_extract({usage}, '$.output_tokens')"
    cost = f"json_extract({usage}, '$.cost.amount_usd')"
    return (
        f"({settled})",
        f"CASE WHEN {settled} THEN COALESCE({inputs}, 0) ELSE 0 END",
        f"CASE WHEN {settled} THEN COALESCE({outputs}, 0) ELSE 0 END",
        f"({settled} AND ({inputs} IS NULL OR {outputs} IS NULL))",
        f"({settled} AND {cost} IS NULL)",
        f"CASE WHEN {settled} THEN COALESCE(CAST(ROUND({cost} * 1e9) AS INTEGER), 0) ELSE 0 END",
    )


def _delta(alias: str, sign: int) -> str:
    values = ", ".join(f"{sign} * ({value})" for value in _amounts(f"{alias}."))
    updates = ", ".join(f"{field} = {field} + excluded.{field}" for field in FIELDS)
    return f"""
    INSERT INTO usage_execution_revision (singleton, revision) VALUES (1, 1)
      ON CONFLICT(singleton) DO UPDATE SET revision = revision + 1;
    INSERT INTO usage_execution_totals (owner_name, group_id, revision, {", ".join(FIELDS)})
      SELECT {alias}.owner_name, {alias}.group_id,
        (SELECT revision FROM usage_execution_revision WHERE singleton = 1), {values}
      WHERE {alias}.owner_name IS NOT NULL AND {alias}.group_id IS NOT NULL
      ON CONFLICT(owner_name, group_id) DO UPDATE SET
        revision = excluded.revision, {updates};
    """


SCHEMA_SQL = f"""
CREATE TABLE usage_execution_revision (
    singleton INTEGER PRIMARY KEY, revision INTEGER NOT NULL
) STRICT;
CREATE TABLE usage_execution_totals (
    owner_name TEXT NOT NULL, group_id TEXT NOT NULL, revision INTEGER NOT NULL,
    {", ".join(f"{field} INTEGER NOT NULL" for field in FIELDS)},
    PRIMARY KEY(owner_name, group_id)
) STRICT;
CREATE INDEX usage_execution_changes ON usage_execution_totals(owner_name, revision);
CREATE TRIGGER usage_execution_insert AFTER INSERT ON usage_calls
  WHEN NEW.owner_name IS NOT NULL AND NEW.group_id IS NOT NULL BEGIN
    {_delta("NEW", 1)}
END;
CREATE TRIGGER usage_execution_update AFTER UPDATE ON usage_calls
  WHEN (OLD.owner_name IS NOT NULL AND OLD.group_id IS NOT NULL)
    OR (NEW.owner_name IS NOT NULL AND NEW.group_id IS NOT NULL) BEGIN
    {_delta("OLD", -1)}
    {_delta("NEW", 1)}
END;
CREATE TRIGGER usage_execution_delete AFTER DELETE ON usage_calls
  WHEN OLD.owner_name IS NOT NULL AND OLD.group_id IS NOT NULL BEGIN
    {_delta("OLD", -1)}
END;
"""


def backfill(connection: sqlite3.Connection) -> None:
    """Fill the additive projection once; subsequent writes update it atomically."""
    if connection.execute("SELECT 1 FROM usage_execution_totals LIMIT 1").fetchone():
        return
    sums = ", ".join(f"SUM({expression})" for expression in _amounts())
    connection.execute(
        f"INSERT INTO usage_execution_totals (owner_name, group_id, revision, {', '.join(FIELDS)}) "
        "SELECT owner_name, group_id, ROW_NUMBER() OVER (ORDER BY owner_name, group_id), "
        f"{sums} FROM usage_calls WHERE owner_name IS NOT NULL AND group_id IS NOT NULL "
        "GROUP BY owner_name, group_id"
    )
    connection.execute(
        "INSERT INTO usage_execution_revision (singleton, revision) "
        "SELECT 1, COALESCE(MAX(revision), 0) FROM usage_execution_totals WHERE true "
        "ON CONFLICT(singleton) DO UPDATE SET revision = MAX(revision, excluded.revision)"
    )


def summary(row: Any, *, exact_cost: bool = False) -> dict[str, Any]:
    calls, inputs, outputs, unreported, unpriced, nano_usd = row or (0, 0, 0, 0, 0, 0)
    result = {
        "calls": calls,
        "input_tokens": inputs,
        "output_tokens": outputs,
        "unreported_calls": unreported,
        "unpriced_calls": unpriced,
        "cost_usd": nano_usd / 1e9 if not calls or unpriced < calls else None,
    }
    if exact_cost:
        result["cost_nano_usd"] = nano_usd
    return result


def read_changes(
    connection: sqlite3.Connection,
    owner: str,
    after: int,
    limit: int,
) -> dict[str, Any]:
    high = connection.execute(
        "SELECT revision FROM usage_execution_revision WHERE singleton = 1"
    ).fetchone()
    rows = connection.execute(
        f"SELECT group_id, revision, {', '.join(FIELDS)} FROM usage_execution_totals "
        "WHERE owner_name = ? AND revision > ? ORDER BY revision LIMIT ?",
        (owner, after, limit + 1),
    ).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    return {
        "revision": rows[-1]["revision"] if more else (high[0] if high else 0),
        "more": more,
        "groups": [
            {"group_id": row["group_id"], "usage": summary(tuple(row)[2:], exact_cost=True)}
            for row in rows
        ],
    }
