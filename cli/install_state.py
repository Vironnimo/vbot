"""Persistent local installation state for install, update, and uninstall.

The git checkout answers where source code comes from.  It does not answer which
product shape was installed into the current Python environment.  This module
stores that independent contract in ``.vbot-install.json`` at the checkout root
so lifecycle commands never have to conflate a branch with dev dependencies or
an importable Desktop module with a server installation.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.utils.atomic import atomic_write_text

INSTALL_STATE_FILE = ".vbot-install.json"
INSTALL_STATE_SCHEMA_VERSION = 2
_SUPPORTED_INSTALL_STATE_SCHEMA_VERSIONS = frozenset({1, INSTALL_STATE_SCHEMA_VERSION})

SERVER_SHAPE = "server"
SERVER_DESKTOP_SHAPE = "server-desktop"
DESKTOP_CLIENT_SHAPE = "desktop-client"
INSTALL_SHAPES = frozenset({SERVER_SHAPE, SERVER_DESKTOP_SHAPE, DESKTOP_CLIENT_SHAPE})

DEV_TRACK = "dev"
RELEASE_TRACK = "release"
SOURCE_TRACKS = frozenset({DEV_TRACK, RELEASE_TRACK})

_GROUP_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")
_DESKTOP_PROBE_MODULE = "webview"
_SERVER_PROBE_MODULE = "fastapi"
_DEV_PROBE_MODULES = ("pytest", "ruff", "mypy")


class InstallStateError(ValueError):
    """Raised when the local installation manifest is invalid or unreadable."""


@dataclass(frozen=True)
class InstallState:
    """The successfully applied state of one checkout-backed installation."""

    schema_version: int
    install_shape: str
    dependency_groups: tuple[str, ...]
    python_executable: str
    source_track: str
    applied_revision: str
    dependency_digest: str
    webui_revision: str | None
    server_host: str | None = None
    server_port: int | None = None
    server_data_directory: str | None = None


def install_state_path(root: Path) -> Path:
    """Return the manifest path for a checkout root."""

    return root / INSTALL_STATE_FILE


def read_install_state(root: Path) -> InstallState | None:
    """Read and validate the manifest, returning ``None`` when it is absent."""

    path = install_state_path(root)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallStateError(f"could not read {path}: {exc}") from exc
    return _parse_install_state(payload, path)


def write_install_state(root: Path, state: InstallState) -> None:
    """Validate and atomically persist one installation manifest."""

    validated = _parse_install_state(_state_payload(state), install_state_path(root))
    content = json.dumps(_state_payload(validated), indent=2, ensure_ascii=False) + "\n"
    atomic_write_text(install_state_path(root), content)


def build_install_state(
    root: Path,
    *,
    install_shape: str,
    dependency_groups: tuple[str, ...],
    python_executable: str,
    server_host: str | None = None,
    server_port: int | None = None,
    server_data_directory: str | None = None,
) -> InstallState:
    """Build the completed state written by an installer."""

    track = detect_source_track(root)
    revision = git_revision(root)
    webui_revision = (
        revision
        if install_shape != DESKTOP_CLIENT_SHAPE
        and (root / "webui" / "dist" / "index.html").is_file()
        else None
    )
    return _parse_install_state(
        {
            "schema_version": INSTALL_STATE_SCHEMA_VERSION,
            "install_shape": install_shape,
            "dependency_groups": list(dependency_groups),
            "python_executable": _absolute_path_preserving_symlinks(python_executable),
            "source_track": track,
            "applied_revision": revision,
            "dependency_digest": file_digest(root / "pyproject.toml"),
            "webui_revision": webui_revision,
            "server_host": server_host,
            "server_port": server_port,
            "server_data_directory": (
                _absolute_path_preserving_symlinks(server_data_directory)
                if server_data_directory is not None
                else None
            ),
        },
        install_state_path(root),
    )


def infer_legacy_install_state(root: Path, *, track: str, revision: str) -> InstallState:
    """Infer a one-time state for an installation made before manifests existed.

    Existing isolated bootstrap environments are unambiguous: a Desktop Client
    has the Desktop stack but no FastAPI server stack.  Shared manual Python
    environments can contain unrelated packages, so the inferred shape is also
    printed by the updater and immediately persisted for future deterministic
    runs.
    """

    desktop_installed = _module_installed(_DESKTOP_PROBE_MODULE)
    server_installed = _module_installed(_SERVER_PROBE_MODULE)
    dev_installed = all(_module_installed(name) for name in _DEV_PROBE_MODULES)
    groups: tuple[str, ...]

    if desktop_installed and not server_installed:
        shape = DESKTOP_CLIENT_SHAPE
        groups = ("cli", "desktop")
    elif desktop_installed:
        shape = SERVER_DESKTOP_SHAPE
        groups = ("dev", "desktop") if dev_installed else ("server", "cli", "desktop")
    else:
        shape = SERVER_SHAPE
        groups = ("dev",) if dev_installed else ("server", "cli")

    webui_revision = (
        revision
        if shape != DESKTOP_CLIENT_SHAPE and (root / "webui" / "dist" / "index.html").is_file()
        else None
    )
    return InstallState(
        schema_version=INSTALL_STATE_SCHEMA_VERSION,
        install_shape=shape,
        dependency_groups=groups,
        python_executable=_absolute_path_preserving_symlinks(sys.executable),
        source_track=track,
        applied_revision=revision,
        dependency_digest=file_digest(root / "pyproject.toml"),
        webui_revision=webui_revision,
    )


def detect_source_track(root: Path) -> str:
    """Return ``dev`` for a branch checkout and ``release`` for detached HEAD."""

    result = _run_git(root, "symbolic-ref", "-q", "--short", "HEAD")
    return DEV_TRACK if result.returncode == 0 and result.stdout.strip() else RELEASE_TRACK


def git_revision(root: Path) -> str:
    """Return the current git commit, or an empty string outside a valid checkout."""

    result = _run_git(root, "rev-parse", "HEAD")
    return result.stdout.strip() if result.returncode == 0 else ""


def file_digest(path: Path) -> str:
    """Return a SHA-256 digest, or an empty string when the file is unavailable."""

    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _absolute_path_preserving_symlinks(path: str) -> str:
    """Keep a venv entry point distinct from the base interpreter it links to."""

    return os.path.abspath(os.path.expanduser(path))


def _module_installed(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _run_git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(["git", *arguments], 127, "", "")


def _state_payload(state: InstallState) -> dict[str, Any]:
    payload = asdict(state)
    payload["dependency_groups"] = list(state.dependency_groups)
    return payload


def _parse_install_state(payload: object, path: Path) -> InstallState:
    if not isinstance(payload, dict):
        raise InstallStateError(f"{path} must contain a JSON object")

    schema_version = payload.get("schema_version")
    if schema_version not in _SUPPORTED_INSTALL_STATE_SCHEMA_VERSIONS:
        supported = ", ".join(
            str(version) for version in sorted(_SUPPORTED_INSTALL_STATE_SCHEMA_VERSIONS)
        )
        raise InstallStateError(f"{path} schema_version must be one of: {supported}")

    shape = payload.get("install_shape")
    if shape not in INSTALL_SHAPES:
        raise InstallStateError(f"{path} has invalid install_shape {shape!r}")

    groups_value = payload.get("dependency_groups")
    if not isinstance(groups_value, list) or not groups_value:
        raise InstallStateError(f"{path} dependency_groups must be a non-empty list")
    if not all(
        isinstance(group, str) and _GROUP_PATTERN.fullmatch(group) for group in groups_value
    ):
        raise InstallStateError(f"{path} contains an invalid dependency group")
    groups = tuple(dict.fromkeys(groups_value))

    python_executable = payload.get("python_executable")
    if not isinstance(python_executable, str) or not python_executable.strip():
        raise InstallStateError(f"{path} python_executable must be a non-empty string")

    source_track = payload.get("source_track")
    if source_track not in SOURCE_TRACKS:
        raise InstallStateError(f"{path} has invalid source_track {source_track!r}")

    applied_revision = payload.get("applied_revision")
    dependency_digest = payload.get("dependency_digest")
    if not isinstance(applied_revision, str) or not isinstance(dependency_digest, str):
        raise InstallStateError(f"{path} revision and digest fields must be strings")

    webui_revision = payload.get("webui_revision")
    if webui_revision is not None and not isinstance(webui_revision, str):
        raise InstallStateError(f"{path} webui_revision must be a string or null")
    if shape == DESKTOP_CLIENT_SHAPE and webui_revision is not None:
        raise InstallStateError(f"{path} desktop-client must not own a WebUI revision")

    server_host = payload.get("server_host")
    server_port = payload.get("server_port")
    server_data_directory = payload.get("server_data_directory")
    server_target = (server_host, server_port, server_data_directory)
    if any(value is not None for value in server_target):
        if not all(value is not None for value in server_target):
            raise InstallStateError(f"{path} server target must be complete")
        if shape == DESKTOP_CLIENT_SHAPE:
            raise InstallStateError(f"{path} desktop-client must not own a server target")
        if not isinstance(server_host, str) or not server_host.strip():
            raise InstallStateError(f"{path} server_host must be a non-empty string")
        if isinstance(server_port, bool) or not isinstance(server_port, int):
            raise InstallStateError(f"{path} server_port must be an integer")
        if not 1 <= server_port <= 65535:
            raise InstallStateError(f"{path} server_port must be between 1 and 65535")
        if not isinstance(server_data_directory, str) or not server_data_directory.strip():
            raise InstallStateError(f"{path} server_data_directory must be a non-empty string")

    return InstallState(
        schema_version=INSTALL_STATE_SCHEMA_VERSION,
        install_shape=shape,
        dependency_groups=groups,
        python_executable=python_executable,
        source_track=source_track,
        applied_revision=applied_revision,
        dependency_digest=dependency_digest,
        webui_revision=webui_revision,
        server_host=server_host,
        server_port=server_port,
        server_data_directory=server_data_directory,
    )


def _parse_cli(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage vBot's local installation manifest")
    subparsers = parser.add_subparsers(dest="command", required=True)

    write_parser = subparsers.add_parser("write")
    write_parser.add_argument("--root", required=True)
    write_parser.add_argument("--shape", required=True, choices=sorted(INSTALL_SHAPES))
    write_parser.add_argument("--groups", required=True, nargs="+")
    write_parser.add_argument("--python-executable", required=True)
    write_parser.add_argument("--server-host")
    write_parser.add_argument("--server-port", type=int)
    write_parser.add_argument("--server-data-directory")

    python_parser = subparsers.add_parser("python")
    python_parser.add_argument("--root", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Small script-facing interface used by the cross-platform installers."""

    args = _parse_cli(argv)
    root = Path(args.root).expanduser().resolve()
    try:
        if args.command == "write":
            state = build_install_state(
                root,
                install_shape=args.shape,
                dependency_groups=tuple(args.groups),
                python_executable=args.python_executable,
                server_host=args.server_host,
                server_port=args.server_port,
                server_data_directory=args.server_data_directory,
            )
            write_install_state(root, state)
            print(f"installation manifest: {install_state_path(root)} ({state.install_shape})")
            return 0

        existing_state = read_install_state(root)
        if existing_state is None:
            print(f"installation manifest not found: {install_state_path(root)}", file=sys.stderr)
            return 1
        print(existing_state.python_executable)
        return 0
    except (InstallStateError, OSError) as exc:
        print(f"installation manifest error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
