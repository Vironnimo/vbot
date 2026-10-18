"""Verification of snapshot database copies and their recorded evidence."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from core.database._connections import (
    classify_unavailable,
    classify_write_error,
    readonly_sqlite_uri,
)
from core.database._snapshot_manifest import (
    SnapshotMember,
    member_path,
)
from core.database.errors import (
    DatabaseCorruptError,
    DatabaseSchemaMismatchError,
    DatabaseUnavailableError,
)
from core.database.marker import (
    valid_database_id,
)
from core.database.spec import (
    DatabaseSpec,
)


@dataclass(frozen=True)
class _MemberVerification:
    database_id: str
    application_id: int
    format_generation: int
    migrations: tuple[str, ...]
    facts: Mapping[str, int]


class _SnapshotCancelledError(Exception):
    """Internal cooperative stop signal for an unpublished snapshot."""


UNUSABLE_COPY_ERRORS = (DatabaseCorruptError, DatabaseSchemaMismatchError)


def _sha256(path: Path, *, cancelled: Callable[[], bool] | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            if cancelled is not None and cancelled():
                raise _SnapshotCancelledError
            digest.update(chunk)
    return digest.hexdigest()


def verify_database_file(
    path: Path,
    *,
    name: str,
    spec: DatabaseSpec | None = None,
    expected_database_id: str | None = None,
    fact_names: Iterable[str] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> _MemberVerification:
    """Verify one standalone database copy and read the facts a manifest records.

    Checks the kernel identity against ``name`` (and ``expected_database_id``),
    ``quick_check`` and ``foreign_key_check``. With a ``spec``, also its
    application id and the owner facts it declares, limited to ``fact_names``
    when given: a member recorded by an older vBot is checked only against the
    facts it recorded. Damage and identity failures raise
    ``DatabaseCorruptError``; a fact query this copy's schema cannot answer
    raises ``DatabaseSchemaMismatchError``.
    """
    selected_facts = None if fact_names is None else frozenset(fact_names)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(readonly_sqlite_uri(path), uri=True)
        if cancelled is not None:
            connection.set_progress_handler(lambda: int(cancelled()), 10_000)
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        generation = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if spec is not None and application_id != spec.application_id:
            raise DatabaseCorruptError(f"{name} copy has a foreign application_id")
        identity = {
            str(key): str(value)
            for key, value in connection.execute("SELECT key, value FROM kernel_meta")
        }
        database_id = identity.get("database_id")
        if (
            identity.get("database_name") != name
            or identity.get("format_generation") != str(generation)
            or not valid_database_id(database_id)
        ):
            raise DatabaseCorruptError(f"{name} copy has an invalid kernel identity")
        if expected_database_id is not None and database_id != expected_database_id:
            raise DatabaseCorruptError(f"{name} copy has another database identity")
        integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
        if integrity != "ok":
            raise DatabaseCorruptError(f"{name} copy integrity failed: {integrity}")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise DatabaseCorruptError(f"{name} copy foreign_key_check failed")
        migrations = tuple(
            sorted(str(row[0]) for row in connection.execute("SELECT name FROM kernel_migrations"))
        )
        facts: dict[str, int] = {}
        if spec is not None and spec.snapshot_facts is not None:
            for fact, query in spec.snapshot_facts.queries.items():
                if selected_facts is not None and fact not in selected_facts:
                    continue
                facts[fact] = _fact_value(connection, name, fact, query)
        return _MemberVerification(
            database_id=str(database_id),
            application_id=application_id,
            format_generation=generation,
            migrations=migrations,
            facts=facts,
        )
    except sqlite3.Error as exc:
        if cancelled is not None and cancelled():
            raise _SnapshotCancelledError from exc
        if classify_unavailable(exc) or classify_write_error(exc) == "unavailable":
            raise DatabaseUnavailableError(f"{name} copy is unavailable") from exc
        raise DatabaseCorruptError(f"{name} copy verification failed") from exc
    finally:
        if connection is not None:
            with suppress(BaseException):
                connection.close()


def _fact_value(connection: sqlite3.Connection, name: str, fact: str, query: str) -> int:
    try:
        row = connection.execute(query).fetchone()
    except sqlite3.DatabaseError as exc:
        if classify_unavailable(exc) or classify_write_error(exc) != "other":
            raise
        raise DatabaseSchemaMismatchError(
            name, f"snapshot fact {fact}", f"cannot be computed ({exc})"
        ) from exc
    return int(row[0]) if row is not None and row[0] is not None else 0


def verify_member(
    snapshot_dir: Path,
    member: SnapshotMember,
    *,
    spec: DatabaseSpec | None = None,
    path: Path | None = None,
) -> None:
    """Verify a member copy and every manifest claim derived from its contents.

    ``path`` verifies another file against the member's claims, such as a
    staged restore copy.
    """
    target = path if path is not None else member_path(snapshot_dir, member)
    if target is None:
        raise DatabaseCorruptError(f"snapshot member {member.name} is missing")
    try:
        if target.stat().st_size != member.file_size:
            raise DatabaseCorruptError(f"snapshot member {member.name} size mismatch")
        if _sha256(target) != member.sha256:
            raise DatabaseCorruptError(f"snapshot member {member.name} hash mismatch")
        verification = verify_database_file(
            target,
            name=member.name,
            spec=spec,
            expected_database_id=member.database_id,
            fact_names=member.facts,
        )
    except FileNotFoundError as exc:
        raise DatabaseCorruptError(f"snapshot member {member.name} is missing") from exc
    except OSError as exc:
        raise DatabaseUnavailableError(f"snapshot member {member.name} is unavailable") from exc
    if (
        verification.application_id != member.application_id
        or verification.format_generation != member.format_generation
        or verification.migrations != member.migrations
    ):
        raise DatabaseCorruptError(f"snapshot member {member.name} disagrees with its manifest")
    for fact, value in verification.facts.items():
        if member.facts[fact] != value:
            raise DatabaseCorruptError(f"snapshot member {member.name} fact {fact} mismatch")
