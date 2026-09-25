"""Runtime handling of disposable databases: lazy opening, failure policy, discard.

``open_database`` already rebuilds a disposable database whose projection
version, identity or file is bad when it opens. Damage an operation meets later
reaches the owner, which classifies the failure with
:func:`projection_failure` and, for ``rebuild``, calls
:meth:`DisposableDatabase.discard`: the handle closes, the files go, and the
next :meth:`DisposableDatabase.get` creates the projection empty.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path
from typing import Literal

from core.database._connections import classify_write_error, is_busy_error
from core.database.database import Database, _discard, open_database
from core.database.errors import (
    DatabaseCorruptError,
    DatabaseFormatError,
    DatabaseUnavailableError,
)
from core.database.spec import DISPOSABLE, DatabaseSpec

ProjectionFailure = Literal["busy", "unavailable", "rebuild"]


def projection_failure(error: BaseException) -> ProjectionFailure | None:
    """Classify a failed operation on a disposable database.

    - ``busy``: another connection holds the file (busy or locked); retry later.
    - ``unavailable``: the file cannot be used right now (permission, full
      disk, I/O failure, a closed handle, or a copy still open elsewhere in
      this process).
    - ``rebuild``: the projection cannot be trusted: classified corruption,
      identity, format or schema failures, and every other SQLite failure of
      the owner's own statements, such as a missing table or a violated
      constraint.
    - ``None``: not a database failure (API misuse or a non-SQLite exception);
      the caller propagates it.

    Neither ``busy`` nor ``unavailable`` ever grounds a discard.
    """
    if isinstance(error, DatabaseUnavailableError):
        return "busy" if _caused_by_contention(error) else "unavailable"
    if isinstance(error, DatabaseCorruptError | DatabaseFormatError):
        return "rebuild"
    if not isinstance(error, sqlite3.DatabaseError) or isinstance(error, sqlite3.ProgrammingError):
        return None
    if is_busy_error(error):
        return "busy"
    if classify_write_error(error) == "unavailable":
        return "unavailable"
    return "rebuild"


def _caused_by_contention(error: BaseException) -> bool:
    cause = error.__cause__
    while cause is not None:
        if isinstance(cause, sqlite3.Error) and is_busy_error(cause):
            return True
        cause = cause.__cause__
    return False


class DisposableDatabase:
    """One owner's lazily opened disposable database.

    ``get`` opens the projection on first use and returns the open handle
    afterwards; ``discard`` closes it and deletes the files so the next ``get``
    rebuilds it; ``close`` releases it for good. Operations that already hold
    the old handle when ``discard`` runs fail as unavailable.
    """

    def __init__(self, spec: DatabaseSpec) -> None:
        if spec.profile != DISPOSABLE:
            raise ValueError(f"{spec.name}: only a disposable database can be discarded")
        self.spec = spec
        self._lock = threading.Lock()
        self._database: Database | None = None
        self._closed = False

    @property
    def path(self) -> Path:
        return self.spec.path

    def get(self) -> Database:
        """Return the open database, opening (and if needed rebuilding) it first."""
        with self._lock:
            if self._closed:
                raise DatabaseUnavailableError(f"{self.spec.name} is closed")
            if self._database is None or self._database.is_closed():
                self._database = open_database(self.spec)
            return self._database

    async def get_async(self) -> Database:
        """``get`` for the Event Loop: the first open runs off the loop."""
        database = self._database
        if database is not None and not database.is_closed() and not self._closed:
            return database
        return await asyncio.to_thread(self.get)

    def discard(self) -> None:
        """Close the handle and delete the files; the next ``get`` rebuilds them.

        Raises :class:`DatabaseUnavailableError` while another handle in this
        process still has the file open or the files cannot be removed.
        """
        with self._lock:
            self._release()
            _discard(self.spec)

    def close(self) -> None:
        """Close the handle; later ``get`` calls fail as unavailable."""
        with self._lock:
            self._closed = True
            self._release()

    def _release(self) -> None:
        database, self._database = self._database, None
        if database is not None:
            database.close()
