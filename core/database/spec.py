"""Declarations an owner hands the kernel to open one database."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from core.database.errors import DatabaseFormatError

DatabaseProfile = Literal["canonical", "disposable"]
CANONICAL: DatabaseProfile = "canonical"
DISPOSABLE: DatabaseProfile = "disposable"

#: The SQLite ``application_id`` of every vBot database family: "VB" plus two
#: letters. Every Extension database shares one id; ``kernel_meta`` names it.
APPLICATION_IDS: Mapping[str, int] = MappingProxyType(
    {
        "sessions": 0x56425353,  # VBSS
        "decisions": 0x56424443,  # VBDC
        "channels": 0x56424348,  # VBCH
        "provider_usage": 0x56425055,  # VBPU
        "model_usage": 0x56424D55,  # VBMU
        "extensions": 0x56424558,  # VBEX
        "statistics": 0x56425354,  # VBST
        "recall_index": 0x56425249,  # VBRI
        "recall_vectors": 0x56425256,  # VBRV
    }
)

_PLAIN_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_EXTENSION_NAME = re.compile(r"^ext\.([a-z0-9][a-z0-9_-]*)\.([a-z0-9][a-z0-9_-]*)$")
_MIGRATION_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_SQLITE_APPLICATION_ID_MAX = 0x7FFFFFFF


@dataclass(frozen=True)
class Migration:
    """One named, append-only data step recorded in ``kernel_migrations``.

    ``apply`` runs inside the open transaction after the additive reconcile. It
    must be idempotent and may only fill new structures or NULLs. A fresh
    database records every declared migration without running it. Set
    ``breaks_older`` when older vBot versions would misread the result: they
    refuse to open the database instead of guessing.
    """

    name: str
    breaks_older: bool = False
    apply: Callable[[sqlite3.Connection], None] | None = None


@dataclass(frozen=True)
class SnapshotFacts:
    """Owner facts a data snapshot records per member and re-verifies on its copy.

    Each query returns one integer from the snapshot copy, such as an entry
    count or a revision watermark. The manifest stores the values; verification
    recomputes every recorded fact the verifier also declares.
    """

    queries: Mapping[str, str]


@dataclass(frozen=True)
class DatabaseHealth:
    """Owner-reported health of an open database, shown in data-store status."""

    state: Literal["healthy", "degraded"]
    reason: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DatabaseSpec:
    """Everything the kernel needs to open, evolve, verify and snapshot one database.

    ``schema_sql`` is the declarative DDL; after the Generation 1 cut it only
    grows additively. ``retired_indexes`` names indexes this version no longer
    declares; the kernel drops them on open. ``after_open`` runs after the
    reconcile and migrations and manages its own transactions (for example FTS
    readiness). ``health`` reports owner state for data-store status from a
    read transaction. ``connection_setup`` runs on every connection the kernel
    opens for the database, the writer and each pooled reader, before any other
    statement, for example to load a SQLite extension its tables or queries need;
    it must not write.

    Canonical databases live at :func:`canonical_database_path` for their name
    and are registered in the data directory's marker. Disposable databases
    declare ``projection_version`` and are discarded and rebuilt on mismatch.
    """

    name: str
    path: Path
    profile: DatabaseProfile
    application_id: int
    format_generation: int
    schema_sql: str
    migrations: tuple[Migration, ...] = ()
    retired_indexes: tuple[str, ...] = ()
    after_open: Callable[[sqlite3.Connection], None] | None = None
    snapshot_facts: SnapshotFacts | None = None
    health: Callable[[sqlite3.Connection], DatabaseHealth] | None = None
    projection_version: int | None = None
    connection_setup: Callable[[sqlite3.Connection], None] | None = None

    def __post_init__(self) -> None:
        validate_database_name(self.name)
        object.__setattr__(self, "path", Path(self.path))
        if self.profile not in (CANONICAL, DISPOSABLE):
            raise ValueError(f"unknown database profile: {self.profile!r}")
        if not 0 < self.application_id <= _SQLITE_APPLICATION_ID_MAX:
            raise ValueError(f"{self.name}: application_id must be a positive 32-bit value")
        if self.format_generation < 1:
            raise ValueError(f"{self.name}: format_generation must be at least 1")
        if self.profile == DISPOSABLE:
            if self.projection_version is None or self.projection_version < 1:
                raise ValueError(f"{self.name}: a disposable database needs projection_version")
            if self.snapshot_facts is not None:
                raise ValueError(f"{self.name}: disposable databases are never snapshotted")
        elif self.projection_version is not None:
            raise ValueError(f"{self.name}: projection_version is for disposable databases")
        names = [migration.name for migration in self.migrations]
        if len(set(names)) != len(names):
            raise ValueError(f"{self.name}: migration names must be unique")
        for migration_name in names:
            if _MIGRATION_NAME.fullmatch(migration_name) is None:
                raise ValueError(f"{self.name}: invalid migration name {migration_name!r}")


def validate_database_name(name: str) -> None:
    """Accept ``lower_snake`` owner names and ``ext.<owner>.<name>`` Extension names."""
    if not isinstance(name, str) or (
        _PLAIN_NAME.fullmatch(name) is None and _EXTENSION_NAME.fullmatch(name) is None
    ):
        raise ValueError(f"invalid database name: {name!r}")


def is_extension_database_name(name: str) -> bool:
    """Whether ``name`` names an Extension database, ``ext.<extension>.<name>``."""
    return isinstance(name, str) and _EXTENSION_NAME.fullmatch(name) is not None


def canonical_relative_path(name: str) -> Path:
    """The fixed location of a canonical database inside its data directory.

    ``sessions`` -> ``sessions.db``, ``provider_usage`` -> ``provider-usage.db``,
    ``ext.<owner>.<name>`` -> ``extension-data/<owner>/<name>.db``. The marker
    lists databases by name only, so offline tools find every member here.
    """
    validate_database_name(name)
    extension = _EXTENSION_NAME.fullmatch(name)
    if extension is not None:
        return Path("extension-data") / extension.group(1) / f"{extension.group(2)}.db"
    return Path(f"{name.replace('_', '-')}.db")


def canonical_database_path(data_dir: Path, name: str) -> Path:
    """The absolute location of canonical database ``name`` in ``data_dir``."""
    return Path(data_dir) / canonical_relative_path(name)


def canonical_data_dir(spec: DatabaseSpec) -> Path:
    """Derive the data directory from a canonical spec's fixed path.

    Resolves both sides, so a symlinked spelling that lands elsewhere can never
    pair a database with another directory's marker.
    """
    relative = canonical_relative_path(spec.name)
    try:
        resolved = spec.path.resolve()
    except OSError as exc:
        raise DatabaseFormatError(
            f"{spec.name}: database path is unavailable: {spec.path}"
        ) from exc
    depth = len(relative.parts)
    data_dir = resolved.parents[depth - 1]
    if data_dir / relative != resolved:
        raise DatabaseFormatError(
            f"{spec.name}: {resolved} is not the canonical path {relative.as_posix()} "
            "inside a data directory"
        )
    return data_dir
