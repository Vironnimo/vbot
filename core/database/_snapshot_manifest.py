"""Snapshot records and the persisted manifest format, paths and parsing."""

from __future__ import annotations

import json
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from core.database._documents import (
    DocumentMember,
    documents_payload,
    parse_documents,
)
from core.database._files import fsync_file
from core.database.errors import (
    DatabaseCorruptError,
    DatabaseUnavailableError,
)
from core.database.marker import (
    valid_database_id,
)
from core.database.spec import (
    validate_database_name,
)
from core.utils.timestamps import (
    parse_canonical_timestamp,
)

SNAPSHOT_ROOT_NAME = "snapshots"

SNAPSHOT_MANIFEST_NAME = "manifest.json"

MANIFEST_VERSION = 1

_SNAPSHOT_ID_PATTERN = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")

_HEX64_PATTERN = re.compile(r"^[0-9a-f]{64}$")

_MANIFEST_KEYS = frozenset(
    {
        "manifest_version",
        "snapshot_id",
        "reason",
        "created_at",
        "vbot_version",
        "sqlite_version",
        "sqlite_source_id",
        "members",
        "documents",
        "complete",
    }
)

_MEMBER_KEYS = frozenset(
    {
        "file",
        "database_id",
        "application_id",
        "format_generation",
        "file_size",
        "sha256",
        "integrity",
        "foreign_key_check",
        "migrations",
        "facts",
    }
)


@dataclass(frozen=True)
class SnapshotMember:
    """One verified database copy inside a data snapshot."""

    name: str
    file: str
    database_id: str
    application_id: int
    format_generation: int
    file_size: int
    sha256: str
    migrations: tuple[str, ...]
    facts: Mapping[str, int]


@dataclass(frozen=True)
class SnapshotManifest:
    """Validated metadata describing one complete data snapshot."""

    snapshot_id: str
    reason: str
    created_at: str
    vbot_version: str
    sqlite_version: str
    sqlite_source_id: str
    members: Mapping[str, SnapshotMember]
    #: The JSON document set by relative path, limited to the documents this
    #: vBot knows.
    documents: Mapping[str, DocumentMember]

    @property
    def total_size(self) -> int:
        return sum(member.file_size for member in self.members.values()) + sum(
            document.file_size for document in self.documents.values()
        )

    def created_instant(self) -> datetime:
        return parse_canonical_timestamp(self.created_at)


def snapshot_root(data_dir: Path) -> Path:
    return Path(data_dir) / SNAPSHOT_ROOT_NAME


def member_file_name(name: str) -> str:
    validate_database_name(name)
    return f"{name}.db"


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _parse_member(name: str, payload: object) -> SnapshotMember:
    try:
        validate_database_name(name)
    except ValueError as exc:
        raise DatabaseCorruptError("snapshot manifest lists an invalid database name") from exc
    if not isinstance(payload, dict) or not set(payload) >= _MEMBER_KEYS:
        raise DatabaseCorruptError(f"snapshot manifest member {name} has an unexpected shape")
    for field in ("application_id", "format_generation", "file_size"):
        if not _is_count(payload[field]):
            raise DatabaseCorruptError(f"snapshot manifest member {name} has an invalid {field}")
    if payload["file"] != member_file_name(name):
        raise DatabaseCorruptError(f"snapshot manifest member {name} names an unexpected file")
    if not valid_database_id(payload["database_id"]):
        raise DatabaseCorruptError(f"snapshot manifest member {name} has an invalid database_id")
    sha256 = payload["sha256"]
    if not isinstance(sha256, str) or _HEX64_PATTERN.fullmatch(sha256) is None:
        raise DatabaseCorruptError(f"snapshot manifest member {name} has an invalid sha256")
    if payload["integrity"] != "ok" or payload["foreign_key_check"] != "ok":
        raise DatabaseCorruptError(f"snapshot manifest member {name} is not verified")
    migrations = payload["migrations"]
    if not isinstance(migrations, list) or not all(
        isinstance(item, str) and item for item in migrations
    ):
        raise DatabaseCorruptError(f"snapshot manifest member {name} has invalid migrations")
    facts = payload["facts"]
    if not isinstance(facts, dict) or not all(
        isinstance(key, str) and key and _is_count(value) for key, value in facts.items()
    ):
        raise DatabaseCorruptError(f"snapshot manifest member {name} has invalid facts")
    return SnapshotMember(
        name=name,
        file=str(payload["file"]),
        database_id=str(payload["database_id"]),
        application_id=int(payload["application_id"]),
        format_generation=int(payload["format_generation"]),
        file_size=int(payload["file_size"]),
        sha256=sha256,
        migrations=tuple(sorted(migrations)),
        facts={str(key): int(value) for key, value in facts.items()},
    )


def _parse_manifest(payload: object, *, child_name: str) -> SnapshotManifest:
    if not isinstance(payload, dict) or not set(payload) >= _MANIFEST_KEYS:
        raise DatabaseCorruptError("snapshot manifest has an unexpected shape")
    if payload["manifest_version"] != MANIFEST_VERSION or isinstance(
        payload["manifest_version"], bool
    ):
        raise DatabaseCorruptError("snapshot manifest version is unsupported")
    snapshot_id = payload["snapshot_id"]
    if (
        not isinstance(snapshot_id, str)
        or snapshot_id != child_name
        or _SNAPSHOT_ID_PATTERN.fullmatch(snapshot_id) is None
    ):
        raise DatabaseCorruptError("snapshot manifest has an invalid snapshot_id")
    for field in ("reason", "vbot_version", "sqlite_version", "sqlite_source_id"):
        value = payload[field]
        if not isinstance(value, str) or not value.strip():
            raise DatabaseCorruptError(f"snapshot manifest has an invalid {field}")
    created_at = payload["created_at"]
    try:
        if not isinstance(created_at, str):
            raise ValueError("created_at is not text")
        # A manifest is written with a canonical timestamp; any other is damage.
        parse_canonical_timestamp(created_at)
    except ValueError as exc:
        raise DatabaseCorruptError("snapshot manifest has an invalid created_at") from exc
    if payload["complete"] is not True:
        raise DatabaseCorruptError("snapshot manifest is incomplete")
    members = payload["members"]
    if not isinstance(members, dict) or not members:
        raise DatabaseCorruptError("snapshot manifest has no members")
    return SnapshotManifest(
        snapshot_id=snapshot_id,
        reason=str(payload["reason"]),
        created_at=created_at,
        vbot_version=str(payload["vbot_version"]),
        sqlite_version=str(payload["sqlite_version"]),
        sqlite_source_id=str(payload["sqlite_source_id"]),
        members={name: _parse_member(name, member) for name, member in members.items()},
        documents=parse_documents(payload["documents"]),
    )


def _manifest_payload(manifest: SnapshotManifest) -> dict[str, Any]:
    return {
        "manifest_version": MANIFEST_VERSION,
        "snapshot_id": manifest.snapshot_id,
        "reason": manifest.reason,
        "created_at": manifest.created_at,
        "vbot_version": manifest.vbot_version,
        "sqlite_version": manifest.sqlite_version,
        "sqlite_source_id": manifest.sqlite_source_id,
        "complete": True,
        "members": {
            name: {
                "file": member.file,
                "database_id": member.database_id,
                "application_id": member.application_id,
                "format_generation": member.format_generation,
                "file_size": member.file_size,
                "sha256": member.sha256,
                "integrity": "ok",
                "foreign_key_check": "ok",
                "migrations": list(member.migrations),
                "facts": dict(sorted(member.facts.items())),
            }
            for name, member in sorted(manifest.members.items())
        },
        "documents": documents_payload(manifest.documents),
    }


def _safe_snapshot_dir(data_dir: Path, snapshot_dir: Path) -> Path | None:
    root = snapshot_root(data_dir)
    candidate = Path(snapshot_dir)
    try:
        root_resolved = root.resolve()
        candidate_resolved = candidate.resolve()
    except OSError as exc:
        raise DatabaseUnavailableError("snapshot paths are unavailable") from exc
    if candidate.is_symlink() or candidate_resolved.parent != root_resolved:
        return None
    if candidate.name.startswith("."):
        return None
    return candidate


def member_path(snapshot_dir: Path, member: SnapshotMember) -> Path | None:
    """The member's file inside its snapshot, or ``None`` when it is unsafe or absent."""
    path = Path(snapshot_dir) / member.file
    try:
        if path.is_symlink() or path.resolve().parent != Path(snapshot_dir).resolve():
            return None
        if not stat.S_ISREG(path.stat().st_mode):
            return None
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DatabaseUnavailableError(f"snapshot member {member.name} is unavailable") from exc
    return path


def read_manifest(data_dir: Path, snapshot_dir: Path) -> SnapshotManifest | None:
    """Parse one published manifest; ``None`` for anything malformed, unsafe or newer.

    Operational read failures raise ``DatabaseUnavailableError``, so a transient
    problem is never mistaken for a bad snapshot.
    """
    candidate = _safe_snapshot_dir(data_dir, snapshot_dir)
    if candidate is None:
        return None
    manifest_path = candidate / SNAPSHOT_MANIFEST_NAME
    try:
        if manifest_path.is_symlink() or not stat.S_ISREG(manifest_path.stat().st_mode):
            return None
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        return _parse_manifest(payload, child_name=candidate.name)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DatabaseUnavailableError("snapshot manifest is unavailable") from exc
    except UnicodeError, json.JSONDecodeError, DatabaseCorruptError:
        return None


def _write_manifest(path: Path, manifest: SnapshotManifest) -> None:
    path.write_text(
        json.dumps(_manifest_payload(manifest), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    fsync_file(path)
