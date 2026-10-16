"""Run exact installed versions through the existing process lifecycle boundary."""

from __future__ import annotations

import os
import subprocess
import time
from contextlib import suppress
from pathlib import Path

import psutil  # type: ignore[import-untyped]

from cli.application import operations
from cli.application.autostart import UNIT_NAME, owned_unit, run_command
from cli.application.operations import child_environment
from cli.application.state import ApplicationError, Installation, discover, exclusive
from cli.server_management import (
    UNRESPONSIVE_SERVER_MESSAGE,
    CommandResult,
    HealthProbeResult,
    ServerInstance,
    ServerState,
    classify_server,
    confined_server_warning,
    probe_health,
    probe_health_patiently,
    probe_webui,
    resolve_instance,
    stop_server,
)
from core.utils.logging import CONSOLE_LOGGING_ENV_VAR
from core.utils.outside_job import Confinement, start_outside_foreign_job
from core.utils.processes import subprocess_creation_flags

# The startup log keeps one previous generation once it outgrows this bound.
STARTUP_LOG_ROTATE_BYTES = 1024 * 1024

OCCUPIED_MESSAGE = "Server port is occupied by another application version or startup mode"

# Where the user starts a server that outlives the application whose Job holds the caller.
_OUTSIDE_JOB_REMEDY = (
    "To keep it running, start vBot from the Start menu and choose Restart server in its tray menu."
)


def target(install: Installation) -> ServerInstance:
    if not install.owns_server:
        raise ApplicationError("This Desktop Client installation has no local server")
    assert install.server_host is not None and install.server_port is not None
    assert install.server_data_directory is not None
    return resolve_instance(
        host=install.server_host, port=install.server_port, data_dir=install.server_data_directory
    )


def owning_installation(instance: ServerInstance) -> Installation | None:
    """The packaged installation whose recorded server *instance* is, if any.

    Such a server starts and stops through its installation, never as a plain
    process: a systemd user unit may run it.
    """
    install = discover()
    if install is None or not install.owns_server:
        return None
    owned = target(install)
    if owned.port != instance.port or owned.data_dir.resolve() != instance.data_dir.resolve():
        return None
    return install


def server_state(
    install: Installation,
    *,
    version_id: str | None = None,
    verification: bool = False,
) -> ServerState:
    """Classify the installation's server for the exact installed version and startup mode.

    ``classify_server`` with the recorded process narrowed to that version's native
    executable and startup mode: any other version or mode is ``foreign``.
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
    def is_exact(process: psutil.Process) -> bool:
        try:
            expected = install.interpreter(version_id, "Server").resolve()
        except ApplicationError:
            return False
        actual = Path(process.exe()).resolve()
        if os.path.normcase(str(actual)) != os.path.normcase(str(expected)):
            return False
        return ("--verification-only" in process.cmdline()) is verification

    return classify_server(instance, health=health, is_expected=is_exact)


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
                else UNRESPONSIVE_SERVER_MESSAGE
                if state == "unresponsive"
                else OCCUPIED_MESSAGE
            ),
            instance=instance,
            health=current,
            webui=probe_webui(instance) if ready else None,
        )
    if not verification and version_id in {None, install.version().name} and owned_unit(install):
        return _start_unit(install, instance, timeout)
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
    app = install.version(version_id) / "app"
    # The server writes its log lines to its daily log; the startup log keeps
    # only output that bypasses logging, such as a crash before logging starts.
    environment = {**child_environment(install), CONSOLE_LOGGING_ENV_VAR: "0"}
    startup_log = install.root / "logs" / "server-startup.log"
    startup_log.parent.mkdir(parents=True, exist_ok=True)
    _rotate_startup_log(startup_log)

    def spawn() -> subprocess.Popen[bytes]:
        with startup_log.open("ab") as output:
            return subprocess.Popen(
                args,
                cwd=app,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                creationflags=subprocess_creation_flags(
                    new_process_group=True, breakaway=breakaway
                ),
                start_new_session=os.name != "nt",
            )

    confinement: Confinement | None = None
    if breakaway:
        # A server started from another application's Job must outlive it; one
        # started through WMI has no startup log output.
        process, confinement = start_outside_foreign_job(
            args, cwd=app, env=environment, spawn=spawn
        )
    else:
        process = spawn()
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
                attention=(
                    (confined_server_warning(confinement, remedy=_OUTSIDE_JOB_REMEDY),)
                    if confinement is not None
                    else ()
                ),
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


def _start_unit(install: Installation, instance: ServerInstance, timeout: float) -> CommandResult:
    """Start the active version through its systemd user unit and await its readiness."""
    started = run_command(["systemctl", "--user", "start", UNIT_NAME])
    if started.returncode != 0:
        return CommandResult(
            ok=False,
            message=f"systemctl --user start {UNIT_NAME} failed: "
            f"{started.stderr or started.stdout}",
            instance=instance,
        )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        health = probe_health(instance)
        if health.is_vbot and (
            _classify(install, instance, health, version_id=None, verification=False) == "running"
        ):
            return CommandResult(
                ok=True,
                message="started via systemd",
                instance=instance,
                health=health,
                webui=probe_webui(instance),
            )
        time.sleep(0.25)
    # A server that never became ready must not keep restarting in the background.
    run_command(["systemctl", "--user", "stop", UNIT_NAME])
    return CommandResult(
        ok=False,
        message=(
            "The vBot server did not become ready; inspect "
            f"`journalctl --user -u {UNIT_NAME}` and {instance.data_dir / 'logs'}"
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
    """Stop the installation's server; *initiator* names the caller in its stop line.

    The server is asked to shut down cooperatively. With a systemd user unit, the
    unit is stopped as well, which cancels an automatic restart it may schedule.
    """
    result = stop_server(target(install), initiator=initiator)
    if owned_unit(install):
        settled = run_command(["systemctl", "--user", "stop", UNIT_NAME])
        if settled.returncode != 0 and result.ok:
            return CommandResult(
                ok=False,
                message=f"systemctl --user stop {UNIT_NAME} failed: "
                f"{settled.stderr or settled.stdout}",
                instance=result.instance,
            )
    return result


def start_or_restart(install: Installation, command: str) -> CommandResult:
    """Run ``vbot server start`` or ``restart`` (``command``) for a CLI caller.

    A caller that another application's Windows Job confines (a coding agent's
    shell, say) hands the command to the running tray, which runs outside that
    Job (``operations.request_server``), so the server outlives that
    application. Without a tray that takes it, the command runs here, where
    ``start`` leaves such a Job through WMI. Raises ``ApplicationError`` while
    another application operation runs.
    """

    if operations.confining_job() is not None and operations.host_running(install):
        answer = operations.request_server(install, command)
        if answer is not None:
            instance = target(install)
            ok = answer.get("ok") is True
            process_id = answer.get("process_id")
            return CommandResult(
                ok=ok,
                message=str(answer.get("message") or ("started" if ok else "failed")),
                instance=instance,
                health=probe_health(instance),
                webui=probe_webui(instance) if ok else None,
                process_id=process_id if type(process_id) is int else None,
            )
    with exclusive(install.root):
        if command == "restart":
            stopped = stop(install)
            if not stopped.ok:
                return stopped
        return start(install)
