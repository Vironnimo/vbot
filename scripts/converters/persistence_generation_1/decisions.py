"""Generation 1 conversion of ``decisions.db``.

Before Generation 1 the Decisions store opened ``decisions.db`` itself, without
an application id or format generation. This area copies its experiments and
evaluations, with their ids, evaluation sequences and statuses, into a new
kernel database staged at the same path, and rewrites the timestamps in the
canonical form. Rows whose JSON documents cannot be read, and evaluations of a
missing experiment, are dropped and reported. An evaluation still marked
running is copied as is: the Decisions store marks it interrupted on its first
start, as it did before.
"""

from __future__ import annotations

import json
import sqlite3

from core.database import APPLICATION_IDS, open_offline_database
from core.model_tasks.decision_store import FORMAT_GENERATION, decision_database_spec
from scripts.converters.persistence_generation_1._context import (
    ConversionContext,
    ConversionError,
)
from scripts.converters.persistence_generation_1._legacy_sqlite import (
    LEGACY_IDENTITY,
    LegacyTable,
    RowCheck,
    Tally,
    copy_table,
    identity,
    open_legacy,
    remove_staged,
    require_columns,
    retire_sidecars,
    table_columns,
)

AREA = "decisions"
DATABASE = "decisions.db"

# The pre-Generation-1 tables, in foreign-key order.
_EXPERIMENTS = LegacyTable(
    "experiments",
    ("id", "title", "revision", "draft", "created_at", "updated_at"),
    timestamps=("created_at", "updated_at"),
)
_EVALUATIONS = LegacyTable(
    "evaluations",
    (
        "sequence",
        "id",
        "experiment_id",
        "request_id",
        "status",
        "snapshot",
        "result",
        "error",
        "created_at",
        "completed_at",
    ),
    timestamps=("created_at", "completed_at"),
    key=("id",),
)


def convert(context: ConversionContext) -> None:
    """Stage the Generation 1 ``decisions.db`` from the source database."""
    source_path = context.source_path(DATABASE)
    if not source_path.is_file():
        context.report.count(AREA, "source_missing")
        return
    with open_legacy(source_path) as source:
        found = identity(source)
        if found == (APPLICATION_IDS["decisions"], FORMAT_GENERATION):
            context.report.count(AREA, "already_current")
            return
        if found != LEGACY_IDENTITY:
            raise ConversionError(
                f"{source_path} is not a pre-Generation-1 decisions database "
                f"(application_id={found[0]}, user_version={found[1]})"
            )
        tables = []
        for table in (_EXPERIMENTS, _EVALUATIONS):
            present = table_columns(source, table.name)
            if present is None:
                context.report.skip(AREA, table.name, "table missing in the source; none copied")
                continue
            require_columns(source_path, table, present)
            tables.append(table)

        target_path = context.staged(DATABASE)
        remove_staged(target_path)
        database = open_offline_database(decision_database_spec(target_path))
        try:

            def operation(connection: sqlite3.Connection) -> Tally:
                tally = Tally()
                for table in tables:
                    copy_table(source, connection, table, tally, check=_CHECKS[table.name])
                return tally

            database.write(operation).publish(context, AREA)
        finally:
            database.close()
    retire_sidecars(context, DATABASE)


def _json_columns(*columns: str, nullable: tuple[str, ...] = ()) -> RowCheck:
    def check(row: sqlite3.Row) -> str | None:
        for column in (*columns, *nullable):
            value = row[column]
            if value is None and column in nullable:
                continue
            try:
                json.loads(value)
            except (TypeError, ValueError):
                return f"{column} is not valid JSON"
        return None

    return check


_CHECKS = {
    _EXPERIMENTS.name: _json_columns("draft"),
    _EVALUATIONS.name: _json_columns("snapshot", nullable=("result", "error")),
}
