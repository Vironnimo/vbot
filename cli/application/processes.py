"""Run exact installed versions through the existing process lifecycle boundary."""

from __future__ import annotations

import os
import subprocess
import time
from contextlib import suppress
from pathlib import Path

import psutil  # type: ignore[import-untyped]

from cli.application.operations import child_environment
from cli.application.state import ApplicationError, Installation
from cli.server_management import (
    CommandResult,
    ServerInstance,
    probe_health,
    probe_webui,
    resolve_instance,
    stop_server,
)
from core.utils.logging import CONSOLE_LOGGING_ENV_VAR
from core.utils.processes import subprocess_creation_flags
from core.utils.server_control import read_server_control

# The startup log keeps one previous generation once it outgrows this bound.
STARTUP_LOG_ROTATE_BYTES = 1024 * 1024


def target(install: Installation) -> ServerInstance:
    if not install.owns_server:
        raise ApplicationError("This Desktop Client installation has no local server")
    assert install.server_host is not None and install.server_port is not None
    assert install.server_data_directory is not None
    return resolve_instance(
        host=install.server_host, port=install.server_port, data_dir=install.server_data_directory
    )


def running_server_matches(
    install: Installation,
    *,
    version_id: str | None = None,
    verification: bool = False,
) -> bool:
    """Prove the listener belongs to the exact installed version and startup mode."""
    instance = target(install)
    health = probe_health(instance)
    if not health.is_vbot:
        return False
    record = read_server_control(instance.data_dir, instance.port)
    if record is None:
        return False
    try:
        process = psutil.Process(record.pid)
        if abs(process.create_time() - record.process_create_time) >= 0.001:
            return False
        expected = install.interpreter(version_id, "Server").resolve()
        actual = Path(process.exe()).resolve()
        if os.path.normcase(str(actual)) != os.path.normcase(str(expected)):
            return False
        command = process.cmdline()
    except (OSError, psutil.Error, ApplicationError):
        return False
    return ("--verification-only" in command) is verification


def start(
    install: Installation,
    *,
    version_id: str | None = None,
    verification: bool = False,
    timeout: float = 90,
    breakaway: bool = True,
) -> CommandResult:
    instance = target(install)
    current = probe_health(instance)
    if current.reachable:
        matches = current.is_vbot and running_server_matches(
            install, version_id=version_id, verification=verification
        )
        return CommandResult(
            ok=matches and not verification,
            message=(
                "already running"
                if matches and not verification
                else "Server port is occupied by another application version or startup mode"
            ),
            instance=instance,
            health=current,
            webui=probe_webui(instance) if matches and not verification else None,
        )
    executable = install.interpreter(version_id, "Server")
    args = [
        str(executable),
        "-m",
        "server.main",
        "--host",
        instance.host,
        "--port",
        str(instance.port),
        "--data-dir",
        str(instance.data_dir),
    ]
    if verification:
        args.append("--verification-only")
    startup_log = install.root / "logs" / "server-startup.log"
    startup_log.parent.mkdir(parents=True, exist_ok=True)
    _rotate_startup_log(startup_log)
    with startup_log.open("ab") as output:
        process = subprocess.Popen(
            args,
            cwd=install.version(version_id) / "app",
            # The server writes its log lines to its daily log; the startup log keeps
            # only output that bypasses logging, such as a crash before logging starts.
            env={**child_environment(install), CONSOLE_LOGGING_ENV_VAR: "0"},
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=subprocess_creation_flags(new_process_group=True, breakaway=breakaway),
            start_new_session=os.name != "nt",
        )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and process.poll() is None:
        if running_server_matches(install, version_id=version_id, verification=verification):
            return CommandResult(
                ok=True,
                message="started",
                instance=instance,
                process_id=process.pid,
                health=probe_health(instance),
                webui=probe_webui(instance) if not verification else None,
            )
        time.sleep(0.25)
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
    return CommandResult(
        ok=False,
        message=(
            f"The vBot server did not become ready; inspect {startup_log} "
            f"and {instance.data_dir / 'logs'}"
        ),
        instance=instance,
    )


def _rotate_startup_log(path: Path) -> None:
    """Move an oversized startup log to its single previous generation."""
    # A missing file needs nothing; a file an earlier server process still holds
    # open cannot move and keeps growing until the next start.
    with suppress(OSError):
        if path.stat().st_size > STARTUP_LOG_ROTATE_BYTES:
            os.replace(path, path.with_name(f"{path.name}.1"))


def stop(install: Installation, *, initiator: str = "cli") -> CommandResult:
    """Stop the installation's server; *initiator* names the caller in its stop line."""
    return stop_server(target(install), initiator=initiator)
