"""Converting one pre-Generation-1 data directory: preflight, run, verify, install.

1. Preflight refuses, changing nothing, while a vBot server uses the data
   directory, when it is already Generation 1, when a source has a shape no
   area reads, or when the volume lacks the space for staging.
2. The conversion holds the maintenance guard ``data-maintenance.json`` from
   the first staged file to the end of the install, so a current vBot refuses
   to open the data directory meanwhile. The areas run in :data:`AREAS` order
   into ``generation-1-staging/files/`` inside the data directory, on the same
   volume.
3. The staged result is verified offline: the Session check compares every
   Session with its source, every staged database is opened with its spec and
   checked (integrity, foreign keys, identity, format generation), and the
   JSON documents are validated by their owners.
4. A dry run stops here: it discards the staging directory and releases the
   guard. Otherwise the install plan is written and executed (see
   ``_install``); the guard is released only after every registered database
   passed its final check.

A failure before the install plan exists discards the staging directory and
leaves the source untouched. A failure during the install keeps the guard, the
plan and the staging directory; running the conversion again finishes it.
"""

from __future__ import annotations

import contextlib
import functools
import os
import shutil
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.database import (
    MARKER_FILE_NAME,
    MaintenanceOperation,
    begin_maintenance,
    finish_maintenance,
    read_maintenance,
)
from core.database.errors import DatabaseError
from core.utils.timestamps import utc_now_timestamp
from scripts.converters.persistence_generation_1 import (
    channels,
    decisions,
    json_documents,
    mcp,
    provider_usage,
    sessions,
    swarm,
)
from scripts.converters.persistence_generation_1._context import (
    ConversionContext,
    ConversionError,
)
from scripts.converters.persistence_generation_1._install import (
    InstallError,
    InstallPlan,
    build_plan,
    changed_sources,
    execute,
    files_root,
    read_plan,
    staging_path,
    write_plan,
)
from scripts.converters.persistence_generation_1._preflight import (
    BACKUP_DIRECTORY,
    OPERATION,
    RefusedError,
    require_data_directory,
    require_free_backup,
    require_no_server,
    require_not_converted,
    require_space,
    require_supported_sources,
    resumable_guard,
    tree_size,
)
from scripts.converters.persistence_generation_1._session_check import check_sessions
from scripts.converters.persistence_generation_1._verify import (
    verify_databases,
    verify_json_documents,
)

REPORT_FORMAT_VERSION = 1


@dataclass(frozen=True)
class Area:
    """One conversion area; ``check_source`` refuses an unreadable source in preflight."""

    name: str
    convert: Callable[[ConversionContext], None]
    check_source: Callable[[Path], None] | None = None


# The declared run order: the quick areas first, so a failure shows early.
AREAS: tuple[Area, ...] = (
    Area(json_documents.AREA, json_documents.convert),
    Area(decisions.AREA, decisions.convert, decisions.check_source),
    Area(swarm.AREA, swarm.convert, swarm.check_source),
    Area(provider_usage.AREA, provider_usage.convert),
    Area(channels.AREA, channels.convert),
    Area(sessions.AREA, sessions.convert, sessions.check_source),
    # Attaches saved MCP results to Tool calls in the staged sessions.db.
    Area(mcp.AREA, mcp.convert),
)


class ConversionFailedError(Exception):
    """The conversion failed before its install; the data directory is unchanged."""


def convert_data_directory(data_dir: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Convert ``data_dir`` to Generation 1, or only verify with ``dry_run``; return the report.

    Raises :class:`RefusedError` or :class:`ConversionFailedError` with the data
    directory unchanged, and :class:`InstallError` for an install that running
    the conversion again finishes.
    """
    data_dir = Path(data_dir).expanduser().resolve()
    require_data_directory(data_dir)
    require_no_server(data_dir)
    try:
        pending = read_plan(data_dir)
    except InstallError as error:
        raise RefusedError(str(error)) from error
    if pending is not None:
        if dry_run:
            raise RefusedError(
                "an interrupted install is pending; run the conversion without --dry-run "
                "to finish it"
            )
        return _resume(data_dir, pending)
    resume = resumable_guard(data_dir)
    require_not_converted(data_dir)
    require_free_backup(data_dir)
    checks = [area.check_source for area in AREAS if area.check_source is not None]
    require_supported_sources(data_dir, checks)
    required, free = require_space(data_dir)
    try:
        guard = begin_maintenance(data_dir, OPERATION, resume=resume)
    except DatabaseError as error:
        raise RefusedError(str(error)) from error
    plan = _staged_plan(data_dir, guard, dry_run=dry_run, space=(required, free))
    if dry_run:
        try:
            _discard(staging_path(data_dir))
            finish_maintenance(data_dir, guard)
        except (OSError, DatabaseError) as error:
            raise ConversionFailedError(f"the dry run could not clean up: {error}") from error
        return plan.report
    return _install(data_dir, plan, guard)


def _staged_plan(
    data_dir: Path, guard: MaintenanceOperation, *, dry_run: bool, space: tuple[int, int]
) -> InstallPlan:
    """Stage and verify under the guard; on any failure, discard staging and release it."""
    try:
        return _stage_and_verify(data_dir, dry_run=dry_run, space=space)
    except BaseException as error:
        # Nothing was installed: the source is untouched and needs no guard.
        with contextlib.suppress(OSError):
            _discard(staging_path(data_dir))
        with contextlib.suppress(DatabaseError):
            finish_maintenance(data_dir, guard)
        if isinstance(error, (OSError, DatabaseError)):
            raise ConversionFailedError(str(error)) from error
        raise


def _stage_and_verify(data_dir: Path, *, dry_run: bool, space: tuple[int, int]) -> InstallPlan:
    # A server may have started while the guard was taken.
    require_no_server(data_dir)
    require_not_converted(data_dir)
    _discard(staging_path(data_dir))
    context = ConversionContext(source=data_dir, staging=files_root(data_dir))
    context.staging.mkdir(parents=True)
    started_at = utc_now_timestamp()
    started_ns = time.time_ns()
    timings: dict[str, float] = {}
    try:
        for area in AREAS:
            timings[area.name] = _timed(functools.partial(area.convert, context))
        verification: dict[str, Any] = {}
        timings["verification"] = _timed(lambda: verification.update(_verify(context)))
    except (ConversionError, DatabaseError, OSError) as error:
        raise ConversionFailedError(str(error)) from error
    databases = verification.pop("staged_databases")
    report = _report(
        data_dir,
        context,
        dry_run=dry_run,
        started_at=started_at,
        timings=timings,
        verification=verification,
        space=space,
    )
    try:
        plan = build_plan(data_dir, context.retired, databases, report)
    except InstallError as error:
        raise ConversionFailedError(str(error)) from error
    _add_install_summary(data_dir, plan)
    if dry_run:
        return plan
    changed = changed_sources(data_dir, plan, started_ns)
    if changed:
        raise ConversionFailedError(
            "source files changed while they were converted, so a vBot process may still use "
            f"the data directory: {', '.join(changed[:10])}"
        )
    require_no_server(data_dir)
    write_plan(data_dir, plan)
    return plan


def _verify(context: ConversionContext) -> dict[str, Any]:
    # The Session check runs first: loading Sessions through the application may
    # reconcile the staged database, which the database checks must then see.
    session_check = check_sessions(context)
    databases = verify_databases(context.staging)
    documents = verify_json_documents(context.source, context.staging)
    return {
        "sessions": None if session_check is None else session_check.to_dict(),
        "databases": {database.name: database.to_dict() for database in databases},
        "json_documents": documents,
        "staged_databases": [database.relative for database in databases],
    }


def _timed(action: Callable[[], None]) -> float:
    started = time.monotonic()
    action()
    return round(time.monotonic() - started, 1)


def _report(
    data_dir: Path,
    context: ConversionContext,
    *,
    dry_run: bool,
    started_at: str,
    timings: dict[str, float],
    verification: dict[str, Any],
    space: tuple[int, int],
) -> dict[str, Any]:
    skipped = context.report.to_dict()["skipped"]
    by_area: dict[str, int] = {}
    for item in skipped:
        by_area[item["area"]] = by_area.get(item["area"], 0) + 1
    return {
        "format_version": REPORT_FORMAT_VERSION,
        "data_directory": str(data_dir),
        "mode": "dry_run" if dry_run else "install",
        "result": "verified",
        "started_at": started_at,
        "finished_at": None,
        "areas": context.report.to_dict()["counts"],
        "skipped_by_area": by_area,
        "skipped": skipped,
        "verification": verification,
        "timings_seconds": timings,
        "space": {"required_bytes": space[0], "free_bytes_before": space[1]},
    }


def _add_install_summary(data_dir: Path, plan: InstallPlan) -> None:
    root = files_root(data_dir)
    moved_aside = sum(tree_size(data_dir / relative) for relative in plan.retire)
    installed = sum(tree_size(root / relative) for relative in plan.install)
    plan.report["install"] = {
        "backup_directory": BACKUP_DIRECTORY,
        "moved_aside": list(plan.retire),
        "installed": list(plan.install),
        "registered_databases": list(plan.databases),
    }
    plan.report["sizes"] = {
        "moved_aside_bytes": moved_aside,
        "installed_bytes": installed,
        "databases_before": {
            relative: tree_size(data_dir / relative)
            for relative in plan.databases
            if (data_dir / relative).is_file()
        },
        "databases_after": {relative: tree_size(root / relative) for relative in plan.databases},
    }
    plan.report["finished_at"] = utc_now_timestamp()


def _install(data_dir: Path, plan: InstallPlan, guard: MaintenanceOperation) -> dict[str, Any]:
    plan.report["result"] = "installed"
    plan.report["finished_at"] = utc_now_timestamp()
    try:
        execute(data_dir, plan)
        finish_maintenance(data_dir, guard)
    except (InstallError, OSError, DatabaseError) as error:
        raise InstallError(
            f"{error}. The install is incomplete: vBot refuses the data directory until the "
            "conversion is run again, which finishes it"
        ) from error
    with contextlib.suppress(OSError):
        # A leftover staging directory only holds the finished plan.
        _discard(staging_path(data_dir))
    return plan.report


def _resume(data_dir: Path, plan: InstallPlan) -> dict[str, Any]:
    """Finish an install that an earlier run began."""
    plan.report["resumed"] = True
    if read_maintenance(data_dir) is None and (data_dir / MARKER_FILE_NAME).exists():
        # The install completed and released its guard; only staging was left.
        _discard(staging_path(data_dir))
        plan.report["result"] = "installed"
        return plan.report
    resumable_guard(data_dir)
    try:
        guard = begin_maintenance(data_dir, OPERATION, resume=True)
    except DatabaseError as error:
        raise RefusedError(str(error)) from error
    return _install(data_dir, plan, guard)


def _discard(path: Path) -> None:
    def make_writable_and_retry(
        action: Callable[[str], object], target: str, _error: object
    ) -> None:
        # Staged JSON keeps source permissions, including Windows read-only files.
        # Only this disposable copy becomes writable; the source is untouched.
        os.chmod(target, stat.S_IMODE(Path(target).stat().st_mode) | stat.S_IWUSR)
        action(target)

    if path.exists():
        shutil.rmtree(path, onerror=make_writable_and_retry)
