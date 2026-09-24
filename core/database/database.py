"""Open, evolve and serve one vBot SQLite database.

``open_database`` applies the spec's profile:

- **canonical**: the data directory's marker must authorize the database; a
  listed database that is missing or corrupt is restored from the newest
  verified data snapshot containing it; an unlisted one is created (or an
  existing unlisted file adopted after verification) and registered.
- **disposable**: a projection version mismatch, a foreign or newer file, or
  corruption discards the file with its sidecars and rebuilds it empty. Busy or
  locked is unavailable and never grounds to discard.

Every open checks identity and format generation before the journal policy may
touch the file, refuses unknown ``breaks_older`` migrations, applies the
additive reconcile and pending migrations in one ``BEGIN IMMEDIATE``
transaction, runs the owner's ``after_open`` hook and probes every declared
table and view.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from core.database._connections import (
    classified_error,
    has_live_connection,
    remove_database_files,
)
from core.database._runtime import WRITE_PATIENCE_S, ConnectionRuntime
from core.database._schema import (
    KERNEL_SCHEMA_SQL,
    DeclaredSchema,
    declared_schema,
    quote_identifier,
    schema_changes,
)
from core.database.errors import (
    DatabaseCorruptError,
    DatabaseError,
    DatabaseFormatError,
    DatabaseUnavailableError,
)
from core.database.marker import (
    MarkerEntry,
    new_database_id,
    operation_lock,
    read_marker,
    register_database,
    require_no_maintenance,
    utc_now,
    valid_database_id,
)
from core.database.spec import (
    CANONICAL,
    DISPOSABLE,
    DatabaseHealth,
    DatabaseSpec,
    canonical_data_dir,
)
from core.utils.version import detect_vbot_version
from core.utils.workers import BoundedWorkerPool

_LOGGER = logging.getLogger("vbot.database")
_Result = TypeVar("_Result")

IO_WORKERS = 8


class _ProjectionMismatchError(Exception):
    """A disposable database was built for another projection; rebuild it."""


class Database:
    """One open database: serialized writes, pooled reads, and its own worker pool.

    ``read()`` and ``write()`` block the calling thread; ``read_async``,
    ``write_async`` and ``run_async`` run blocking work on this database's
    bounded worker pool instead of the Event Loop's shared executor.
    """

    def __init__(
        self,
        spec: DatabaseSpec,
        runtime: ConnectionRuntime,
        *,
        database_id: str,
        data_dir: Path | None,
    ) -> None:
        self.spec = spec
        self._runtime = runtime
        self._database_id = database_id
        self._data_dir = data_dir
        self._workers = BoundedWorkerPool(name=f"db-{spec.name}", max_workers=IO_WORKERS)

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def path(self) -> Path:
        return self.spec.path

    @property
    def database_id(self) -> str:
        return self._database_id

    @property
    def data_dir(self) -> Path | None:
        """The data directory of a marker-registered canonical database."""
        return self._data_dir

    @property
    def writer(self) -> sqlite3.Connection:
        """The serialized writer connection, for diagnostics and tests only."""
        return self._runtime.writer

    def read(self) -> contextlib.AbstractContextManager[sqlite3.Connection]:
        """One read transaction on a pooled reader, or on the writer without WAL."""
        return self._runtime.read_ctx()

    def write(
        self,
        operation: Callable[[sqlite3.Connection], _Result],
        *,
        patience_s: float = WRITE_PATIENCE_S,
    ) -> _Result:
        """Run ``operation`` in one ``BEGIN IMMEDIATE`` transaction with busy retry."""
        return self._runtime.execute_write(operation, patience_s=patience_s)  # type: ignore[no-any-return]

    async def run_async(
        self, function: Callable[..., _Result], *arguments: Any, **keyword_arguments: Any
    ) -> _Result:
        """Run blocking work that uses this database on its bounded worker pool.

        A closed database raises :class:`DatabaseUnavailableError`, including when
        ``close()`` shut the pool down while this call waited for admission.
        """
        if self.is_closed():
            raise DatabaseUnavailableError(f"{self.name} is closed")
        try:
            return await self._workers.run(function, *arguments, **keyword_arguments)
        except RuntimeError as exc:
            if self.is_closed():
                raise DatabaseUnavailableError(f"{self.name} is closed") from exc
            raise

    async def read_async(self, operation: Callable[[sqlite3.Connection], _Result]) -> _Result:
        return await self.run_async(self._read_operation, operation)

    async def write_async(
        self,
        operation: Callable[[sqlite3.Connection], _Result],
        *,
        patience_s: float = WRITE_PATIENCE_S,
    ) -> _Result:
        return await self.run_async(self.write, operation, patience_s=patience_s)

    def _read_operation(self, operation: Callable[[sqlite3.Connection], _Result]) -> _Result:
        with self.read() as connection:
            return operation(connection)

    def backup(self, destination: Path, *, cancelled: Callable[[], bool] | None = None) -> bool:
        """Write one consistent standalone copy; ``False`` when ``cancelled`` stopped it."""
        return self._runtime.backup(destination, cancelled=cancelled)

    def checkpoint(self) -> None:
        self._runtime.checkpoint()

    def verify_read_write(self) -> None:
        """Exercise the read/write path without changing persistent rows."""

        def verify(connection: sqlite3.Connection) -> None:
            connection.execute("CREATE TEMP TABLE kernel_verify(value INTEGER NOT NULL)")
            try:
                connection.execute("INSERT INTO kernel_verify(value) VALUES (1)")
                row = connection.execute("SELECT value FROM kernel_verify").fetchone()
                if row is None or int(row[0]) != 1:
                    raise DatabaseUnavailableError(f"{self.name}: read/write verification failed")
            finally:
                connection.execute("DROP TABLE IF EXISTS temp.kernel_verify")

        self.write(verify)

    def health(self) -> DatabaseHealth:
        """The owner's health report; a failing check reports degraded."""
        if self.spec.health is None:
            return DatabaseHealth("healthy")
        try:
            with self.read() as connection:
                return self.spec.health(connection)
        except Exception as exc:
            return DatabaseHealth("degraded", f"health check failed: {exc}")

    def wal_active(self) -> bool:
        return self._runtime.wal_active()

    def reader_stats(self) -> tuple[int, int]:
        return self._runtime.reader_stats()

    def live_connection_count(self) -> int:
        return self._runtime.live_connection_count()

    def is_closed(self) -> bool:
        return self._runtime.is_closed()

    def close(self) -> None:
        """Close every connection; in-flight pool work fails as unavailable."""
        self._runtime.close()
        self._workers.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Opening
# ---------------------------------------------------------------------------


def open_database(spec: DatabaseSpec) -> Database:
    """Open ``spec`` under its profile's rules; see the module docstring."""
    if spec.profile == CANONICAL:
        return _open_canonical(spec)
    return _open_disposable(spec)


def open_offline_database(spec: DatabaseSpec) -> Database:
    """Create or open a canonical database outside the marker checks.

    For converters, staging directories and snapshot copies: the file may sit
    anywhere, no marker or maintenance guard is consulted, nothing is registered
    and nothing is restored. The kernel checks, reconcile and migrations still
    apply.
    """
    if spec.profile != CANONICAL:
        raise ValueError("offline opening is for canonical databases")
    if not _has_content(spec.path):
        _create_database_file(spec)
    return _open_existing(spec, expected_database_id=None, data_dir=None)


def _open_canonical(spec: DatabaseSpec) -> Database:
    from core.database.recovery import auto_restore_if_needed, pending_restore

    data_dir = canonical_data_dir(spec)
    require_no_maintenance(data_dir)
    marker = read_marker(data_dir)
    if marker is None:
        raise DatabaseFormatError(
            f"the data directory does not authorize a current-format data store: {data_dir}; "
            "initialize the data directory or install converted databases first"
        )
    entry = marker.databases.get(spec.name)
    if entry is None:
        return _bootstrap_canonical(spec, data_dir)
    _require_generation(spec, entry)
    if pending_restore(data_dir, spec.name):
        auto_restore_if_needed(data_dir, spec, entry.database_id)
    if not spec.path.exists() and not auto_restore_if_needed(data_dir, spec, entry.database_id):
        raise DatabaseUnavailableError(
            f"the {spec.name} database is missing although the data store lists it: "
            f"{spec.path}; no verified data snapshot could restore it"
        )
    try:
        return _open_existing(spec, expected_database_id=entry.database_id, data_dir=data_dir)
    except (DatabaseCorruptError, OSError):
        if auto_restore_if_needed(data_dir, spec, entry.database_id):
            return _open_existing(spec, expected_database_id=entry.database_id, data_dir=data_dir)
        raise


def _require_generation(spec: DatabaseSpec, entry: MarkerEntry) -> None:
    if entry.format_generation > spec.format_generation:
        raise DatabaseFormatError(
            f"the {spec.name} database is format generation {entry.format_generation}, "
            f"newer than this vBot supports ({spec.format_generation})"
        )
    if entry.format_generation != spec.format_generation:
        raise DatabaseFormatError(
            f"the {spec.name} database is format generation {entry.format_generation}; "
            f"this vBot needs generation {spec.format_generation}. Run the converter first"
        )


def _bootstrap_canonical(spec: DatabaseSpec, data_dir: Path) -> Database:
    """Create or adopt an unlisted canonical database, then register it."""
    with operation_lock(data_dir):
        marker = read_marker(data_dir)
        listed = marker is not None and spec.name in marker.databases
        created = False
        if not listed and not _has_content(spec.path):
            _create_database_file(spec)
            created = True
    if listed:
        # Another opener registered it while this one waited for the lock.
        return _open_canonical(spec)
    database = _open_existing(spec, expected_database_id=None, data_dir=data_dir)
    try:
        if not created:
            _LOGGER.warning(
                "Registering the existing unlisted %s database %s after verification",
                spec.name,
                spec.path,
            )
        register_database(
            data_dir,
            spec.name,
            MarkerEntry(database.database_id, spec.format_generation),
        )
    except BaseException:
        database.close()
        raise
    return database


def _open_disposable(spec: DatabaseSpec) -> Database:
    for attempt in range(2):
        if not _has_content(spec.path):
            _discard(spec)
            _create_database_file(spec)
        try:
            return _open_existing(spec, expected_database_id=None, data_dir=None)
        except DatabaseUnavailableError:
            raise
        except (DatabaseError, _ProjectionMismatchError, sqlite3.DatabaseError, OSError) as exc:
            if attempt:
                if isinstance(exc, DatabaseError):
                    raise
                raise DatabaseCorruptError(f"{spec.name}: rebuilt projection is unusable") from exc
            _LOGGER.warning("Discarding the %s projection at %s: %s", spec.name, spec.path, exc)
            _discard(spec)
    raise AssertionError("unreachable")


def _discard(spec: DatabaseSpec) -> None:
    """Delete a disposable database with its sidecars, never under a live connection."""
    if has_live_connection(spec.path):
        raise DatabaseUnavailableError(
            f"{spec.name}: the projection is still open in this process and cannot be rebuilt"
        )
    remove_database_files(spec.path)
    if any(Path(f"{spec.path}{suffix}").exists() for suffix in ("", "-wal", "-shm", "-journal")):
        raise DatabaseUnavailableError(f"{spec.name}: the projection files cannot be removed")


def _has_content(path: Path) -> bool:
    try:
        return path.stat().st_size > 0
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise DatabaseUnavailableError(f"database file is unavailable: {path}") from exc


def _declared(spec: DatabaseSpec) -> DeclaredSchema:
    declared = declared_schema(KERNEL_SCHEMA_SQL + spec.schema_sql)
    retired = set(spec.retired_indexes).intersection(declared.object_text)
    if retired:
        raise ValueError(f"{spec.name}: retired indexes are still declared: {sorted(retired)}")
    return declared


def _create_database_file(spec: DatabaseSpec) -> None:
    """Build a complete new database beside ``spec.path`` and publish it atomically.

    The file at the final path is either absent or complete: identity, schema,
    and every declared migration recorded as applied.
    """
    declared = _declared(spec)
    path = spec.path
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.creating")
    now = utc_now()
    version = detect_vbot_version()
    meta = {
        "database_id": new_database_id(),
        "database_name": spec.name,
        "format_generation": str(spec.format_generation),
        "created_at": now,
        "created_by_version": version,
    }
    if spec.projection_version is not None:
        meta["projection_version"] = str(spec.projection_version)
    try:
        connection = sqlite3.connect(temporary, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            for _kind, _name, sql in declared.objects:
                connection.execute(sql)
            connection.executemany(
                "INSERT INTO kernel_meta(key, value) VALUES (?, ?)", sorted(meta.items())
            )
            connection.executemany(
                "INSERT INTO kernel_migrations(name, applied_at, applied_by_version, breaks_older) "
                "VALUES (?, ?, ?, ?)",
                [
                    (migration.name, now, version, int(migration.breaks_older))
                    for migration in spec.migrations
                ],
            )
            connection.execute(f"PRAGMA application_id = {int(spec.application_id)}")
            connection.execute(f"PRAGMA user_version = {int(spec.format_generation)}")
            connection.execute("COMMIT")
        finally:
            connection.close()
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except sqlite3.Error as exc:
        remove_database_files(temporary)
        translated = classified_error(exc, f"{spec.name}: the new database cannot be created")
        raise (translated or DatabaseUnavailableError(f"{spec.name}: creation failed")) from exc
    except OSError as exc:
        remove_database_files(temporary)
        raise DatabaseUnavailableError(f"{spec.name}: the new database cannot be created") from exc
    except BaseException:
        remove_database_files(temporary)
        raise


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _open_existing(
    spec: DatabaseSpec, *, expected_database_id: str | None, data_dir: Path | None
) -> Database:
    runtime = ConnectionRuntime(
        spec.path,
        name=spec.name,
        synchronous="FULL" if spec.profile == CANONICAL else "NORMAL",
        application_id=spec.application_id,
        format_generation=spec.format_generation,
    )
    identity: dict[str, str] = {}

    def verify(connection: sqlite3.Connection) -> None:
        identity.update(_verify_identity(connection, spec, expected_database_id))

    try:
        writer = runtime.open_writer(verify)
        declared = _declared(spec)
        try:
            _evolve(writer, spec, declared)
            if spec.after_open is not None:
                spec.after_open(writer)
            _probe_structure(writer, spec, declared)
        except sqlite3.Error as exc:
            translated = classified_error(exc, f"{spec.name}: the database cannot be opened")
            if translated is None and isinstance(exc, sqlite3.DatabaseError):
                translated = DatabaseCorruptError(f"{spec.name}: the database cannot be opened")
            if translated is None:
                raise
            raise translated from exc
    except BaseException:
        runtime.close()
        raise
    return Database(spec, runtime, database_id=identity["database_id"], data_dir=data_dir)


def _verify_identity(
    connection: sqlite3.Connection, spec: DatabaseSpec, expected_database_id: str | None
) -> dict[str, str]:
    """Check identity, generation, projection version and the ledger, read-only."""
    application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
    generation = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if application_id != spec.application_id:
        raise DatabaseCorruptError(f"{spec.path} is not a vBot {spec.name} database")
    if generation > spec.format_generation:
        raise DatabaseFormatError(
            f"{spec.path} is format generation {generation}, newer than this vBot supports "
            f"({spec.format_generation})"
        )
    if generation != spec.format_generation:
        raise DatabaseFormatError(
            f"{spec.path} is format generation {generation}; this vBot needs generation "
            f"{spec.format_generation}. Run the converter first"
        )
    try:
        rows = connection.execute("SELECT key, value FROM kernel_meta").fetchall()
        ledger = connection.execute("SELECT name, breaks_older FROM kernel_migrations").fetchall()
    except sqlite3.OperationalError as exc:
        if classified_error(exc, "") is not None:
            raise
        raise DatabaseCorruptError(f"{spec.path} has no kernel identity") from exc
    identity = {str(key): str(value) for key, value in rows}
    if identity.get("database_name") != spec.name:
        raise DatabaseCorruptError(
            f"{spec.path} records database {identity.get('database_name')!r}, not {spec.name}"
        )
    if identity.get("format_generation") != str(generation):
        raise DatabaseCorruptError(f"{spec.path} records an inconsistent format generation")
    database_id = identity.get("database_id")
    if not valid_database_id(database_id):
        raise DatabaseCorruptError(f"{spec.path} has an invalid database identity")
    if expected_database_id is not None and database_id != expected_database_id:
        raise DatabaseCorruptError(f"{spec.path} identity does not match the data-store marker")
    if spec.profile == DISPOSABLE and identity.get("projection_version") != str(
        spec.projection_version
    ):
        raise _ProjectionMismatchError(
            f"projection version {identity.get('projection_version')} is not "
            f"{spec.projection_version}"
        )
    _refuse_unknown_breaking_migrations(spec, ledger)
    return identity


def _refuse_unknown_breaking_migrations(spec: DatabaseSpec, ledger: list[Any]) -> None:
    declared = {migration.name for migration in spec.migrations}
    breaking = sorted(
        str(name) for name, breaks_older in ledger if str(name) not in declared and breaks_older
    )
    if breaking:
        raise DatabaseFormatError(
            f"the {spec.name} database was changed by a newer vBot in a way this version "
            f"cannot read (migration {', '.join(breaking)}); update vBot or restore the "
            "data snapshot taken before that update"
        )


def _evolve(writer: sqlite3.Connection, spec: DatabaseSpec, declared: DeclaredSchema) -> None:
    """Apply the additive reconcile, retired-index drops and pending migrations atomically."""
    planned = schema_changes(writer, declared, retired_indexes=spec.retired_indexes)
    recorded = _recorded_migrations(writer)
    if not planned and all(migration.name in recorded for migration in spec.migrations):
        return
    writer.execute("BEGIN IMMEDIATE")
    try:
        planned = schema_changes(writer, declared, retired_indexes=spec.retired_indexes)
        for statement, _description in planned:
            writer.execute(statement)
        ledger = writer.execute("SELECT name, breaks_older FROM kernel_migrations").fetchall()
        _refuse_unknown_breaking_migrations(spec, ledger)
        recorded = {str(row[0]) for row in ledger}
        pending = [migration for migration in spec.migrations if migration.name not in recorded]
        now = utc_now()
        version = detect_vbot_version() if pending else ""
        for migration in pending:
            if migration.apply is not None:
                migration.apply(writer)
            writer.execute(
                "INSERT INTO kernel_migrations(name, applied_at, applied_by_version, breaks_older) "
                "VALUES (?, ?, ?, ?)",
                (migration.name, now, version, int(migration.breaks_older)),
            )
        writer.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(BaseException):
            if writer.in_transaction:
                writer.execute("ROLLBACK")
        raise
    if planned or pending:
        _LOGGER.info(
            "Evolved the %s database at %s: %s",
            spec.name,
            spec.path,
            "; ".join(
                [description for _statement, description in planned]
                + [f"applied migration {migration.name}" for migration in pending]
            ),
        )


def _recorded_migrations(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute("SELECT name FROM kernel_migrations")}


def _probe_structure(
    writer: sqlite3.Connection, spec: DatabaseSpec, declared: DeclaredSchema
) -> None:
    """Read one row from every declared table and view; SQLite errors classify upstream."""
    del spec
    for relation in declared.readable_relations:
        writer.execute(f"SELECT 1 FROM {quote_identifier(relation)} LIMIT 1").fetchone()
