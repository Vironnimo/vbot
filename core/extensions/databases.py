"""Owner-bound Extension databases on the shared database kernel.

An Extension opens its SQLite state through ``host.open_database(...)`` on its
owner-bound host instead of opening files itself. Each database is a canonical
kernel database named ``ext.<owner>.<name>`` at
``<data-dir>/extension-data/<owner>/<name>.db``: the kernel supplies identity,
the data-store marker registration, additive schema evolution, data snapshots
and automatic restore. The Extension keeps its DDL, queries and meaning.

A handle belongs to one Extension registration. Shutdown, live disable and
reload release the registration, which closes its handles; the next
registration opens the database again.
"""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.database import (
    APPLICATION_IDS,
    CANONICAL,
    Database,
    DatabaseError,
    DatabaseSpec,
    Migration,
    canonical_database_path,
    open_database,
)
from core.extensions._declarations import ExtensionUnavailableError
from core.utils.ids import is_safe_id
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool

__all__ = [
    "FORMAT_GENERATION",
    "Database",
    "DatabaseError",
    "ExtensionDatabaseOpener",
    "ExtensionDatabases",
    "Migration",
    "extension_database_name",
    "extension_database_spec",
]

_LOGGER = get_logger("extensions")

# Extension databases are format generation 1 of the shared Extension identity;
# an Extension evolves its schema additively and through named migrations.
FORMAT_GENERATION = 1

ExtensionDatabaseOpener = Callable[..., Awaitable[Database]]


def extension_database_name(owner: str, name: str) -> str:
    """The kernel name ``ext.<owner>.<name>`` after validating both ids."""
    if not is_safe_id(owner):
        raise ValueError(
            f"Extension {owner!r} cannot open databases: its id must be a lowercase "
            "safe id (letters, digits, '_' or '-', starting with a letter or digit)"
        )
    if not is_safe_id(name):
        raise ValueError(
            f"Invalid Extension database name {name!r}: use a lowercase id of letters, "
            "digits, '_' or '-' that starts with a letter or digit"
        )
    return f"ext.{owner}.{name}"


def extension_database_spec(
    data_dir: Path,
    owner: str,
    name: str,
    schema_sql: str,
    *,
    migrations: Sequence[Migration] = (),
    retired_indexes: Sequence[str] = (),
) -> DatabaseSpec:
    """Declare Extension database ``name`` of ``owner`` inside ``data_dir``.

    Converters and tests use this with ``open_offline_database`` to build the
    same database an Extension opens through its host.
    """
    if not isinstance(schema_sql, str) or not schema_sql.strip():
        raise ValueError("Extension database schema_sql must contain the CREATE statements")
    database_name = extension_database_name(owner, name)
    return DatabaseSpec(
        name=database_name,
        path=canonical_database_path(data_dir, database_name),
        profile=CANONICAL,
        application_id=APPLICATION_IDS["extensions"],
        format_generation=FORMAT_GENERATION,
        schema_sql=schema_sql,
        migrations=tuple(migrations),
        retired_indexes=tuple(retired_indexes),
    )


@dataclass(frozen=True)
class _OpenDatabase:
    owner: Any
    database: Database


class ExtensionDatabases:
    """Open Extension databases per owner registration and close them on release.

    ``owner`` values are Extension registration identities: hashable, with the
    Extension id as ``.name``. A released registration can never open again;
    the reloaded Extension gets a new registration.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = Path(data_dir)
        self._changed = threading.Condition()
        self._open: dict[str, _OpenDatabase] = {}
        self._pending: dict[str, Any] = {}
        self._released: set[Any] = set()
        self._closed = False
        self._workers = BoundedWorkerPool(name="extension-databases", max_workers=2)

    def opener(self, owner: Any) -> ExtensionDatabaseOpener:
        """The ``host.open_database`` capability of one owner registration."""

        async def open_extension_database(
            name: str,
            schema_sql: str,
            migrations: Sequence[Migration] = (),
            retired_indexes: Sequence[str] = (),
        ) -> Database:
            return await self.open(
                owner,
                name,
                schema_sql,
                migrations=migrations,
                retired_indexes=retired_indexes,
            )

        return open_extension_database

    async def open(
        self,
        owner: Any,
        name: str,
        schema_sql: str,
        *,
        migrations: Sequence[Migration] = (),
        retired_indexes: Sequence[str] = (),
    ) -> Database:
        spec = extension_database_spec(
            self._data_dir,
            owner.name,
            name,
            schema_sql,
            migrations=migrations,
            retired_indexes=retired_indexes,
        )
        with self._changed:
            self._require_admission(owner)
        # Admission, opening and registration run together on a worker, so a
        # caller cancelled mid-open never leaves an untracked handle behind.
        return await self._workers.run(self._open_blocking, owner, spec)

    def _open_blocking(self, owner: Any, spec: DatabaseSpec) -> Database:
        with self._changed:
            self._require_admission(owner)
            if spec.name in self._open or spec.name in self._pending:
                raise ValueError(
                    f"Extension database {spec.name!r} is already open; "
                    "reuse the handle returned by the first open_database call"
                )
            self._pending[spec.name] = owner
        try:
            database = open_database(spec)
        except BaseException:
            with self._changed:
                del self._pending[spec.name]
                self._changed.notify_all()
            raise
        with self._changed:
            del self._pending[spec.name]
            current = not self._closed and owner not in self._released
            if current:
                self._open[spec.name] = _OpenDatabase(owner, database)
            self._changed.notify_all()
        if not current:
            database.close()
            raise ExtensionUnavailableError("Extension registration is no longer current")
        _LOGGER.info("Extension database opened (name=%s)", spec.name)
        return database

    def _require_admission(self, owner: Any) -> None:
        if self._closed or owner in self._released:
            raise ExtensionUnavailableError("Extension registration is no longer current")

    async def release(self, owner: Any) -> None:
        """Close every database of ``owner`` and refuse its later opens."""
        with self._changed:
            if self._closed:
                return
        await self._workers.run(self._release_blocking, owner)

    def _release_blocking(self, owner: Any) -> None:
        with self._changed:
            self._released.add(owner)
            # An open already running for this owner finishes first, then closes
            # itself because the owner is released.
            while owner in self._pending.values():
                self._changed.wait()
            names = [name for name, entry in self._open.items() if entry.owner == owner]
            databases = [self._open.pop(name).database for name in names]
        self._close_all(databases)

    def open_databases(self) -> tuple[Database, ...]:
        """Every open Extension database handle, for data snapshots and status."""
        with self._changed:
            entries = tuple(self._open.values())
        return tuple(entry.database for entry in entries if not entry.database.is_closed())

    def close(self) -> None:
        """Close every remaining handle and stop admitting opens (Runtime shutdown)."""
        with self._changed:
            self._closed = True
            while self._pending:
                self._changed.wait()
            databases = [entry.database for entry in self._open.values()]
            self._open.clear()
        try:
            self._close_all(databases)
        finally:
            self._workers.shutdown(wait=False)

    @staticmethod
    def _close_all(databases: Sequence[Database]) -> None:
        for database in databases:
            try:
                database.close()
            except Exception:
                _LOGGER.exception("Extension database close failed (name=%s)", database.name)
            else:
                _LOGGER.info("Extension database closed (name=%s)", database.name)
