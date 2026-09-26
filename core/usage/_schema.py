"""Canonical request accounting schema; Generation 1 evolves additively."""

from pathlib import Path

from core.database import APPLICATION_IDS, CANONICAL, DatabaseSpec, SnapshotFacts

DATABASE_NAME = "model_usage"

SCHEMA_SQL = """
CREATE TABLE usage_revision (
    singleton INTEGER PRIMARY KEY,
    revision INTEGER NOT NULL
) STRICT;

CREATE TABLE usage_calls (
    id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    model TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    usage_json TEXT NOT NULL,
    agent_id TEXT,
    project_id TEXT,
    session_id TEXT,
    run_id TEXT,
    connection_id TEXT,
    session_title TEXT,
    owner_name TEXT,
    group_id TEXT
) STRICT;

-- Statistics reconciles changed calls, including updates to cumulative Usage.
CREATE UNIQUE INDEX usage_calls_by_revision ON usage_calls (revision);

-- Resumable, named Session-history import: insertion and cursor move are atomic.
CREATE TABLE usage_imports (
    name TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    cursor INTEGER NOT NULL
) STRICT;
"""


def usage_database_spec(path: Path) -> DatabaseSpec:
    return DatabaseSpec(
        name=DATABASE_NAME,
        path=path,
        profile=CANONICAL,
        application_id=APPLICATION_IDS[DATABASE_NAME],
        format_generation=1,
        schema_sql=SCHEMA_SQL,
        snapshot_facts=SnapshotFacts(
            {
                "call_count": "SELECT COUNT(*) FROM usage_calls",
                "revision": "SELECT COALESCE(MAX(revision), 0) FROM usage_revision",
            }
        ),
    )
