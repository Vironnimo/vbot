"""Installing a verified Generation 1 staging directory, resumably.

Before the first file moves, the complete install is written down as a plan
inside the staging directory. Installing replays the plan; every step checks
whether it already happened, so an interrupted install finishes by running the
conversion again:

1. Every source file the result replaces or retires, including the journal
   files of every rebuilt database, moves to ``pre-generation-1/`` at the same
   relative path. Nothing is deleted.
2. Every staged file moves to its place in the data directory.
3. ``data-store.json`` registers every installed canonical database.
4. Every registered database is opened offline and checked against its
   registration.

All moves are renames within the data directory's volume.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from core.database import (
    DatabaseError,
    canonical_database_path,
    open_offline_database,
    read_marker,
    write_marker_for_databases,
)
from core.database.snapshots import verify_database_file
from core.utils.atomic import atomic_write_text
from scripts.converters.persistence_generation_1._preflight import (
    BACKUP_DIRECTORY,
    STAGING_DIRECTORY,
)
from scripts.converters.persistence_generation_1._verify import database_specs

PLAN_FILE_NAME = "install-plan.json"
FILES_DIRECTORY = "files"
REPORT_FILE_NAME = "conversion-report.json"
_PLAN_FORMAT_VERSION = 1
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


class InstallError(Exception):
    """An install step failed; running the conversion again resumes the install."""


@dataclass(frozen=True)
class InstallPlan:
    """Every move of one install, as data-directory relative POSIX paths."""

    retire: tuple[str, ...]
    install: tuple[str, ...]
    databases: tuple[str, ...]
    report: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": _PLAN_FORMAT_VERSION,
            "retire": list(self.retire),
            "install": list(self.install),
            "databases": list(self.databases),
            "report": self.report,
        }


def staging_path(data_dir: Path) -> Path:
    return data_dir / STAGING_DIRECTORY


def files_root(data_dir: Path) -> Path:
    return staging_path(data_dir) / FILES_DIRECTORY


def plan_path(data_dir: Path) -> Path:
    return staging_path(data_dir) / PLAN_FILE_NAME


def build_plan(
    data_dir: Path,
    retired: list[PurePosixPath],
    databases: list[str],
    report: dict[str, Any],
) -> InstallPlan:
    """Plan the install of the staged files: what moves aside and what moves in.

    A staged file replaces the source file at its path, and the journal files
    of a staged database never meet it, so both move aside with the files the
    conversion retired.
    """
    root = files_root(data_dir)
    install = sorted(
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    )
    retire = {path.as_posix() for path in retired}
    for relative in install:
        candidates = [relative]
        if relative in databases:
            candidates.extend(f"{relative}{suffix}" for suffix in _SIDECAR_SUFFIXES)
        retire.update(name for name in candidates if os.path.lexists(data_dir / name))
    ordered = sorted(retire)
    _require_disjoint(ordered, install)
    return InstallPlan(tuple(ordered), tuple(install), tuple(sorted(databases)), report)


def _require_disjoint(retire: list[str], install: list[str]) -> None:
    """A retired directory must not contain another moved path."""
    for directory in retire:
        prefix = f"{directory}/"
        inside = [path for path in (*retire, *install) if path.startswith(prefix)]
        if inside:
            raise InstallError(f"retiring {directory} would also move {inside[0]}")


def write_plan(data_dir: Path, plan: InstallPlan) -> None:
    atomic_write_text(plan_path(data_dir), json.dumps(plan.to_dict(), indent=2) + "\n")


def read_plan(data_dir: Path) -> InstallPlan | None:
    path = plan_path(data_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("format_version") != _PLAN_FORMAT_VERSION:
            raise ValueError(f"unsupported format_version {payload.get('format_version')!r}")
        return InstallPlan(
            retire=tuple(str(item) for item in payload["retire"]),
            install=tuple(str(item) for item in payload["install"]),
            databases=tuple(str(item) for item in payload["databases"]),
            report=dict(payload["report"]),
        )
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise InstallError(f"the install plan {path} cannot be read: {error}") from error


def changed_sources(data_dir: Path, plan: InstallPlan, started_ns: int) -> list[str]:
    """Retired source files that changed after the conversion started reading them."""
    changed = []
    for relative in plan.retire:
        path = data_dir / relative
        paths = [path, *path.rglob("*")] if path.is_dir() else [path]
        if any(item.stat().st_mtime_ns >= started_ns for item in paths if item.is_file()):
            changed.append(relative)
    return changed


def execute(data_dir: Path, plan: InstallPlan) -> list[str]:
    """Replay every install step; return the registered database names."""
    backup = data_dir / BACKUP_DIRECTORY
    root = files_root(data_dir)
    for relative in plan.retire:
        _retire(data_dir / relative, backup / relative, relative)
    for relative in plan.install:
        _install(root / relative, data_dir / relative, relative)
    try:
        marker = write_marker_for_databases(
            data_dir, [data_dir / relative for relative in plan.databases]
        )
    except (DatabaseError, OSError) as error:
        raise InstallError(f"data-store.json could not be written: {error}") from error
    _check_registered(data_dir, marker.databases)
    atomic_write_text(backup / REPORT_FILE_NAME, json.dumps(plan.report, indent=2) + "\n")
    return sorted(marker.databases)


def _retire(source: Path, target: Path, relative: str) -> None:
    if os.path.lexists(target):
        return
    if not os.path.lexists(source):
        raise InstallError(f"{relative} disappeared from the data directory during the install")
    _move(source, target, relative)


def _install(staged: Path, target: Path, relative: str) -> None:
    if not staged.exists():
        if target.exists():
            return
        raise InstallError(f"the staged {relative} is missing and was never installed")
    if os.path.lexists(target):
        raise InstallError(
            f"{relative} appeared in the data directory during the install; "
            "is a vBot server running?"
        )
    _move(staged, target, relative)


def _move(source: Path, target: Path, relative: str) -> None:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)
    except OSError as error:
        raise InstallError(f"{relative} could not be moved: {error}") from error


def _check_registered(data_dir: Path, registered: Any) -> None:
    """Open every registered database offline and check it against its registration."""
    marker = read_marker(data_dir)
    if marker is None or dict(marker.databases) != dict(registered):
        raise InstallError("data-store.json does not list the installed databases")
    specs = database_specs(data_dir)
    for name, entry in marker.databases.items():
        path = canonical_database_path(data_dir, name)
        spec = specs.get(name)
        try:
            if spec is not None:
                open_offline_database(spec).close()
            verify_database_file(path, name=name, spec=spec, expected_database_id=entry.database_id)
        except (DatabaseError, OSError) as error:
            raise InstallError(
                f"the installed {name} database failed its check: {error}"
            ) from error
