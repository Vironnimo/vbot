"""Coalesced revisions for the Factory's rebuildable budget projection.

Only the latest change per resource is retained. Triggers share the source
mutation's transaction, including writes from older application versions.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

type Json = dict[str, Any]
_TABLES = {
    "task": "native_tasks",
    "topic": "native_topics",
    "execution": "native_executions",
    "card": "cards",
}
SCHEMA_SQL = """
CREATE TABLE factory_budget_changes (
 revision INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, id TEXT NOT NULL,
 UNIQUE(kind, id)
) STRICT;
"""
for _kind, _table in _TABLES.items():
    for _event in ("INSERT", "UPDATE", "DELETE"):
        _alias = "OLD" if _event == "DELETE" else "NEW"
        SCHEMA_SQL += f"""
CREATE TRIGGER factory_budget_{_kind}_{_event.lower()} AFTER {_event} ON {_table} BEGIN
 INSERT OR REPLACE INTO factory_budget_changes (kind, id) VALUES ('{_kind}', {_alias}.id);
END;
"""


def backfill(connection: sqlite3.Connection) -> None:
    for kind, table in _TABLES.items():
        connection.execute(
            f"INSERT OR IGNORE INTO factory_budget_changes (kind, id) SELECT ?, id FROM {table}",
            (kind,),
        )


_PROJECTIONS = {
    "task": "id, factory_id, parent_id",
    "topic": "id, factory_id, json_extract(body_json, '$.task_id') AS task_id",
    "execution": (
        "id, factory_id, kind AS execution_kind, scope_id, group_id, "
        "json_extract(body_json, '$.runtime.runs') AS runs, "
        "json_extract(body_json, '$.runtime.tool_calls') AS tool_calls"
    ),
    "card": (
        "id, parent_id, group_id, json_extract(runtime_json, '$.native_task_id') AS task_id, "
        "json_extract(runtime_json, '$.budget.runs') AS runs, "
        "json_extract(runtime_json, '$.budget.tool_calls') AS tool_calls, "
        "(SELECT json_group_array(json_extract(value, '$.id')) "
        "FROM json_each(cards.runtime_json, '$.workflow.steps')) AS steps"
    ),
}


def read_changes(connection: sqlite3.Connection, after: int, limit: int) -> Json:
    high = connection.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = 'factory_budget_changes'"
    ).fetchone()
    changes = connection.execute(
        "SELECT kind, id, revision FROM factory_budget_changes WHERE revision > ? "
        "ORDER BY revision LIMIT ?",
        (after, limit + 1),
    ).fetchall()
    more = len(changes) > limit
    changes = changes[:limit]
    records: dict[tuple[str, str], Json] = {}
    for kind, table in _TABLES.items():
        ids = [item["id"] for item in changes if item["kind"] == kind]
        if not ids:
            continue
        for row in connection.execute(
            f"SELECT {_PROJECTIONS[kind]} FROM {table} "
            "WHERE id IN (SELECT value FROM json_each(?))",
            (json.dumps(ids),),
        ):
            value = dict(row)
            for field in ("runs", "steps"):
                if field in value:
                    value[field] = json.loads(value[field] or "[]")
            if "tool_calls" in value:
                value["tool_calls"] = value["tool_calls"] or 0
            records[(kind, row["id"])] = value
    return {
        "revision": changes[-1]["revision"] if more else (high[0] if high else 0),
        "more": more,
        "records": [
            {
                "kind": item["kind"],
                "id": item["id"],
                "value": records.get((item["kind"], item["id"])),
            }
            for item in changes
        ],
    }
