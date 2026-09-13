"""User-managed Extension dependency recipes for immutable application releases."""

from __future__ import annotations

import csv
import os
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from cli.application.state import ApplicationError, Installation, contained, safe_id, write_json
from core.extensions.dependencies import (
    DependencyError,
    active_record,
    normalize_distribution,
    runtime_dependencies,
)
from core.utils.ids import new_id

_MAX_REQUIREMENTS_BYTES = 1024 * 1024


def environment_creation_command(
    install: Installation, destination: Path
) -> tuple[list[str], dict[str, str]]:
    """Build an immutable-safe uv command for a private managed environment."""
    launcher = install.interpreter(role="Python")
    base_python = (
        launcher.with_name("python.exe")
        if os.name == "nt"
        else install.version() / "runtime" / "bin" / "python3"
    )
    if not base_python.is_file():
        raise ApplicationError("The private application Python runtime is unavailable")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("VBOT_RUN_")
        and key
        not in {
            "VBOT_INSTALL_ROOT",
            "VBOT_DATA_DIR",
            "VBOT_UPDATE_HANDOFF",
            "PYTHONHOME",
            "PYTHONPATH",
            "VIRTUAL_ENV",
        }
    }
    environment.update(
        {
            "PIP_NO_INPUT": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }
    )
    return (
        [
            str(launcher),
            "-m",
            "uv",
            "venv",
            "--python",
            str(base_python),
            "--seed",
            str(destination),
        ],
        environment,
    )


def install_dependencies(install: Installation, requirements: Path) -> dict[str, Any]:
    """Resolve one explicit local replacement recipe for the active release."""
    if not install.owns_server:
        raise ApplicationError("Desktop Client installations cannot install Extension dependencies")
    contents = _read_requirements(requirements)
    return _resolve(install, install.version().name, contents)


def prepare_for_version(install: Installation, version_id: str) -> None:
    """Resolve the active recipe for a staged version before its server stops."""
    if not install.owns_server:
        return
    if not (_data_dir(install) / "extension-dependencies").exists():
        return
    current = runtime_dependencies(install.version())
    try:
        record = active_record(_data_dir(install), current)
    except DependencyError as exc:
        raise ApplicationError(f"Active Extension dependency recipe is invalid: {exc}") from exc
    if record is None:
        return
    _resolve(install, safe_id(version_id), record["requirements"])


def status(install: Installation) -> dict[str, Any]:
    """Describe the active release's compatible user-managed dependencies."""
    if not install.owns_server:
        return {"available": False, "reason": "desktop_client"}
    try:
        runtime = runtime_dependencies(install.version())
        record = active_record(_data_dir(install), runtime)
    except DependencyError as exc:
        return {"available": False, "reason": "invalid", "message": str(exc)}
    if record is None:
        return {
            "available": False,
            "reason": "not_configured",
            "compatibility": runtime.fingerprint,
        }
    return {
        "available": True,
        "compatibility": runtime.fingerprint,
        "requirements": record["requirements"],
        "site": str(_site_path(install, runtime.fingerprint, record["site_relative"])),
    }


def _resolve(install: Installation, version_id: str, contents: str) -> dict[str, Any]:
    if not install.owns_server:
        raise ApplicationError("Desktop Client installations cannot install Extension dependencies")
    version = install.version(version_id)
    try:
        runtime = runtime_dependencies(version)
    except DependencyError as exc:
        raise ApplicationError(f"Release runtime inventory is invalid: {exc}") from exc
    data = _data_dir(install)
    base = _dependency_root(data, runtime.fingerprint)
    try:
        existing = active_record(data, runtime)
    except DependencyError:
        existing = None
    if existing is not None and existing["requirements"] == contents:
        return {
            "available": True,
            "compatibility": runtime.fingerprint,
            "requirements": contents,
            "site": str(_site_path(install, runtime.fingerprint, existing["site_relative"])),
        }
    base.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="staging-", dir=base))
    try:
        site = staging / "site"
        recipe = staging / "requirements.txt"
        constraints = staging / "constraints.txt"
        recipe.write_text(contents, encoding="utf-8")
        constraints.write_text(
            "".join(
                f"{name}=={version}\n" for name, version in sorted(runtime.distributions.items())
            ),
            encoding="utf-8",
        )
        _run_uv(
            install.interpreter(version_id, "Python"),
            recipe,
            constraints,
            site,
            data / "extension-dependencies" / "install.log",
        )
        _remove_runtime_duplicates(site, runtime.distributions)
        _safe_tree(site, site)
        candidate = new_id("deps")
        destination = base / candidate
        site_relative = f"{candidate}/site"
        staging.rename(destination)
        record = {
            "schema_version": 1,
            "compatibility": runtime.fingerprint,
            "requirements": contents,
            "site_relative": site_relative,
        }
        write_json(base / "active.json", record)
        return {
            "available": True,
            "compatibility": runtime.fingerprint,
            "requirements": contents,
            "site": str(destination / "site"),
        }
    except (OSError, DependencyError) as exc:
        raise ApplicationError(f"Extension dependency installation failed: {exc}") from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _run_uv(python: Path, requirements: Path, constraints: Path, site: Path, log: Path) -> None:
    base_python = python.with_name("python.exe") if os.name == "nt" else python
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("VBOT_RUN_")
        and key
        not in {
            "VBOT_INSTALL_ROOT",
            "VBOT_DATA_DIR",
            "VBOT_UPDATE_HANDOFF",
            "PYTHONHOME",
            "PYTHONPATH",
            "VIRTUAL_ENV",
        }
    }
    environment.update(
        {"PIP_NO_INPUT": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}
    )
    log.parent.mkdir(parents=True, exist_ok=True)
    log.touch(mode=0o600, exist_ok=True)
    log.chmod(0o600)
    with log.open("ab") as output:
        result = subprocess.run(
            [
                str(python),
                "-m",
                "uv",
                "pip",
                "install",
                "--python",
                str(base_python),
                "--target",
                str(site),
                "--constraint",
                str(constraints),
                "--requirement",
                str(requirements),
            ],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            check=False,
            env=environment,
        )
    if result.returncode:
        raise ApplicationError(
            f"shipped uv failed with exit code {result.returncode}; inspect {log}"
        )


def _remove_runtime_duplicates(site: Path, runtime: dict[str, str]) -> None:
    for metadata in site.glob("*.dist-info"):
        if metadata.is_symlink() or not metadata.is_dir():
            raise DependencyError("Extension dependency metadata is unsafe")
        name, version = _metadata_name_version(metadata / "METADATA")
        if name not in runtime:
            continue
        if version != runtime[name]:
            raise DependencyError(f"Extension dependency conflicts with runtime package {name}")
        record = metadata / "RECORD"
        if not record.is_file() or record.is_symlink():
            raise DependencyError("Runtime duplicate has no safe RECORD")
        with record.open(encoding="utf-8", newline="") as handle:
            entries = [row[0] for row in csv.reader(handle) if row]
        for entry in entries:
            path = _record_path(site, entry)
            if path.is_symlink():
                raise DependencyError("Runtime duplicate RECORD contains a link")
            if path.is_file():
                path.unlink()
        if metadata.exists():
            shutil.rmtree(metadata)


def _metadata_name_version(path: Path) -> tuple[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise DependencyError("Extension dependency metadata is unreadable") from exc
    values = {line.split(":", 1)[0]: line.split(":", 1)[1].strip() for line in lines if ":" in line}
    try:
        return normalize_distribution(values["Name"]), values["Version"]
    except KeyError as exc:
        raise DependencyError("Extension dependency metadata is incomplete") from exc


def _record_path(site: Path, value: str) -> Path:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise DependencyError("Runtime duplicate RECORD escapes its site")
    result = site / path.as_posix()
    try:
        if not result.resolve().is_relative_to(site.resolve()):
            raise DependencyError("Runtime duplicate RECORD escapes its site")
    except OSError as exc:
        raise DependencyError("Runtime duplicate RECORD is unavailable") from exc
    return result


def _read_requirements(path: Path) -> str:
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > _MAX_REQUIREMENTS_BYTES:
            raise ApplicationError("Extension requirements file is invalid")
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ApplicationError("Cannot read Extension requirements file") from exc


def _data_dir(install: Installation) -> Path:
    if not install.server_data_directory:
        raise ApplicationError("Installation has no server data directory")
    return Path(install.server_data_directory)


def _dependency_root(data_dir: Path, fingerprint: str) -> Path:
    return contained(data_dir, f"extension-dependencies/{fingerprint}")


def _site_path(install: Installation, fingerprint: str, relative: str) -> Path:
    base = _dependency_root(_data_dir(install), fingerprint)
    return contained(base, relative)


def _safe_tree(root: Path, path: Path) -> None:
    for item in path.rglob("*"):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise DependencyError("Extension dependency site contains links")
        if not item.resolve().is_relative_to(root.resolve()):
            raise DependencyError("Extension dependency site escapes its root")
