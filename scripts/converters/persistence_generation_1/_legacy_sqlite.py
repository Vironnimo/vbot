"""Copying pre-Generation-1 SQLite tables into their Generation 1 databases.

The source database is opened read-only and never modified. Each area names
the tables and columns it copies, frozen from the pre-Generation-1 schema, so
the old schema knowledge stays in the converter. Rows are copied in rowid order
with their keys, and timestamps are rewritten in the canonical Generation 1
form. A row the Generation 1 database refuses (a broken reference or a
duplicate key) is skipped and reported instead of failing the whole area.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from core.utils import timestamps
from scripts.converters.persistence_generation_1._context import (
    ConversionContext,
    ConversionError,
)

# Pre-Generation-1 databases carried no application id and no format generation.
LEGACY_IDENTITY = (0, 0)

_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


@dataclass(frozen=True, slots=True)
class LegacyTable:
    """One source table copied column for column into the same Generation 1 table.

    ``key`` names the columns that identify a row in the report; ``where`` limits
    the copied rows (a condition in SQL, without ``WHERE``).
    """

    name: str
    columns: tuple[str, ...]
    timestamps: tuple[str, ...] = ()
    key: tuple[str, ...] = ()
    where: str | None = None

    def describe(self, row: sqlite3.Row) -> str:
        key = self.key or self.columns[:1]
        return f"{self.name} " + "/".join(str(row[column]) for column in key)


@dataclass(slots=True)
class Tally:
    """Counts and skipped rows of one area, published once its write committed.

    The kernel retries a busy write transaction as a whole, so an area collects
    into a fresh tally per attempt and publishes only the successful one.
    """

    counts: dict[str, int] = field(default_factory=dict)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    def count(self, key: str, amount: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + amount

    def skip(self, item: str, reason: str) -> None:
        self.skipped.append((item, reason))

    def publish(self, context: ConversionContext, area: str) -> None:
        for key, amount in self.counts.items():
            context.report.count(area, key, amount)
        for item, reason in self.skipped:
            context.report.skip(area, item, reason)


RowCheck = Callable[[sqlite3.Row], str | None]


@contextmanager
def open_legacy(path: Path) -> Iterator[sqlite3.Connection]:
    """Open ``path`` read-only for one area, closing it afterwards."""
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as error:
        raise ConversionError(f"cannot open {path} read-only: {error}") from error
    try:
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        except sqlite3.Error as error:
            raise ConversionError(f"cannot read {path}: {error}") from error
        yield connection
    finally:
        connection.close()


def identity(connection: sqlite3.Connection) -> tuple[int, int]:
    """The source's ``(application_id, user_version)``."""
    application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    return application_id, user_version


def table_columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...] | None:
    """The source table's column names, or ``None`` when the table does not exist."""
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    return tuple(str(row["name"]) for row in rows) or None


def source_tables(connection: sqlite3.Connection) -> tuple[str, ...]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()
    return tuple(str(row["name"]) for row in rows)


def require_columns(source_path: Path, table: LegacyTable, present: tuple[str, ...]) -> None:
    missing = [column for column in table.columns if column not in present]
    if missing:
        raise ConversionError(
            f"{source_path}: table {table.name} lacks the columns {', '.join(missing)}; "
            "it does not have the pre-Generation-1 schema this converter reads"
        )


def canonical_timestamp(value: object) -> str | None:
    """``YYYY-MM-DDTHH:MM:SS.ffffffZ`` for an ISO 8601 value with an offset, else ``None``."""
    if not isinstance(value, str):
        return None
    try:
        return timestamps.canonical_timestamp(value)
    except ValueError:
        return None


def copy_table(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    table: LegacyTable,
    tally: Tally,
    *,
    check: RowCheck | None = None,
) -> None:
    """Copy ``table``'s rows in rowid order, counting them under the table name.

    ``check`` returns a reason to skip a row, or ``None`` to copy it. A timestamp
    that cannot be read as ISO 8601 with an offset is copied unchanged and
    reported.
    """
    columns = ",".join(table.columns)
    where = f" WHERE {table.where}" if table.where else ""
    insert = f"INSERT INTO {table.name}({columns}) VALUES({','.join('?' * len(table.columns))})"
    tally.count(table.name, 0)
    for row in source.execute(f"SELECT {columns} FROM {table.name}{where} ORDER BY rowid"):
        reason = check(row) if check is not None else None
        if reason is not None:
            tally.skip(table.describe(row), f"row dropped: {reason}")
            continue
        values = []
        for column in table.columns:
            value = row[column]
            if column in table.timestamps and value is not None:
                canonical = canonical_timestamp(value)
                if canonical is None:
                    tally.skip(
                        table.describe(row), f"{column} {value!r} kept: not an ISO 8601 timestamp"
                    )
                else:
                    value = canonical
            values.append(value)
        try:
            target.execute(insert, values)
        except sqlite3.IntegrityError as error:
            tally.skip(table.describe(row), f"row dropped: {error}")
            continue
        tally.count(table.name)


def remove_staged(path: Path) -> None:
    """Remove an earlier run's staged database, so a repeated run replaces it."""
    for suffix in ("", *_SIDECAR_SUFFIXES):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def retire_sidecars(context: ConversionContext, relative: str) -> None:
    """Retire the source's journal files: they must not meet the new database file."""
    for suffix in _SIDECAR_SUFFIXES:
        if context.source_path(f"{relative}{suffix}").exists():
            context.retire(f"{relative}{suffix}")
