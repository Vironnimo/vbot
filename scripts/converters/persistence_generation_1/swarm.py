"""Generation 1 conversion of the bundled Swarm Extension's database.

Before Generation 1 the Swarm store opened ``extension-data/swarm/swarm.db``
itself. This area copies every Swarm table, with its keys, into a new kernel
database for the Extension database ``ext.swarm.swarm`` staged at the same path,
and rewrites the timestamps in the canonical form. The signed-cursor key in
``swarm_meta`` is kept, so continuation cursors stay valid.

Generation 1 drops the passive decision tables of the removed Swarm Decisions
Tool (no API read them) and the request receipts of that Tool. A row the new
database refuses, such as one that references a missing Swarm, is dropped and
reported.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from core.database import APPLICATION_IDS, DatabaseSpec, open_offline_database
from core.extensions.databases import FORMAT_GENERATION, extension_database_spec
from resources.extensions.swarm.store import DATABASE_NAME, SCHEMA_SQL
from scripts.converters.persistence_generation_1._context import (
    ConversionContext,
    ConversionError,
)
from scripts.converters.persistence_generation_1._legacy_sqlite import (
    LEGACY_IDENTITY,
    LegacyTable,
    Tally,
    copy_table,
    identity,
    open_legacy,
    remove_staged,
    require_columns,
    retire_sidecars,
    source_tables,
    table_columns,
)

AREA = "swarm"
OWNER = "swarm"
DATABASE = f"extension-data/{OWNER}/{DATABASE_NAME}.db"

_DECISION_REQUEST_SCOPE = "decisions:"

# The pre-Generation-1 Swarm tables, in foreign-key order, frozen with the
# columns the converter copies.
_TABLES = (
    LegacyTable("swarm_meta", ("key", "value")),
    LegacyTable(
        "profiles",
        ("id", "slug", "name", "revision", "payload", "created_at", "updated_at"),
        timestamps=("created_at", "updated_at"),
    ),
    LegacyTable(
        "swarms",
        ("id", "prompt", "profile_snapshot", "effective_configuration", "state", "created_at"),
        timestamps=("created_at",),
    ),
    LegacyTable(
        "participants",
        (
            "id",
            "swarm_id",
            "model",
            "display_name",
            "ordinal",
            "state",
            "idle_boundary",
            "wake_announced_seq",
            "wake_epoch",
            "wake_pending",
            "wake_pending_seq",
            "lifecycle_run_id",
        ),
    ),
    LegacyTable(
        "participant_sessions",
        (
            "participant_id",
            "project_id",
            "agent_id",
            "session_id",
            "generation_id",
            "owner_name",
        ),
    ),
    LegacyTable("swarm_settings", ("swarm_id", "revision", "delivery_json")),
    LegacyTable("swarm_epochs", ("swarm_id", "epoch", "is_open")),
    LegacyTable(
        "swarm_execution_epochs",
        ("swarm_id", "epoch", "execution_epoch"),
        key=("swarm_id", "epoch"),
    ),
    LegacyTable(
        "swarm_events",
        (
            "id",
            "swarm_id",
            "kind",
            "actor",
            "old_json",
            "new_json",
            "settings_revision",
            "created_at",
        ),
        timestamps=("created_at",),
    ),
    LegacyTable(
        "discussions",
        ("id", "swarm_id", "title", "sequence", "is_main", "created_at"),
        timestamps=("created_at",),
    ),
    LegacyTable(
        "memberships",
        ("discussion_id", "participant_id"),
        key=("discussion_id", "participant_id"),
    ),
    LegacyTable(
        "posts",
        (
            "id",
            "swarm_id",
            "discussion_id",
            "sequence",
            "author_kind",
            "author_id",
            "author_name",
            "text",
            "reply_to",
            "recipients_json",
            "created_at",
        ),
        timestamps=("created_at",),
    ),
    LegacyTable(
        "recipients",
        (
            "post_id",
            "participant_id",
            "route_class",
            "delivered_at",
            "receipt_id",
            "content_hash",
            "effect_kind",
            "carrier_kind",
            "carrier_sequence",
            "prepared_at",
        ),
        timestamps=("delivered_at", "prepared_at"),
        key=("post_id", "participant_id"),
    ),
    LegacyTable(
        "delivery_batches",
        (
            "receipt_id",
            "participant_id",
            "content_hash",
            "effect_kind",
            "created_at",
            "acknowledged_at",
            "carrier_kind",
            "carrier_sequence",
            "settings_revision",
        ),
        timestamps=("created_at", "acknowledged_at"),
    ),
    LegacyTable(
        "delivery_batch_entries",
        ("receipt_id", "post_id", "participant_id"),
        key=("receipt_id", "post_id", "participant_id"),
    ),
    LegacyTable(
        "requests",
        ("scope", "request_id", "payload_hash", "outcome"),
        key=("scope", "request_id"),
        where=f"substr(scope,1,{len(_DECISION_REQUEST_SCOPE)})<>'{_DECISION_REQUEST_SCOPE}'",
    ),
    LegacyTable("swarm_goals", ("swarm_id", "post_id")),
    LegacyTable("wiki_pages", ("id", "swarm_id", "revision")),
    LegacyTable(
        "wiki_revisions",
        (
            "id",
            "swarm_id",
            "page_id",
            "revision",
            "title",
            "content",
            "deleted",
            "author_id",
            "author_name",
            "author_kind",
            "created_at",
        ),
        timestamps=("created_at",),
    ),
)

# Passive tables of the removed Swarm Decisions Tool; Generation 1 drops them.
_DROPPED_TABLES = ("decision_events", "decision_positions", "decision_questions")


def database_spec(root: Path) -> DatabaseSpec:
    """The Swarm Extension's database in data directory ``root``, as the Extension opens it."""
    return extension_database_spec(root, OWNER, DATABASE_NAME, SCHEMA_SQL)


def check_source(source: Path) -> None:
    """Refuse a Swarm database this area cannot read, before anything is staged."""
    source_path = source / DATABASE
    if source_path.is_file():
        with open_legacy(source_path) as connection:
            _is_current(source_path, connection)


def convert(context: ConversionContext) -> None:
    """Stage the Generation 1 Swarm database from the source database."""
    source_path = context.source_path(DATABASE)
    if not source_path.is_file():
        context.report.count(AREA, "source_missing")
        return
    with open_legacy(source_path) as source:
        if _is_current(source_path, source):
            context.report.count(AREA, "already_current")
            return
        tables = _copied_tables(context, source)
        _report_dropped(context, source)

        spec = database_spec(context.staging)
        target_path = context.staged(DATABASE)
        if spec.path != target_path:
            raise ConversionError(f"the Swarm database is expected at {spec.path}")
        remove_staged(target_path)
        database = open_offline_database(spec)
        try:

            def operation(connection: sqlite3.Connection) -> Tally:
                tally = Tally()
                for table in tables:
                    copy_table(source, connection, table, tally)
                return tally

            database.write(operation).publish(context, AREA)
        finally:
            database.close()
    retire_sidecars(context, DATABASE)


def _is_current(source_path: Path, source: sqlite3.Connection) -> bool:
    """Whether the source is already Generation 1; refuse a foreign database."""
    found = identity(source)
    if found == (APPLICATION_IDS["extensions"], FORMAT_GENERATION):
        return True
    if found != LEGACY_IDENTITY:
        raise ConversionError(
            f"{source_path} is not a pre-Generation-1 Swarm database "
            f"(application_id={found[0]}, user_version={found[1]})"
        )
    for table in _TABLES:
        present = table_columns(source, table.name)
        if present is not None:
            require_columns(source_path, table, present)
    return False


def _copied_tables(context: ConversionContext, source: sqlite3.Connection) -> list[LegacyTable]:
    tables = []
    for table in _TABLES:
        present = table_columns(source, table.name)
        if present is None:
            context.report.skip(AREA, table.name, "table missing in the source; none copied")
            continue
        for column in present:
            if column not in table.columns:
                context.report.skip(AREA, f"{table.name}.{column}", "unknown source column dropped")
        tables.append(table)
    known = {table.name for table in _TABLES} | set(_DROPPED_TABLES)
    for name in source_tables(source):
        if name not in known:
            context.report.skip(AREA, name, "unknown source table dropped")
    return tables


def _report_dropped(context: ConversionContext, source: sqlite3.Connection) -> None:
    present = set(source_tables(source))
    for name in _DROPPED_TABLES:
        if name not in present:
            continue
        rows = int(source.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
        context.report.count(AREA, f"{name}_dropped", rows)
        if rows:
            context.report.skip(
                AREA, name, f"{rows} rows of the removed Swarm Decisions Tool dropped"
            )
    if "requests" in present:
        rows = int(
            source.execute(
                "SELECT COUNT(*) FROM requests WHERE substr(scope,1,?)=?",
                (len(_DECISION_REQUEST_SCOPE), _DECISION_REQUEST_SCOPE),
            ).fetchone()[0]
        )
        context.report.count(AREA, "decision_requests_dropped", rows)
        if rows:
            context.report.skip(
                AREA,
                "requests",
                f"{rows} request receipts of the removed Swarm Decisions Tool dropped",
            )
