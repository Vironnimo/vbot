"""Canonical request accounting schema; Generation 1 evolves additively."""

from pathlib import Path

from core.database import (
    APPLICATION_IDS,
    CANONICAL,
    TRAILING_CAPTURE,
    DatabaseSpec,
    SnapshotFacts,
)

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
-- source_restore_id is the source database's latest restore when the cursor
-- moved (NULL: none); another one means the cursor no longer applies.
CREATE TABLE usage_imports (
    name TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    cursor INTEGER NOT NULL,
    source_restore_id TEXT
) STRICT;

-- Input estimate calibration (_calibration.py): per provider/model, decayed
-- sums of Provider-measured and locally estimated Chat request input.
CREATE TABLE input_estimate_calibration (
    model TEXT PRIMARY KEY,
    measured_tokens REAL NOT NULL,
    estimated_tokens REAL NOT NULL,
    samples INTEGER NOT NULL,
    updated_at TEXT NOT NULL
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
        # Recording goes on while a data snapshot is taken. A copy newer than the
        # Session copy stays correct: calls are keyed by id, and a new Session
        # handle replays the whole Session history (``import_session_history``).
        snapshot_capture=TRAILING_CAPTURE,
    )
