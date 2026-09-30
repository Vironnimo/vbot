"""Run exact installed versions through the existing process lifecycle boundary."""

from __future__ import annotations

import os
import subprocess
import time
from contextlib import suppress
from pathlib import Path
from typing import Literal

import psutil  # type: ignore[import-untyped]

from cli.application.operations import child_environment
from cli.application.state import ApplicationError, Installation
from cli.server_management import (
    CommandResult,
    HealthProbeResult,
    ServerInstance,
    probe_health,
    probe_health_patiently,
    probe_webui,
    resolve_instance,
    stop_server,
)
from core.utils.logging import CONSOLE_LOGGING_ENV_VAR
from core.utils.processes import subprocess_creation_flags
from core.utils.server_control import read_server_control

# The startup log keeps one previous generation once it outgrows this bound.
STARTUP_LOG_ROTATE_BYTES = 1024 * 1024

#: How the installation's server target relates to one exact version and startup mode.
#:
#: ``running``: the control record's live process is that exact server and answers
#: ``/health``. ``unresponsive``: that exact process is alive but does not answer: busy,
#: still starting or already stopping. ``foreign``: something else holds or answers the
#: target, such as another version or startup mode, a process that cannot be inspected, a
#: vBot server of another data directory or an unidentified listener. ``absent``: no live
#: recorded process and no listener.
ServerState = Literal["absent", "running", "unresponsive", "foreign"]

OCCUPIED_MESSAGE = "Server port is occupied by another application version or startup mode"
UNRESPONSIVE_MESSAGE = "the server process is running but does not answer its health check"


def target(install: Installation) -> ServerInstance:
    if not install.owns_server:
        raise ApplicationError("This Desktop Client installation has no local server")
    assert install.server_host is not None and install.server_port is not None
    assert install.server_data_directory is not None
    return resolve_instance(
        host=install.server_host, port=install.server_port, data_dir=install.server_data_directory
    )


def server_state(
    install: Installation,
    *,
    version_id: str | None = None,
    verification: bool = False,
) -> ServerState:
    """Classify the server target for the exact installed version and startup mode.

    Identity and liveness come from the control record (PID, creation time, exact
    native executable and startup mode), health from the patient probe, so a busy
    server is never mistaken for a stopped one.
    """
    instance = target(install)
    health = probe_health_patiently(instance)
    return _classify(install, instance, health, version_id=version_id, verification=verification)


def _classify(
    install: Installation,
    instance: ServerInstance,
    health: HealthProbeResult,
    *,
    version_id: str | None,
    verification: bool,
) -> ServerState:
    recorded = _recorded_process(install, instance, version_id, verification)
    if recorded == "other" or (health.reachable and not health.is_vbot):
        return "foreign"
    if recorded == "exact":
        return "running" if health.is_vbot else "unresponsive"
    # Without a live recorded process any listener belongs to someone else.
    return "foreign" if health.reachable or health.unresponsive else "absent"


def _recorded_process(
    install: Installation,
    instance: ServerInstance,
    version_id: str | None,
    verification: bool,
) -> Literal["exact", "other"] | None:
    """Whether the control record's live process is the exact server; ``None`` if none lives."""
    record = read_server_control(instance.data_dir, instance.port)
    if record is None:
        return None
    try:
        process = psutil.Process(record.pid)
        if abs(process.create_time() - record.process_create_time) >= 0.001:
            return None
    except (OSError, psutil.Error):
        # The recorded process exited, or its identity cannot be confirmed.
        return None
    try:
        expected = install.interpreter(version_id, "Server").resolve()
        actual = Path(process.exe()).resolve()
        command = process.cmdline()
    except psutil.NoSuchProcess:
        return None
    except (OSError, psutil.Error, ApplicationError):
        # A live process that cannot be inspected is never taken for this server.
        return "other"
    if os.path.normcase(str(actual)) != os.path.normcase(str(expected)):
        return "other"
    return "exact" if ("--verification-only" in command) is verification else "other"


def start(
    install: Installation,
    *,
    version_id: str | None = None,
    verification: bool = False,
    timeout: float = 90,
    breakaway: bool = True,
) -> CommandResult:
    instance = target(install)
    current = probe_health_patiently(instance)
    state = _classify(install, instance, current, version_id=version_id, verification=verification)
    if state != "absent":
        # Never spawn next to a server: a duplicate cannot claim the target, and the
        # readiness check would then report the existing server as started.
        ready = state == "running" and not verification
        return CommandResult(
            ok=ready,
            message=(
                "already running"
                if ready
                else UNRESPONSIVE_MESSAGE
                if state == "unresponsive"
                else OCCUPIED_MESSAGE
            ),
            instance=instance,
            health=current,
            webui=probe_webui(instance) if ready else None,
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
        health = probe_health(instance)
        if health.is_vbot and (
            _classify(install, instance, health, version_id=version_id, verification=verification)
            == "running"
        ):
            return CommandResult(
                ok=True,
                message="started",
                instance=instance,
                process_id=process.pid,
                health=health,
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
