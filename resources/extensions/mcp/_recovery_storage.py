"""Receipt-bound protection for MCP recovery records, including prior plaintext rows."""

from __future__ import annotations

import sqlite3
from functools import partial

from core.extensions.databases import Database, ExtensionDataProtection, Migration

# Generation 1 keeps the original tables in its schema. The marker prevents an
# older recovery worker from treating migrated calls as lost or repeating work.
MIGRATIONS = (Migration("mcp.0001_protected_recovery", breaks_older=True),)

TASK_SCHEMA = """
CREATE TABLE pending_tasks(
    receipt_id TEXT PRIMARY KEY,
    connection_id TEXT NOT NULL,
    configuration_hash TEXT NOT NULL,
    task_json TEXT NOT NULL CHECK(json_valid(task_json)),
    arguments_json TEXT NOT NULL CHECK(json_valid(arguments_json)),
    outcome_json TEXT CHECK(outcome_json IS NULL OR json_valid(outcome_json)),
    delivered INTEGER NOT NULL DEFAULT 0 CHECK(delivered IN (0,1))
) STRICT;
CREATE TABLE pending_calls(
    receipt_id TEXT PRIMARY KEY,
    connection_id TEXT NOT NULL,
    configuration_hash TEXT NOT NULL,
    arguments_json TEXT NOT NULL CHECK(json_valid(arguments_json)),
    received_json TEXT CHECK(received_json IS NULL OR json_valid(received_json)),
    outcome_json TEXT CHECK(outcome_json IS NULL OR json_valid(outcome_json)),
    delivered INTEGER NOT NULL DEFAULT 0 CHECK(delivered IN (0,1))
) STRICT;
CREATE TABLE protected_tasks(
    receipt_id TEXT PRIMARY KEY,
    connection_id TEXT NOT NULL,
    configuration_hash TEXT NOT NULL,
    sealed_task TEXT,
    arguments_json TEXT NOT NULL CHECK(json_valid(arguments_json)),
    sealed_outcome TEXT,
    sealed_literals TEXT,
    delivered INTEGER NOT NULL DEFAULT 0 CHECK(delivered IN (0,1))
) STRICT;
CREATE TABLE protected_calls(
    receipt_id TEXT PRIMARY KEY,
    connection_id TEXT NOT NULL,
    configuration_hash TEXT NOT NULL,
    arguments_json TEXT NOT NULL CHECK(json_valid(arguments_json)),
    sealed_received TEXT,
    sealed_outcome TEXT,
    sealed_literals TEXT,
    delivered INTEGER NOT NULL DEFAULT 0 CHECK(delivered IN (0,1))
) STRICT;
"""


def payload_identity(receipt: str, field: str) -> str:
    return f"tasks/1/{field}\0{receipt}"


async def protect_prior_records(database: Database, protection: ExtensionDataProtection) -> None:
    """Move each record atomically, without publishing data before its key exists."""
    await _migrate_table(
        database, protection, "pending_tasks", "protected_tasks", ("task", "outcome")
    )
    await _migrate_table(
        database, protection, "pending_calls", "protected_calls", ("received", "outcome")
    )


async def _migrate_table(
    database: Database,
    protection: ExtensionDataProtection,
    source: str,
    target: str,
    fields: tuple[str, ...],
) -> None:
    legacy = ", ".join(f"{field}_json" for field in fields)
    columns = ", ".join(f"sealed_{field}" for field in fields)
    parameters = ", ".join("?" for _ in fields)
    select = f"SELECT receipt_id, delivered, {legacy} FROM {source} LIMIT 1"
    # SELECT from the still-present source, so another opener that finished
    # the transfer cannot resurrect an already retired row.
    insert = (
        f"INSERT INTO {target} (receipt_id, connection_id, configuration_hash, "
        f"arguments_json, delivered, {columns}) SELECT receipt_id, connection_id, "
        f"configuration_hash, json_object('name', json_extract(arguments_json, "
        f"'$.name')), delivered, {parameters} FROM {source} WHERE receipt_id=?"
    )

    def prepare(row: sqlite3.Row) -> list[str | None]:
        return [
            protection.seal(payload_identity(row["receipt_id"], f"{target}.{field}"), raw.encode())
            if (raw := row[f"{field}_json"]) is not None
            and (field == "outcome" or not row["delivered"])
            else None
            for field in fields
        ]

    def transfer(receipt: str, sealed: list[str | None], db: sqlite3.Connection) -> None:
        db.execute("PRAGMA secure_delete=ON")
        db.execute(insert, (*sealed, receipt))
        db.execute(f"DELETE FROM {source} WHERE receipt_id=?", (receipt,))

    while row := await database.read_async(lambda db: db.execute(select).fetchone()):
        sealed = await database.run_async(prepare, row)
        await database.write_async(partial(transfer, row["receipt_id"], sealed))
