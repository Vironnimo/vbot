"""Hashes, durable flushes and scalar values for offline Session conversion."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

from core.sessions import SessionAddress


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDWR)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _address_payload(address: SessionAddress) -> list[str | None]:
    return [address.project_id, address.agent_id, address.session_id]
