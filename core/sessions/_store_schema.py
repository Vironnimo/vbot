"""The Session database declaration handed to the shared database kernel."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from core.database import (
    ANCHOR_CAPTURE,
    CANONICAL,
    DatabaseHealth,
    DatabaseSpec,
    Migration,
    SnapshotFacts,
)
from core.sessions import _store_archive_backfill, _store_fts, _store_working_projects
from core.sessions._store_health import SessionHealth
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


def session_database_spec(
    path: Path, *, health: SessionHealth | None = None, verify_search: bool = False
) -> DatabaseSpec:
    """Declare the canonical Session database at ``path`` (``<data-dir>/sessions.db``)."""
    data_dir = Path(path).parent
    index_health = health if health is not None else SessionHealth()

    def report_health(connection: sqlite3.Connection) -> DatabaseHealth:
        return index_health.status(connection, verify_search=verify_search)

    def adopt_archives(connection: sqlite3.Connection) -> None:
        _store_archive_backfill.adopt_legacy_archives(connection, data_dir)

    def adopt_root_projects(connection: sqlite3.Connection) -> None:
        _store_working_projects.adopt_agent_root_projects(connection, data_dir)

    return DatabaseSpec(
        name=DATABASE_NAME,
        path=Path(path),
        profile=CANONICAL,
        application_id=APPLICATION_ID,
        format_generation=FORMAT_GENERATION,
        schema_sql=SCHEMA_SQL,
        migrations=(
            # Archives written before archive entries existed become entries: the
            # legacy trees under archive/ and the archived Sessions.
            Migration("sessions.0001_archive_entries", apply=adopt_archives),
            # Sessions created before Sessions stored a working Project keep
            # working in their Agent's root Project. An older vBot ignores the
            # column and keeps using the Agent's root Project.
            Migration("sessions.0002_session_working_projects", apply=adopt_root_projects),
        ),
        after_open=_store_fts._ensure_fts_schema,
        snapshot_facts=_SNAPSHOT_FACTS,
        health=report_health,
        # Session writes, and so Runs, go on while a data snapshot is taken; the
        # snapshot stands for the instant this database is copied.
        snapshot_capture=ANCHOR_CAPTURE,
    )
