"""Local server process and service lifecycle; target probing lives in _server_target."""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from functools import partial
from typing import Any, Literal

import httpx
import psutil  # type: ignore[import-untyped]

from cli._server_target import (
    DEFAULT_PROBE_TIMEOUT_SECONDS,
    HEALTH_PATH,
    PATIENT_PROBE_TIMEOUT_SECONDS,
    WEBUI_PATH,
    WILDCARD_HOSTS,
    CommandResult,
    HealthProbeResult,
    ServerInstance,
    WebUIProbeResult,
    _probe_url,
    build_server_base_url,
    find_listening_process,
    is_local_target,
    probe_health,
    probe_health_patiently,
    probe_webui,
    resolve_instance,
)
from core.storage.layout import initialize_data_directory
from core.utils.logging import CONSOLE_LOGGING_ENV_VAR, LogManager
from core.utils.processes import outside_service_unit, subprocess_creation_flags
from core.utils.server_control import (
    CONTROL_INITIATOR_HEADER,
    CONTROL_SHUTDOWN_PATH,
    CONTROL_TOKEN_HEADER,
    process_started,
    read_server_control,
)

# Readiness covers a cold Runtime start (imports, databases, Extensions, WebUI
# assets). A loaded machine can exceed 10 s, and a timeout kills the child that
# was about to become ready, so the budget matches the other start paths.
DEFAULT_STARTUP_TIMEOUT_SECONDS = 60.0


DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 5.0


# A busy server may need seconds to accept the shutdown request. Giving up early
# falls back to `terminate`, a hard kill on Windows that skips the graceful teardown.
DEFAULT_CONTROL_REQUEST_TIMEOUT_SECONDS = PATIENT_PROBE_TIMEOUT_SECONDS


# A local listener holds the target port but never answered `/health`, so it is
# neither confirmed as vBot nor known to be foreign.
UNRESPONSIVE_LISTENER_MESSAGE = "port occupied by unresponsive process"

# The control record names a live server process for the target, but the target does
# not answer `/health`: the server is busy, still starting or already stopping.
UNRESPONSIVE_SERVER_MESSAGE = "the server process is running but does not answer its health check"

# A vBot server answers on the target port, but the target data directory's control
# record does not name it: it belongs to another data directory and is left running.
UNRECORDED_SERVER_MESSAGE = "port occupied by a vBot server of another data directory"


PROCESS_CREATE_TIME_TOLERANCE_SECONDS = 0.001


CLI_SERVER_LOGGER_NAME = "cli.server_management"


_SCHEDULED_RESTART_WAIT_TIMEOUT_SECONDS = 60.0


_SCHEDULED_RESTART_SETTLE_SECONDS = 0.5


_RUN_CONTEXT_ENV_PREFIX = "VBOT_RUN_"


def start_server_process(instance: ServerInstance) -> subprocess.Popen[bytes]:
    """Start the foreground server entrypoint as a background subprocess."""

    environment = dict(os.environ)
    environment[CONSOLE_LOGGING_ENV_VAR] = "0"
    args = [
        sys.executable,
        "-m",
        "server.main",
        "--host",
        instance.host,
        "--port",
        str(instance.port),
        "--data-dir",
        str(instance.data_dir),
    ]
    if sys.platform == "win32":
        return _open_server_process(
            args,
            env=environment,
            # Keep the long-lived server independent from the invoking shell and
            # explicitly suppress a console. DETACHED_PROCESS alone can still
            # leave Python with a visible console host in installer launch paths.
            creationflags=subprocess_creation_flags(
                new_process_group=True,
                breakaway=True,
                platform_name="nt",
            ),
        )
    return _open_server_process(args, env=environment, start_new_session=True)


def _open_server_process(
    args: list[str],
    *,
    env: dict[str, str],
    creationflags: int = 0,
    start_new_session: bool = False,
) -> subprocess.Popen[bytes]:
    """Open the server subprocess with typed subprocess arguments."""

    return subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        env=env,
        creationflags=creationflags,
        start_new_session=start_new_session,
    )


def start_server(
    instance: ServerInstance,
    *,
    startup_timeout_seconds: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
    probe_interval_seconds: float = 0.1,
) -> CommandResult:
    """Start the server and wait until vBot health is reachable."""

    manager = _create_cli_log_manager(instance)
    logger = manager.get_logger(CLI_SERVER_LOGGER_NAME)
    try:
        initial_health = probe_health_patiently(instance)
        # Never spawn next to a server: a duplicate cannot claim the target, and the
        # readiness probe would then report the existing server as started.
        state = classify_server(instance, health=initial_health)
        if state == "running":
            logger.debug("CLI-managed background server already running at %s", instance.url)
            return CommandResult(
                ok=True,
                message="already running",
                instance=instance,
                health=initial_health,
                webui=probe_webui(instance),
                log_path=instance.log_path,
            )
        if state != "absent":
            message = _start_refusal_message(state, initial_health)
            logger.warning(
                "Refusing CLI-managed background server start at %s: %s", instance.url, message
            )
            return CommandResult(
                ok=False,
                message=message,
                instance=instance,
                health=initial_health,
                log_path=instance.log_path,
            )

        logger.debug("Starting CLI-managed background server at %s", instance.url)
        process = start_server_process(instance)
        logger.debug("Started CLI-managed background server process %s", process.pid)
        started_at = time.monotonic()
        deadline = started_at + startup_timeout_seconds
        health = initial_health
        result: CommandResult | None = None
        while time.monotonic() < deadline:
            health = probe_health(instance)
            if health.is_vbot:
                logger.info(
                    "CLI-managed background server started (pid=%s url=%s ready=%.1fs)",
                    process.pid,
                    instance.url,
                    time.monotonic() - started_at,
                )
                return CommandResult(
                    ok=True,
                    message="started",
                    instance=instance,
                    health=health,
                    webui=probe_webui(instance),
                    log_path=instance.log_path,
                    process_id=process.pid,
                )
            if health.reachable:
                logger.error(
                    "CLI-managed background server startup hit a non-vBot responder at %s",
                    instance.url,
                )
                result = CommandResult(
                    ok=False,
                    message="port occupied by non-vBot process",
                    instance=instance,
                    health=health,
                    log_path=instance.log_path,
                    process_id=process.pid,
                )
                break
            if process.poll() is not None:
                logger.error(
                    "CLI-managed background server process %s exited before readiness at %s",
                    process.pid,
                    instance.url,
                )
                return CommandResult(
                    ok=False,
                    message="server process exited before readiness",
                    instance=instance,
                    health=health,
                    log_path=instance.log_path,
                    process_id=process.pid,
                )
            time.sleep(probe_interval_seconds)

        if result is None:
            logger.error(
                "CLI-managed background server process %s readiness timed out at %s "
                "after %.1f s; stopping it",
                process.pid,
                instance.url,
                time.monotonic() - started_at,
            )
            result = CommandResult(
                ok=False,
                message="server readiness timed out",
                instance=instance,
                health=health,
                log_path=instance.log_path,
                process_id=process.pid,
            )
        _cleanup_spawned_process(process, timeout_seconds=DEFAULT_PROBE_TIMEOUT_SECONDS)
        return result
    finally:
        manager.close()


def _start_refusal_message(state: ServerState, health: HealthProbeResult) -> str:
    """Name what holds the target when ``start_server`` must not spawn."""

    if state == "unresponsive":
        return UNRESPONSIVE_SERVER_MESSAGE
    if health.is_vbot:
        return UNRECORDED_SERVER_MESSAGE
    if health.reachable:
        return "port occupied by non-vBot process"
    return UNRESPONSIVE_LISTENER_MESSAGE


def _create_cli_log_manager(instance: ServerInstance) -> LogManager:
    """Return a managed CLI log manager for the target data directory.

    CLI lifecycle logs live under ``<data_dir>/logs``. Creating that directory in a
    missing data root would leave a root without the Session store's bootstrap
    marker, which the server then refuses to open, so a missing root is initialized
    through the canonical layout first.
    """

    if not instance.data_dir.exists():
        initialize_data_directory(instance.data_dir)
    return LogManager(data_dir=instance.data_dir, enable_console=False)


def _cleanup_spawned_process(
    process: subprocess.Popen[bytes] | Any,
    *,
    timeout_seconds: float,
) -> bool:
    """Terminate a just-spawned child with bounded kill fallback."""

    if process.poll() is not None:
        return False

    process.terminate()
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        # Preserve the authoritative startup failure even when the child ignores kill.
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=timeout_seconds)
        return True
    return False


def stop_server(
    instance: ServerInstance,
    *,
    shutdown_timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    initiator: str = "cli",
) -> CommandResult:
    """Request Runtime shutdown, with bounded terminate/kill fallback.

    *initiator* names the caller in the server's stop line (``STOP_INITIATORS``).
    A stop that bypasses the server's own shutdown is logged to the target's log.
    """

    if not is_local_target(instance):
        return CommandResult(
            ok=False,
            message="server stop requires a local target",
            instance=instance,
        )
    health = probe_health_patiently(instance)
    if health.unresponsive:
        # A listener that never answers cannot be confirmed as vBot through /health;
        # only the exact process of the control record may be stopped. It still gets
        # the cooperative request first: a merely busy server honors it.
        process = _resolve_control_process(instance)
        if process is None:
            return CommandResult(
                ok=False,
                message=UNRESPONSIVE_LISTENER_MESSAGE,
                instance=instance,
                health=health,
            )
    elif not health.reachable:
        process = _resolve_control_process(instance)
        if process is not None:
            return _await_process_exit(
                instance,
                health,
                process,
                shutdown_timeout_seconds=shutdown_timeout_seconds,
            )
        return CommandResult(ok=True, message="not running", instance=instance, health=health)
    elif not health.is_vbot:
        return CommandResult(
            ok=False,
            message="port occupied by non-vBot process",
            instance=instance,
            health=health,
        )
    else:
        process = find_listening_process(instance)
        if process is None:
            return CommandResult(
                ok=False,
                message="vBot process not found",
                instance=instance,
                health=health,
            )
        # Any vBot answers /health; only the server this data directory started may
        # be stopped, so a wrong data directory never stops another installation.
        recorded = _resolve_control_process(instance)
        if recorded is None or recorded.pid != process.pid:
            return CommandResult(
                ok=False,
                message=UNRECORDED_SERVER_MESSAGE,
                instance=instance,
                health=health,
            )

    if not _request_cooperative_shutdown(instance, process, initiator=initiator):
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            return CommandResult(
                ok=True, message="stopped", instance=instance, health=health, process_id=process.pid
            )
        _log_forced_stop(
            instance,
            logging.WARNING,
            "Server process terminated after its shutdown request failed (pid=%s)",
            process.pid,
        )
    return _await_process_exit(
        instance, health, process, shutdown_timeout_seconds=shutdown_timeout_seconds
    )


def _resolve_control_process(instance: ServerInstance) -> psutil.Process | None:
    """Resolve the exact process that authored this instance's control record."""

    control = read_server_control(instance.data_dir, instance.port)
    if control is None:
        return None
    try:
        process = psutil.Process(control.pid)
        if (
            abs(process_started(process) - control.process_create_time)
            > PROCESS_CREATE_TIME_TOLERANCE_SECONDS
        ):
            return None
    except (psutil.Error, OSError):
        return None
    return process


#: How a local target's server stands before a lifecycle decision (``classify_server``).
ServerState = Literal["absent", "running", "unresponsive", "foreign"]


def classify_server(
    instance: ServerInstance,
    *,
    health: HealthProbeResult | None = None,
    is_expected: Callable[[psutil.Process], bool] | None = None,
) -> ServerState:
    """Classify a local target's server, keeping identity, liveness and health apart.

    Identity and liveness come from the target's control record: the exact process
    (PID plus creation time) that published it, which *is_expected* may narrow, for
    example to one installed version. Health comes from ``probe_health_patiently``
    unless the caller passes an observation it already made, so a busy server is
    never taken for a stopped one.

    ``running``: the recorded process lives and the target answers as vBot.
    ``unresponsive``: it lives but the target does not answer (busy Event Loop, still
    starting, already stopping; the record exists before the listener). ``foreign``:
    something else holds or answers the target (a process *is_expected* rejects or
    that cannot be inspected, a vBot server of another data directory, a non-vBot
    or unidentified listener). ``absent``: no live recorded process and no listener.
    """

    if health is None:
        health = probe_health_patiently(instance)
    process = _resolve_control_process(instance)
    recorded: Literal["expected", "other"] | None = None
    if process is not None:
        try:
            recorded = "expected" if is_expected is None or is_expected(process) else "other"
        except psutil.NoSuchProcess:
            recorded = None
        except (psutil.Error, OSError):
            # A live process that cannot be inspected is never taken for the server.
            recorded = "other"
    if recorded == "other" or (health.reachable and not health.is_vbot):
        return "foreign"
    if recorded == "expected":
        return "running" if health.is_vbot else "unresponsive"
    # Without a live recorded process any listener belongs to someone else.
    return "foreign" if health.reachable or health.unresponsive else "absent"


def _await_process_exit(
    instance: ServerInstance,
    health: HealthProbeResult,
    process: psutil.Process | Any,
    *,
    shutdown_timeout_seconds: float,
) -> CommandResult:
    """Wait for the exact server process to finish teardown, killing it after the timeout."""

    forced = False
    try:
        process.wait(timeout=shutdown_timeout_seconds)
    except psutil.TimeoutExpired:
        forced = True
        _log_forced_stop(
            instance,
            logging.WARNING,
            "Server process killed after stop timeout (pid=%s timeout=%.0fs)",
            process.pid,
            shutdown_timeout_seconds,
        )
        try:
            process.kill()
            process.wait(timeout=shutdown_timeout_seconds)
        except psutil.TimeoutExpired:
            _log_forced_stop(
                instance,
                logging.ERROR,
                "Server process did not exit after kill (pid=%s)",
                process.pid,
            )
            return CommandResult(
                ok=False,
                message="forced termination timed out",
                instance=instance,
                health=health,
                process_id=process.pid,
                forced=True,
            )
        except psutil.NoSuchProcess:
            pass
    except psutil.NoSuchProcess:
        pass

    return CommandResult(
        ok=True,
        message="stopped",
        instance=instance,
        health=health,
        process_id=process.pid,
        forced=forced,
    )


def _log_forced_stop(instance: ServerInstance, level: int, message: str, *args: object) -> None:
    """Record a stop that bypassed the server's own shutdown in the target's log."""

    if not instance.data_dir.is_dir():
        # A stop must not initialize a data directory as a side effect.
        return
    manager = _create_cli_log_manager(instance)
    try:
        manager.get_logger(CLI_SERVER_LOGGER_NAME).log(level, message, *args)
    finally:
        manager.close()


def _request_cooperative_shutdown(
    instance: ServerInstance,
    process: psutil.Process | Any,
    *,
    initiator: str = "cli",
    timeout_seconds: float = DEFAULT_CONTROL_REQUEST_TIMEOUT_SECONDS,
) -> bool:
    """Ask the exact listener process to enter its application shutdown path."""

    control = read_server_control(instance.data_dir, instance.port)
    if control is None or control.pid != process.pid:
        return False
    try:
        response = httpx.post(
            _probe_url(instance, CONTROL_SHUTDOWN_PATH),
            headers={CONTROL_TOKEN_HEADER: control.token, CONTROL_INITIATOR_HEADER: initiator},
            timeout=timeout_seconds,
            trust_env=False,
        )
    except httpx.RequestError:
        return False
    return response.status_code == httpx.codes.ACCEPTED


def restart_server(
    instance: ServerInstance,
    *,
    stop: Callable[[ServerInstance], CommandResult] = stop_server,
    start: Callable[[ServerInstance], CommandResult] = start_server,
) -> CommandResult:
    """Restart the local server: terminate it, then start it again."""

    stop_result = stop(instance)
    if not stop_result.ok and stop_result.message != "not running":
        return CommandResult(
            ok=False,
            message=f"restart aborted: {stop_result.message}",
            instance=instance,
            health=stop_result.health,
        )
    start_result = start(instance)
    if start_result.ok:
        return CommandResult(
            ok=True,
            message="restarted",
            instance=instance,
            health=start_result.health,
            webui=start_result.webui,
            log_path=start_result.log_path,
            process_id=start_result.process_id,
        )
    return CommandResult(
        ok=False,
        message=f"restart failed: {start_result.message}",
        instance=instance,
        health=start_result.health,
        log_path=start_result.log_path,
    )


def has_vbot_run_context(environment: Mapping[str, str] | None = None) -> bool:
    """Return whether Bash supplied the exact local Agent Run identity fields."""

    values = os.environ if environment is None else environment
    return bool(values.get("VBOT_RUN_AGENT_ID") and values.get("VBOT_RUN_SESSION_ID"))


def schedule_server_restart(
    instance: ServerInstance,
    *,
    wait_pid: int | None = None,
) -> CommandResult:
    """Detach one private restart attempt from the current server-owned process tree."""

    parent_pid = _restart_handoff_wait_pid(instance) if wait_pid is None else wait_pid
    # The helper outlives the server, so it also leaves the server's systemd unit.
    arguments = outside_service_unit(
        [
            sys.executable,
            "-m",
            "cli.server_management",
            "--scheduled-restart",
            "--wait-pid",
            str(parent_pid),
            "--host",
            instance.host,
            "--port",
            str(instance.port),
            "--data-dir",
            str(instance.data_dir),
        ]
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(_RUN_CONTEXT_ENV_PREFIX)
    }
    try:
        process = _open_scheduled_restart_process(arguments, environment)
    except OSError as exc:
        return CommandResult(
            ok=False,
            message=f"could not schedule server restart: {exc}",
            instance=instance,
        )
    return CommandResult(
        ok=True,
        message=f"restart scheduled by helper process {process.pid}",
        instance=instance,
    )


def _restart_handoff_wait_pid(instance: ServerInstance) -> int:
    """Return the outer server-owned launcher so Tool output can finish first."""

    control = read_server_control(instance.data_dir, instance.port)
    server_pid = control.pid if control is not None else None
    try:
        descendant = psutil.Process()
        parent = descendant.parent()
        while parent is not None:
            if parent.pid == server_pid:
                return int(descendant.pid)
            descendant = parent
            parent = descendant.parent()
    except (psutil.Error, OSError):
        pass
    return os.getppid()


def _open_scheduled_restart_process(
    arguments: list[str], environment: dict[str, str]
) -> subprocess.Popen[bytes]:
    """Spawn the sole process allowed to leave the old server's containment boundary."""

    creationflags = subprocess_creation_flags(new_process_group=True, breakaway=True)
    return subprocess.Popen(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        env=environment,
        creationflags=creationflags,
        start_new_session=os.name != "nt",
    )


def _run_scheduled_restart(argv: list[str]) -> int:
    """Private detached entrypoint used only by ``schedule_server_restart``."""

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--scheduled-restart", action="store_true", required=True)
    parser.add_argument("--wait-pid", type=int, required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--data-dir", required=True)
    arguments = parser.parse_args(argv)
    instance = resolve_instance(
        host=arguments.host,
        port=arguments.port,
        data_dir=arguments.data_dir,
    )
    manager = _create_cli_log_manager(instance)
    logger = manager.get_logger(CLI_SERVER_LOGGER_NAME)
    try:
        if arguments.wait_pid <= 0 or arguments.wait_pid == os.getpid():
            logger.error("Scheduled restart rejected invalid wait PID %s", arguments.wait_pid)
            return 1
        try:
            caller = psutil.Process(arguments.wait_pid)
            caller.wait(timeout=_SCHEDULED_RESTART_WAIT_TIMEOUT_SECONDS)
        except psutil.NoSuchProcess:
            pass
        except psutil.TimeoutExpired:
            logger.error(
                "Scheduled restart abandoned because update process %s did not exit in time",
                arguments.wait_pid,
            )
            return 1
        time.sleep(_SCHEDULED_RESTART_SETTLE_SECONDS)
        result = _restart_target(instance)
        log = logger.info if result.ok else logger.error
        log("Scheduled update restart result: %s", result.message)
        return 0 if result.ok else 1
    finally:
        manager.close()


def _restart_target(instance: ServerInstance) -> CommandResult:
    """Restart *instance* the way its owner starts it.

    The server of a packaged installation restarts through the installation, so
    that a systemd user unit that ran it runs it again.
    """
    from cli.application import processes
    from cli.application.state import ApplicationError, exclusive

    install = processes.owning_installation(instance)
    if install is None:
        return restart_server(instance, stop=partial(stop_server, initiator="scheduled_restart"))
    try:
        with exclusive(install.root):
            stopped = processes.stop(install, initiator="scheduled_restart")
            if not stopped.ok:
                return stopped
            return processes.start(install)
    except ApplicationError as exc:
        return CommandResult(ok=False, message=str(exc), instance=instance)


def get_status(instance: ServerInstance) -> CommandResult:
    """Return current vBot/API and WebUI status for the instance."""

    health = probe_health_patiently(instance)
    if health.is_vbot:
        return CommandResult(
            ok=True,
            message="running",
            instance=instance,
            health=health,
            webui=probe_webui(instance),
            log_path=instance.log_path,
        )
    if health.reachable:
        return CommandResult(
            ok=False,
            message="port occupied by non-vBot process",
            instance=instance,
            health=health,
            webui=WebUIProbeResult(available=False),
            log_path=instance.log_path,
        )
    if health.unresponsive:
        return CommandResult(
            ok=False,
            message=UNRESPONSIVE_LISTENER_MESSAGE,
            instance=instance,
            health=health,
            log_path=instance.log_path,
        )
    return CommandResult(
        ok=True,
        message="not running",
        instance=instance,
        health=health,
        webui=WebUIProbeResult(available=False),
        log_path=instance.log_path,
    )


if __name__ == "__main__":
    raise SystemExit(_run_scheduled_restart(sys.argv[1:]))


__all__ = [
    "CommandResult",
    "DEFAULT_PROBE_TIMEOUT_SECONDS",
    "HEALTH_PATH",
    "HealthProbeResult",
    "ServerInstance",
    "WEBUI_PATH",
    "WebUIProbeResult",
    "build_server_base_url",
    "probe_health",
    "probe_health_patiently",
    "probe_webui",
    "resolve_instance",
    "UNRECORDED_SERVER_MESSAGE",
    "UNRESPONSIVE_LISTENER_MESSAGE",
    "UNRESPONSIVE_SERVER_MESSAGE",
    "DEFAULT_STARTUP_TIMEOUT_SECONDS",
    "DEFAULT_SHUTDOWN_TIMEOUT_SECONDS",
    "DEFAULT_CONTROL_REQUEST_TIMEOUT_SECONDS",
    "PROCESS_CREATE_TIME_TOLERANCE_SECONDS",
    "WILDCARD_HOSTS",
    "CLI_SERVER_LOGGER_NAME",
    "start_server_process",
    "start_server",
    "find_listening_process",
    "ServerState",
    "classify_server",
    "stop_server",
    "restart_server",
    "has_vbot_run_context",
    "schedule_server_restart",
    "get_status",
]
