"""The Session database declaration handed to the shared database kernel."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from core.database import CANONICAL, DatabaseHealth, DatabaseSpec, SnapshotFacts
from core.sessions import _store_fts
from core.sessions.schema import APPLICATION_ID, DATABASE_NAME, FORMAT_GENERATION, SCHEMA_SQL

# Owner facts every data snapshot records for the Session member and
# re-verifies on its copy: row counts and revision watermarks.
_SNAPSHOT_FACTS = SnapshotFacts(
    {
        "session_count": "SELECT COUNT(*) FROM sessions",
        "entry_count": "SELECT COUNT(*) FROM entries",
        "latest_history_revision": "SELECT COALESCE(MAX(history_revision), 0) FROM sessions",
        "latest_state_revision": "SELECT COALESCE(MAX(state_revision), 0) FROM sessions",
    }
)


def session_database_spec(path: Path) -> DatabaseSpec:
    """Declare the canonical Session database at ``path`` (``<data-dir>/sessions.db``)."""
    return DatabaseSpec(
        name=DATABASE_NAME,
        path=Path(path),
        profile=CANONICAL,
        application_id=APPLICATION_ID,
        format_generation=FORMAT_GENERATION,
        schema_sql=SCHEMA_SQL,
        after_open=_store_fts._ensure_fts_schema,
        snapshot_facts=_SNAPSHOT_FACTS,
        health=_session_health,
    )


def _session_health(connection: sqlite3.Connection) -> DatabaseHealth:
    """Report the derived search index; canonical rows are verified by the kernel."""
    fts = _store_fts._fts_health_from_connection(connection, verify_coverage=True)
    return DatabaseHealth(
        "healthy" if fts.available else "degraded",
        None if fts.available else f"Session search: {fts.reason}",
        {
            "fts": {
                "state": fts.state,
                "reason": fts.reason,
                "generation": fts.generation,
                "target_high_water": fts.target_high_water,
                "completed_high_water": fts.completed_high_water,
            }
        },
    )
