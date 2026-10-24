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
from core.sessions import (
    _store_archive_backfill,
    _store_attachments,
    _store_fts,
    _store_notifications,
    _store_values,
    _store_working_projects,
)
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
            Migration("sessions.0003_subagent_reply_targets", apply=_adopt_subagent_reply_targets),
            # Older Session writes do not maintain ownership provenance. Refuse
            # their opens rather than letting a downgrade leave trusted stale links.
            Migration(
                "sessions.0004_restore_provenance",
                breaks_older=True,
                apply=_adopt_parent_generations,
            ),
            # Blob collection relies on every writer keeping canonical references.
            Migration(
                "sessions.0005_attachment_references",
                breaks_older=True,
                apply=_store_attachments.backfill,
            ),
            Migration(
                "sessions.0006_review_lineage",
                breaks_older=True,
                apply=_adopt_review_forks,
            ),
            # Older runtimes cannot recover accepted ordinary Accessor inputs.
            Migration("sessions.0007_accessor_inputs", breaks_older=True),
            Migration(
                "sessions.0008_run_notifications",
                breaks_older=True,
                apply=_store_notifications.backfill,
            ),
        ),
        after_open=_store_fts._ensure_fts_schema,
        snapshot_facts=_SNAPSHOT_FACTS,
        health=report_health,
        # Session writes, and so Runs, go on while a data snapshot is taken; the
        # snapshot stands for the instant this database is copied.
        snapshot_capture=ANCHOR_CAPTURE,
    )


def _adopt_subagent_reply_targets(connection: sqlite3.Connection) -> None:
    """Preserve the answer destination of Sessions the user previously took over."""
    connection.execute(
        "UPDATE sessions SET metadata_json = json_set(metadata_json, "
        "'$.subagent_reply_target', 'user'), state_revision = state_revision + 1 "
        "WHERE subagent_parent_id IS NOT NULL "
        "AND json_type(metadata_json, '$.subagent_taken_over_at') = 'text' "
        "AND json_type(metadata_json, '$.subagent_reply_target') IS NULL"
    )


def _adopt_parent_generations(connection: sqlite3.Connection) -> None:
    """Identify legacy Parents only when their address has exactly one generation.

    Ambiguous addresses stay unproven: history creation times do not prove when
    a link or Working Project association was made. In particular, no migration
    guesses Working Project users for Projects that were already archived.
    """
    connection.execute(
        "WITH parents AS MATERIALIZED ("
        "SELECT project_id, agent_id, session_id, MIN(generation_id) AS generation_id "
        "FROM sessions GROUP BY project_id, agent_id, session_id HAVING COUNT(*) = 1) "
        "UPDATE sessions AS child SET subagent_parent_generation_id = parent.generation_id "
        "FROM parents AS parent "
        "WHERE child.subagent_parent_generation_id IS NULL "
        "AND child.subagent_parent_session_id IS NOT NULL "
        "AND parent.project_id = COALESCE(child.subagent_parent_project_id, '') "
        "AND parent.agent_id = child.subagent_parent_agent_id "
        "AND parent.session_id = child.subagent_parent_session_id"
    )


def _adopt_review_forks(connection: sqlite3.Connection) -> None:
    """Capture established same-scope reviews without guessing moved legacy owners."""
    connection.execute(
        "INSERT OR IGNORE INTO session_review_forks (session_key, source_key) "
        "SELECT s.session_key, p.session_key FROM sessions s "
        "JOIN sessions p ON p.session_key = s.fork_parent_key "
        "AND p.project_id = s.project_id AND p.agent_id = s.agent_id "
        "WHERE EXISTS (SELECT 1 FROM session_run_kinds k WHERE k.session_key = s.session_key "
        f"AND k.run_kind IN {_store_values._REFLECTION_RUN_KINDS_SQL})"
    )
