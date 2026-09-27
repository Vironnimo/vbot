"""Shared fakes for ``vbot update`` tests: a scripted checkout, restart recorders, the manifest."""

from __future__ import annotations

import io
import sys
import tarfile
from collections.abc import Callable, Sequence
from pathlib import Path

from cli._update_types import _Step
from cli.install_state import (
    INSTALL_STATE_SCHEMA_VERSION,
    InstallState,
    write_install_state,
)
from cli.install_state import dependency_digest as current_dependency_digest
from cli.server_management import CommandResult, ServerInstance
from cli.update_management import CommandRun

Handler = Callable[[list[str]], CommandRun]


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


def _upstream(*, behind: int = 0, ahead: int = 0) -> CommandRun:
    """Answer ``git rev-list --left-right --count HEAD...@{upstream}``."""

    return _ok(f"{ahead}	{behind}")


def checkout(
    *,
    heads: str | Sequence[str] = "samesha",
    branch: bool = True,
    status: str = "",
    upstream: CommandRun | None = None,
    on_merge: Callable[[], object] | None = None,
    answer: Callable[[list[str]], CommandRun | None] | None = None,
) -> Handler:
    """Answer the updater's commands for one checkout.

    ``heads`` answers successive ``git rev-parse HEAD`` calls; the last one repeats. A branch
    checkout is on ``main`` with ``upstream`` as its ahead/behind count; ``branch=False`` is a
    detached release checkout. ``on_merge`` runs when the updater fast-forwards. ``answer`` sees
    every command first and may return a reply; every other command succeeds silently.
    """

    remaining = [heads] if isinstance(heads, str) else list(heads)

    def handle(command: list[str]) -> CommandRun:
        if answer is not None and (reply := answer(command)) is not None:
            return reply
        if command[:2] == ["git", "symbolic-ref"]:
            return _ok("main") if branch else _err()
        if command[:2] == ["git", "rev-parse"]:
            return _ok(remaining.pop(0) if len(remaining) > 1 else remaining[0])
        if command[:2] == ["git", "status"]:
            return _ok(status)
        if command[:2] == ["git", "rev-list"]:
            return upstream or _upstream()
        if command[:2] == ["git", "merge"] and on_merge is not None:
            on_merge()
        return _ok()

    return handle


def only_reads(*queries: str, **replies: CommandRun) -> Callable[[list[str]], CommandRun | None]:
    """An ``answer`` that fails the test on any command except the named git queries.

    ``replies`` answers named git subcommands; the remaining ``queries`` fall through to the
    checkout's defaults.
    """

    def answer(command: list[str]) -> CommandRun | None:
        if command[0] == "git" and command[1] in replies:
            return replies[command[1]]
        if command[0] == "git" and command[1] in queries:
            return None
        raise AssertionError(f"unexpected command: {command}")

    return answer


class ScriptedRunner:
    """Records command invocations and answers from a per-command handler."""

    def __init__(self, handler: Handler) -> None:
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


def _recording_snapshot() -> tuple[list[ServerInstance], Callable[[ServerInstance], _Step]]:
    taken: list[ServerInstance] = []

    def snapshot(instance: ServerInstance) -> _Step:
        taken.append(instance)
        return _Step(True, "test-owned snapshot")

    return taken, snapshot


def _webui_tar_bytes(files: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, payload in (files or {"dist/index.html": b"<!doctype html>"}).items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def write_webui_build(root: Path, content: str = "<!doctype html>") -> Path:
    """Write an installed WebUI build and return its ``dist`` directory."""

    dist = root / "webui" / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text(content, encoding="utf-8")
    return dist


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
                current_dependency_digest(root) if dependency_digest is None else dependency_digest
            ),
            webui_revision=None if shape == "desktop-client" else webui_revision,
            server_host=server_host,
            server_port=server_port,
            server_data_directory=server_data_directory,
        ),
    )
