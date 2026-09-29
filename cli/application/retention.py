"""Remove application versions and downloads that nothing can still use.

Every update adds a complete version (runtime, dependencies and WebUI), so
versions accumulate. After an update completes, the worker retires every
complete version outside the needed set:

- the active version;
- versions named by operations since the last one that changed the active
  version, including that operation's previous version as a known-good
  fallback, and by every unfinished operation;
- versions named by the local customization state;
- versions that a private Python environment is based on (the customization
  build environment and the managed speech environments);
- versions a running process of this installation uses: its executable, or,
  for the root bootstraps, a module it has loaded.

A vBot process that cannot be inspected keeps every version. Retiring renames
a version below ``staging/`` while the worker holds the operation lock, so no
version can be activated, created or started concurrently. Deleting the
renamed trees happens afterwards, outside the lock. A tree that cannot be
renamed or completely deleted stays for the next cleanup. Cleanup never
changes an operation's outcome.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Iterable
from pathlib import Path

import psutil  # type: ignore[import-untyped]

from cli.application.state import (
    NATIVE_HOST_NAMES,
    Installation,
    contained,
    operations,
)
from core.utils.ids import is_safe_id

_LOGGER = logging.getLogger("vbot.application.retention")
_RETIRED_PREFIX = "retired-"


class _UndecidableError(Exception):
    """Whether a version is still used cannot be established."""


def retire_unneeded(install: Installation) -> list[Path]:
    """Move unneeded versions and finished downloads below ``staging/``.

    The caller holds the operation lock and passes the result to
    ``remove_retired`` after releasing it. Never raises.
    """
    try:
        staging = contained(install.root, "staging")
        # Trees an interrupted cleanup already moved out of use.
        retired = sorted(staging.glob(f"{_RETIRED_PREFIX}*"))
    except Exception:
        _LOGGER.warning("Application version cleanup was skipped", exc_info=True)
        return []
    earlier = len(retired)
    try:
        needed = _needed_versions(install)
        for version in _plain_directories(contained(install.root, "versions")):
            if (
                os.path.normcase(version.name) not in needed
                and (version / "release.json").is_file()
            ):
                _retire(version, staging, retired)
        if len(retired) > earlier:
            _LOGGER.info("Retired unneeded application versions (count=%d)", len(retired) - earlier)
        unfinished = {operation.id for operation in operations(install) if not operation.terminal}
        for download in _plain_directories(contained(install.root, "downloads")):
            if download.name not in unfinished:
                _retire(download, staging, retired)
    except _UndecidableError as exc:
        _LOGGER.warning("Application version cleanup kept every version (reason=%s)", exc)
    except Exception:
        _LOGGER.warning("Application version cleanup stopped early", exc_info=True)
    return retired


def remove_retired(paths: Iterable[Path]) -> None:
    """Delete retired trees; whatever remains is retried by the next cleanup."""
    for path in paths:
        shutil.rmtree(path, ignore_errors=True)
        if path.exists():
            _LOGGER.warning("Could not completely remove %s; the next update retries", path)


def _retire(path: Path, staging: Path, retired: list[Path]) -> None:
    staging.mkdir(exist_ok=True)
    target = staging / f"{_RETIRED_PREFIX}{path.parent.name}-{path.name}"
    suffix = 0
    while target.exists():
        suffix += 1
        target = staging / f"{_RETIRED_PREFIX}{path.parent.name}-{path.name}-{suffix}"
    try:
        path.rename(target)
    except OSError as exc:
        # Windows refuses to rename a tree that is still open, e.g. a working directory.
        _LOGGER.warning("Application version cleanup deferred a tree (path=%s error=%s)", path, exc)
        return
    retired.append(target)


def _plain_directories(root: Path) -> list[Path]:
    """Managed entries only: safe names, real directories, never links or junctions."""
    if not root.is_dir():
        return []
    return [
        path
        for path in sorted(root.iterdir())
        if is_safe_id(path.name)
        and path.is_dir()
        and not path.is_symlink()
        and not (hasattr(path, "is_junction") and path.is_junction())
    ]


def _needed_versions(install: Installation) -> set[str]:
    try:
        needed = {install.version().name}
        needed |= _operation_versions(install)
        needed |= _customization_versions(install)
        needed |= _environment_versions(install)
    except (OSError, ValueError) as exc:
        raise _UndecidableError(str(exc)) from exc
    # Windows names the same directory in any letter case.
    return {os.path.normcase(version) for version in needed | _running_versions(install)}


def _operation_versions(install: Installation) -> set[str]:
    needed: set[str] = set()
    changed_active = False
    for operation in operations(install):  # newest first
        named = {operation.previous_version, operation.candidate_version} - {None}
        if not changed_active or not operation.terminal:
            needed |= {str(version) for version in named}
        if (
            operation.phase == "completed"
            and operation.previous_version is not None
            and operation.candidate_version is not None
            and operation.previous_version != operation.candidate_version
        ):
            changed_active = True
    return needed


def _customization_versions(install: Installation) -> set[str]:
    from cli.application.customize import development_state

    state = development_state(install)
    if state is None:
        return set()
    records = [state, state.get("pending_rebase")]
    return {
        value
        for record in records
        if isinstance(record, dict)
        for key, value in record.items()
        if key.endswith("_version") and isinstance(value, str) and is_safe_id(value)
    }


def _environment_versions(install: Installation) -> set[str]:
    configurations = list(contained(install.root, "development").glob("*/pyvenv.cfg"))
    if install.server_data_directory is not None:
        from core.storage.layout import DataDirectoryLayout

        speech = DataDirectoryLayout(Path(install.server_data_directory)).speech_engines
        configurations += speech.glob("*/pyvenv.cfg")
    versions = contained(install.root, "versions").resolve()
    needed: set[str] = set()
    for configuration in configurations:
        for line in configuration.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip().casefold() == "home":
                version = _version_of(Path(value.strip()).resolve(), versions)
                if version is not None:
                    needed.add(version)
    return needed


def _running_versions(install: Installation) -> set[str]:
    root = install.root.resolve()
    versions = contained(install.root, "versions").resolve()
    needed: set[str] = set()
    for process in psutil.process_iter(["name", "exe"]):
        executable = process.info.get("exe")
        if not executable:
            if str(process.info.get("name") or "").casefold() in NATIVE_HOST_NAMES:
                raise _UndecidableError(f"vBot process {process.pid} cannot be inspected")
            continue
        path = Path(executable)
        if not path.is_relative_to(root):
            continue
        version = _version_of(path, versions)
        if version is not None:
            needed.add(version)
            continue
        # A root bootstrap runs the version that was active when it started,
        # possibly for days; its loaded modules name that version.
        try:
            mapped = [Path(region.path) for region in process.memory_maps(grouped=True)]
        except psutil.NoSuchProcess:
            continue
        except (psutil.Error, OSError) as exc:
            raise _UndecidableError(f"vBot process {process.pid} cannot be inspected") from exc
        needed |= {
            version
            for version in (_version_of(item, versions) for item in mapped)
            if version is not None
        }
    return needed


def _version_of(path: Path, versions: Path) -> str | None:
    try:
        parts = path.relative_to(versions).parts
    except ValueError:
        return None
    return parts[0] if parts else None
