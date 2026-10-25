"""Named retention claims within the snapshot owner's operation lock.

An updater keeps the snapshot needed by its retained application version.
Ordinary count, byte and free-space cleanup cannot remove such a snapshot.
An unreadable claim record refuses cleanup rather than guessing which data
is still needed. Owners release their own claims after publishing a successor.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.database._snapshot_manifest import _SNAPSHOT_ID_PATTERN, snapshot_root
from core.database.errors import DatabaseUnavailableError
from core.database.marker import operation_lock
from core.utils.atomic import atomic_write_text
from core.utils.ids import is_safe_id


def _read(data_dir: Path) -> dict[str, Any]:
    path = snapshot_root(data_dir) / "retention.json"
    try:
        if path.stat().st_size > 64 * 1024:
            raise ValueError("record too large")
        value = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict)
            or value.get("format_version") != 1
            or not isinstance(value.get("owners"), dict)
            or any(
                not is_safe_id(owner)
                or not isinstance(snapshot, str)
                or _SNAPSHOT_ID_PATTERN.fullmatch(snapshot) is None
                for owner, snapshot in value["owners"].items()
            )
        ):
            raise ValueError("invalid retention claims")
        return value
    except FileNotFoundError:
        return {"format_version": 1, "owners": {}}
    except (OSError, UnicodeError, ValueError) as exc:
        raise DatabaseUnavailableError("snapshot retention claims cannot be read") from exc


def retained_snapshot_ids(data_dir: Path) -> set[str]:
    """Read retention claims; the caller holds the data-store operation lock."""
    return set(_read(data_dir)["owners"].values())


def retain_snapshot_locked(data_dir: Path, owner: str, snapshot_id: str) -> None:
    """Claim a verified, published snapshot before capture's lock is released."""
    if not is_safe_id(owner) or _SNAPSHOT_ID_PATTERN.fullmatch(snapshot_id) is None:
        raise ValueError("invalid snapshot retention owner or snapshot id")
    value = _read(data_dir)
    value["owners"][owner] = snapshot_id
    _write(data_dir, value)


def release_snapshot_retention(data_dir: Path, owner: str) -> None:
    """Release one owner's claim, leaving other owners and the snapshot intact."""
    if not is_safe_id(owner):
        raise ValueError("invalid snapshot retention owner")
    with operation_lock(data_dir):
        value = _read(data_dir)
        if owner in value["owners"]:
            del value["owners"][owner]
            _write(data_dir, value)


def _write(data_dir: Path, value: dict[str, Any]) -> None:
    atomic_write_text(
        snapshot_root(data_dir) / "retention.json",
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        mode=0o600,
    )
