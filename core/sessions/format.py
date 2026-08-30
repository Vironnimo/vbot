"""Current-format Session store marker.

``sessions.db`` may be created or opened only when the data directory's
current-format marker authorizes it. Data-directory initialization writes a
``bootstrap`` marker when it creates a genuinely new data-directory root;
the Session store publishes ``ready`` only after a verified database with the
marker's identity exists. A data directory without a marker is not a
current-format store and Runtime refuses it without inspecting anything else
in the directory — in particular it never searches for legacy Session
artifacts to guess why the marker is absent.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from core.sessions.errors import SessionStorageFormatError
from core.sessions.schema import SCHEMA_VERSION

MARKER_FILE_NAME = "session-store.json"
MARKER_FORMAT_VERSION = 1
MARKER_STATE_BOOTSTRAP = "bootstrap"
MARKER_STATE_READY = "ready"
_MARKER_STATES = (MARKER_STATE_BOOTSTRAP, MARKER_STATE_READY)
_DATABASE_ID_LENGTH = 32
_MARKER_KEYS = frozenset({"format_version", "state", "database_id", "schema_version"})


def session_store_marker_path(data_dir: Path) -> Path:
    """The fixed marker path for one data directory."""
    return Path(data_dir) / MARKER_FILE_NAME


def new_database_id() -> str:
    """A fresh database identity shared by the marker and ``store_meta``."""
    return uuid.uuid4().hex


def read_session_store_marker(data_dir: Path) -> dict[str, Any] | None:
    """Load and strictly validate the marker; ``None`` when it does not exist."""
    path = session_store_marker_path(data_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise SessionStorageFormatError(f"Session store marker cannot be read: {path}") from exc
    return _validate_marker(raw, path)


def write_bootstrap_marker(data_dir: Path) -> dict[str, Any]:
    """Write the bootstrap marker authorizing creation of a fresh Session database."""
    payload: dict[str, Any] = {
        "format_version": MARKER_FORMAT_VERSION,
        "state": MARKER_STATE_BOOTSTRAP,
        "database_id": new_database_id(),
        "schema_version": SCHEMA_VERSION,
    }
    _write_marker(session_store_marker_path(data_dir), payload)
    return payload


def publish_ready_marker(data_dir: Path, database_id: str) -> dict[str, Any]:
    """Atomically publish the ready marker once a verified database exists."""
    _validate_database_id(database_id)
    payload: dict[str, Any] = {
        "format_version": MARKER_FORMAT_VERSION,
        "state": MARKER_STATE_READY,
        "database_id": database_id,
        "schema_version": SCHEMA_VERSION,
    }
    _write_marker(session_store_marker_path(data_dir), payload)
    return payload


def validate_session_store_paths(data_dir: Path, database_path: Path) -> Path:
    """Require the canonical database to sit directly in the data directory.

    The marker is always resolved beside the database, so a database path
    outside the data directory (or reached through a symlink spelling that
    resolves elsewhere) can never pair with the wrong marker.
    """
    resolved_data_dir = Path(data_dir).resolve()
    resolved_database = Path(database_path).resolve()
    if resolved_database.parent != resolved_data_dir:
        raise SessionStorageFormatError(
            f"Session database {resolved_database} is not the canonical path inside "
            f"the data directory {resolved_data_dir}"
        )
    return resolved_data_dir


def _validate_marker(raw: str, path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SessionStorageFormatError(f"Session store marker is malformed: {path}") from exc
    if not isinstance(payload, dict) or set(payload) != set(_MARKER_KEYS):
        raise SessionStorageFormatError(f"Session store marker has an unexpected shape: {path}")
    format_version = payload["format_version"]
    if not isinstance(format_version, int) or isinstance(format_version, bool):
        raise SessionStorageFormatError(f"Session store marker has an invalid format: {path}")
    if format_version > MARKER_FORMAT_VERSION:
        raise SessionStorageFormatError(
            f"Session store marker is from a newer vBot: format version {format_version} "
            f"at {path} exceeds supported {MARKER_FORMAT_VERSION}"
        )
    if format_version != MARKER_FORMAT_VERSION:
        raise SessionStorageFormatError(
            f"Session store marker has an unsupported older format version "
            f"{format_version} at {path}"
        )
    if payload["state"] not in _MARKER_STATES:
        raise SessionStorageFormatError(f"Session store marker has an invalid state: {path}")
    _validate_database_id(payload["database_id"], path)
    schema_version = payload["schema_version"]
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version < 1
    ):
        raise SessionStorageFormatError(
            f"Session store marker has an invalid schema version: {path}"
        )
    return payload


def _validate_database_id(value: object, path: Path | None = None) -> None:
    valid = (
        isinstance(value, str)
        and len(value) == _DATABASE_ID_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )
    if not valid:
        suffix = f" at {path}" if path is not None else ""
        raise SessionStorageFormatError(f"Session store marker has an invalid database id{suffix}")


def _write_marker(path: Path, payload: dict[str, Any]) -> None:
    """Atomic marker write: durable temp file, atomic replace, persisted entry."""
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise SessionStorageFormatError(f"Session store marker cannot be written: {path}") from exc
    if os.name == "posix":
        descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
