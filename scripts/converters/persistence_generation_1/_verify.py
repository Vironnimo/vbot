"""Offline verification of a staged Generation 1 data directory before it is installed."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.config_validation import JsonValidationReport
from core.database import (
    DatabaseError,
    DatabaseSpec,
    canonical_database_path,
    open_offline_database,
)
from core.database.marker import read_kernel_identity
from core.database.snapshots import verify_database_file
from core.json_documents import FORMAT_VERSION_FIELD
from core.runtime.databases import canonical_database_specs
from core.settings import validate_data_dir_config
from scripts.converters.persistence_generation_1 import swarm
from scripts.converters.persistence_generation_1._context import ConversionError

_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_FORMAT_VERSION_PATH = f"$.{FORMAT_VERSION_FIELD}"
# How many document diagnostics the report lists.
_REPORTED_DIAGNOSTICS = 50


@dataclass(frozen=True)
class VerifiedDatabase:
    """One staged canonical database and the identity it is registered with."""

    relative: str
    name: str
    database_id: str
    format_generation: int
    size: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.relative,
            "database_id": self.database_id,
            "format_generation": self.format_generation,
            "size_bytes": self.size,
        }


def database_specs(root: Path) -> dict[str, DatabaseSpec]:
    """Every canonical database spec this converter knows, placed in data directory ``root``."""
    specs = {spec.name: spec for spec in canonical_database_specs(root)}
    swarm_spec = swarm.database_spec(root)
    specs[swarm_spec.name] = swarm_spec
    return specs


def verify_databases(root: Path) -> list[VerifiedDatabase]:
    """Open and verify every database file below ``root``; leave no journal files.

    Each file must carry a kernel identity whose name places it at its own
    path. A known database is opened offline with its spec (reconcile,
    migrations and format generation) and then checked for integrity, foreign
    keys, identity and application id. The journal files are checkpointed away,
    so only complete database files are installed.
    """
    specs = database_specs(root)
    verified: list[VerifiedDatabase] = []
    for path in sorted(candidate for candidate in root.rglob("*.db") if candidate.is_file()):
        relative = path.relative_to(root).as_posix()
        try:
            identity = read_kernel_identity(path)
            name = identity["database_name"]
            if canonical_database_path(root, name).resolve() != path.resolve():
                raise ConversionError(f"{relative} records database {name}, which lives elsewhere")
            spec = specs.get(name)
            if spec is not None:
                open_offline_database(spec).close()
            result = verify_database_file(path, name=name, spec=spec)
        except DatabaseError as error:
            raise ConversionError(
                f"staged database {relative} failed verification: {error}"
            ) from error
        if spec is not None and result.format_generation != spec.format_generation:
            raise ConversionError(
                f"staged database {relative} has format generation {result.format_generation}, "
                f"not {spec.format_generation}"
            )
        settle_journal(path)
        verified.append(
            VerifiedDatabase(
                relative, name, result.database_id, result.format_generation, path.stat().st_size
            )
        )
    return verified


def settle_journal(path: Path) -> None:
    """Checkpoint a closed database's write-ahead log and remove its journal files."""
    if Path(f"{path}-wal").exists():
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    for suffix in ("-wal", "-shm"):
        journal = Path(f"{path}{suffix}")
        if journal.exists() and (suffix == "-shm" or journal.stat().st_size == 0):
            journal.unlink()
    remaining = [suffix for suffix in _SIDECAR_SUFFIXES if Path(f"{path}{suffix}").exists()]
    if remaining:
        raise ConversionError(f"{path} keeps its {', '.join(remaining)} file after closing")


def verify_json_documents(source: Path, root: Path) -> dict[str, Any]:
    """Validate the durable JSON documents the installed data directory will have.

    Staged documents and the source documents that were already current are
    validated by their owners, as ``vbot doctor config`` does. A staged
    document without a valid ``format_version`` is a conversion failure; other
    errors describe content the conversion carried over unchanged, so they are
    reported for the owner to repair.
    """
    staged = [report for report in validate_data_dir_config(root) if report.exists]
    staged_paths = {report.file_path.resolve() for report in staged}
    current = [
        report
        for report in validate_data_dir_config(source)
        if report.exists
        and (root / report.file_path.relative_to(source)).resolve() not in staged_paths
        and _has_format_version(report.file_path)
    ]
    for report in staged:
        for diagnostic in report.diagnostics:
            if diagnostic.severity == "error" and diagnostic.path == _FORMAT_VERSION_PATH:
                relative = report.file_path.relative_to(root).as_posix()
                raise ConversionError(f"staged {relative}: format_version {diagnostic.message}")
    errors = [
        {"file": _relative(report, source, root), "path": item.path, "message": item.message}
        for report in (*staged, *current)
        for item in report.diagnostics
        if item.severity == "error"
    ]
    return {
        "documents_validated": len(staged) + len(current),
        "documents_with_errors": sum(1 for report in (*staged, *current) if not report.ok),
        "warnings": sum(report.warning_count for report in (*staged, *current)),
        "errors": errors[:_REPORTED_DIAGNOSTICS],
    }


def _has_format_version(path: Path) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return False
    return isinstance(value, dict) and FORMAT_VERSION_FIELD in value


def _relative(report: JsonValidationReport, source: Path, root: Path) -> str:
    base = root if report.file_path.is_relative_to(root) else source
    return report.file_path.relative_to(base).as_posix()
