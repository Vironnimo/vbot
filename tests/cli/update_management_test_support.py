"""Shared fixtures and fakes for update management behavior tests."""

from __future__ import annotations

import io
import sys
import tarfile
from collections.abc import Callable
from pathlib import Path

from cli.install_state import (
    INSTALL_STATE_SCHEMA_VERSION,
    InstallState,
    file_digest,
    write_install_state,
)
from cli.server_management import CommandResult, ServerInstance
from cli.update_management import (
    CommandRun,
)


def _instance() -> ServerInstance:
    return ServerInstance(
        host="127.0.0.1",
        port=8420,
        data_dir=Path("/data"),
        url="http://127.0.0.1:8420",
        log_path=Path("/data/logs/today.log"),
    )


def _ok(stdout: str = "") -> CommandRun:
    return CommandRun(returncode=0, stdout=stdout, stderr="")


def _err(stderr: str = "boom") -> CommandRun:
    return CommandRun(returncode=1, stdout="", stderr=stderr)


class ScriptedRunner:
    """Records command invocations and answers from a per-command handler."""

    def __init__(self, handler: Callable[[list[str]], CommandRun]) -> None:
        self._handler = handler
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], cwd: Path) -> CommandRun:
        self.calls.append(list(command))
        return self._handler(list(command))

    def ran(self, *needle: str) -> bool:
        target = list(needle)
        return any(
            call[index : index + len(target)] == target
            for call in self.calls
            for index in range(len(call) - len(target) + 1)
        )


def _recording_restart() -> tuple[
    list[str], Callable[..., CommandResult], Callable[..., CommandResult]
]:
    events: list[str] = []

    def stop(instance: ServerInstance) -> CommandResult:
        events.append("stop")
        return CommandResult(ok=True, message="stopped", instance=instance)

    def start(instance: ServerInstance) -> CommandResult:
        events.append("start")
        return CommandResult(ok=True, message="started", instance=instance)

    return events, stop, start


def _webui_tar_bytes(files: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, payload in (files or {"dist/index.html": b"<!doctype html>"}).items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def _write_state(
    root: Path,
    *,
    track: str = "dev",
    revision: str = "samesha",
    shape: str = "server",
    groups: tuple[str, ...] | None = None,
    python_executable: str | None = None,
    dependency_digest: str | None = None,
    webui_revision: str | None = None,
    server_host: str | None = None,
    server_port: int | None = None,
    server_data_directory: str | None = None,
) -> None:
    if groups is None:
        if shape == "desktop-client":
            groups = ("cli", "desktop")
        elif shape == "server-desktop":
            groups = ("server", "cli", "desktop")
        else:
            groups = ("server", "cli")
    write_install_state(
        root,
        InstallState(
            schema_version=INSTALL_STATE_SCHEMA_VERSION,
            install_shape=shape,
            dependency_groups=groups,
            python_executable=python_executable or sys.executable,
            source_track=track,
            applied_revision=revision,
            dependency_digest=(
                file_digest(root / "pyproject.toml")
                if dependency_digest is None
                else dependency_digest
            ),
            webui_revision=None if shape == "desktop-client" else webui_revision,
            server_host=server_host,
            server_port=server_port,
            server_data_directory=server_data_directory,
        ),
    )
