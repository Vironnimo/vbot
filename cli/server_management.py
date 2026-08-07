"""Server lifecycle primitives for the vBot CLI."""

from __future__ import annotations

import argparse
import locale
import os
import re
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import psutil  # type: ignore[import-untyped]

from core.tools.process_manager import subprocess_creation_flags
from core.utils.config import DEFAULT_HOST, Config, resolve_port
from core.utils.logging import CONSOLE_LOGGING_ENV_VAR, LogManager, resolve_daily_log_path
from core.utils.server_control import (
    CONTROL_SHUTDOWN_PATH,
    CONTROL_TOKEN_HEADER,
    read_server_control,
)

DEFAULT_STARTUP_TIMEOUT_SECONDS = 10.0
DEFAULT_PROBE_TIMEOUT_SECONDS = 0.5
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 5.0
DEFAULT_CONTROL_REQUEST_TIMEOUT_SECONDS = 1.0
HEALTH_PATH = "/health"
WEBUI_PATH = "/"
WILDCARD_HOSTS = {"", "*", "0.0.0.0", "::"}
CLI_SERVER_LOGGER_NAME = "cli.server_management"
DEFAULT_SERVICE_NAME = "vbot"
_SYSTEMD_SERVICE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]*$")
_SYSTEMD_SERVICE_NAME_MAX_LENGTH = 200
# A bare systemctl probe (is-active) returns immediately, but `restart` blocks on
# the unit's stop+start: the stop alone can take the unit's whole TimeoutStopSec
# (10s in the unit we install) before SIGKILL, plus the fresh start. The restart
# cap must stay comfortably above that sum, or a slow-but-successful restart trips
# the subprocess timeout and gets misreported as a failure. Keep it above the
# unit's TimeoutStopSec if that value is ever raised.
_SYSTEMCTL_PROBE_TIMEOUT_SECONDS = 10.0
_SYSTEMCTL_RESTART_TIMEOUT_SECONDS = 30.0
# Conventional "command timed out" exit code, kept distinct from 127 (not found).
_SYSTEMCTL_TIMEOUT_RETURN_CODE = 124
_SYSTEMD_RESTART_READY_TIMEOUT_SECONDS = 10.0
_SYSTEMD_RESTART_PROBE_INTERVAL_SECONDS = 0.2
_SCHEDULED_RESTART_WAIT_TIMEOUT_SECONDS = 60.0
_SCHEDULED_RESTART_SETTLE_SECONDS = 0.5
_RUN_CONTEXT_ENV_PREFIX = "VBOT_RUN_"


def is_valid_systemd_service_name(service_name: str) -> bool:
    """Return whether a user-supplied basename is safe as one systemd unit file."""

    return (
        bool(service_name)
        and len(service_name) <= _SYSTEMD_SERVICE_NAME_MAX_LENGTH
        and not service_name.endswith(".service")
        and _SYSTEMD_SERVICE_NAME_PATTERN.fullmatch(service_name) is not None
    )


def decode_command_output(output: bytes | str | None) -> str:
    """Decode captured local-command output without locale-dependent loss.

    Git and Node commonly emit UTF-8 even on legacy Windows code pages, while
    native tools such as ``schtasks`` may use the process locale. Prefer UTF-8,
    then fall back to the locale with escaped undecodable bytes so lifecycle
    decisions never receive ``None`` merely because output decoding failed.
    """

    if output is None:
        return ""
    if isinstance(output, str):
        return output
    try:
        return output.decode("utf-8-sig")
    except UnicodeDecodeError:
        return output.decode(locale.getpreferredencoding(False), errors="backslashreplace")


@dataclass(frozen=True)
class ServerInstance:
    """Resolved local server instance configuration."""

    host: str
    port: int
    data_dir: Path
    url: str
    log_path: Path


@dataclass(frozen=True)
class HealthProbeResult:
    """Result of probing a target server's vBot health endpoint."""

    reachable: bool
    is_vbot: bool
    status_code: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class WebUIProbeResult:
    """Result of probing whether the WebUI is available from the server."""

    available: bool
    status_code: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class CommandResult:
    """Automation-safe outcome returned by lifecycle commands."""

    ok: bool
    message: str
    instance: ServerInstance
    health: HealthProbeResult | None = None
    webui: WebUIProbeResult | None = None
    log_path: Path | None = None
    process_id: int | None = None
    forced: bool = False


def resolve_instance(
    *,
    host: str = DEFAULT_HOST,
    port: int | None = None,
    data_dir: str | Path | None = None,
) -> ServerInstance:
    """Resolve a CLI target using the same port rules as the server."""

    config = Config(data_dir=Path(data_dir) if data_dir is not None else None)
    resolved_data_dir = config.data_dir.expanduser().resolve()
    resolved_port = resolve_port(config, port)
    return ServerInstance(
        host=host,
        port=resolved_port,
        data_dir=resolved_data_dir,
        url=f"http://{host}:{resolved_port}",
        log_path=resolve_daily_log_path(resolved_data_dir),
    )


def probe_health(
    instance: ServerInstance,
    *,
    timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
) -> HealthProbeResult:
    """Probe `/health` and classify only the exact vBot health response as vBot."""

    try:
        response = httpx.get(
            _probe_url(instance, HEALTH_PATH),
            timeout=timeout_seconds,
            trust_env=False,
        )
    except httpx.RequestError as exc:
        return HealthProbeResult(reachable=False, is_vbot=False, error=exc.__class__.__name__)

    if response.status_code != httpx.codes.OK:
        return HealthProbeResult(reachable=True, is_vbot=False, status_code=response.status_code)

    try:
        payload = response.json()
    except ValueError:
        return HealthProbeResult(reachable=True, is_vbot=False, status_code=response.status_code)

    return HealthProbeResult(
        reachable=True,
        is_vbot=payload == {"status": "ok"},
        status_code=response.status_code,
    )


def probe_webui(
    instance: ServerInstance,
    *,
    timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
) -> WebUIProbeResult:
    """Probe `/` separately from API health to classify WebUI availability."""

    try:
        response = httpx.get(
            _probe_url(instance, WEBUI_PATH),
            timeout=timeout_seconds,
            trust_env=False,
        )
    except httpx.RequestError as exc:
        return WebUIProbeResult(available=False, error=exc.__class__.__name__)

    return WebUIProbeResult(
        available=200 <= response.status_code < 400,
        status_code=response.status_code,
    )


def _probe_url(instance: ServerInstance, path: str) -> str:
    """Return a direct local probe URL for health and WebUI checks."""

    probe_host = instance.host
    if probe_host in {"", "*", "0.0.0.0"}:
        probe_host = "127.0.0.1"
    elif probe_host == "::":
        probe_host = "::1"

    if ":" in probe_host and not probe_host.startswith("["):
        probe_host = f"[{probe_host}]"

    return f"http://{probe_host}:{instance.port}{path}"


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
            creationflags=subprocess_creation_flags(new_process_group=True, breakaway=True),
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
        initial_health = probe_health(instance)
        if initial_health.is_vbot:
            logger.info("CLI-managed background server already running at %s", instance.url)
            return CommandResult(
                ok=True,
                message="already running",
                instance=instance,
                health=initial_health,
                webui=probe_webui(instance),
                log_path=instance.log_path,
            )
        if initial_health.reachable:
            logger.warning(
                "Refusing CLI-managed background server start because %s is occupied by"
                " a non-vBot process",
                instance.url,
            )
            return CommandResult(
                ok=False,
                message="port occupied by non-vBot process",
                instance=instance,
                health=initial_health,
                log_path=instance.log_path,
            )

        logger.info("Starting CLI-managed background server at %s", instance.url)
        process = start_server_process(instance)
        logger.info("Started CLI-managed background server process %s", process.pid)
        deadline = time.monotonic() + startup_timeout_seconds
        health = initial_health
        result: CommandResult | None = None
        while time.monotonic() < deadline:
            health = probe_health(instance)
            if health.is_vbot:
                logger.info("CLI-managed background server became ready at %s", instance.url)
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
            logger.error("CLI-managed background server readiness timed out at %s", instance.url)
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


def _create_cli_log_manager(instance: ServerInstance) -> LogManager:
    """Return a managed CLI log manager for the target data directory."""

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


def find_listening_process(instance: ServerInstance) -> psutil.Process | None:
    """Find the local process listening on the resolved TCP host and port."""

    for process in psutil.process_iter():
        try:
            connections = process.net_connections(kind="tcp")
        except psutil.Error:
            continue
        for connection in connections:
            if connection.status != psutil.CONN_LISTEN:
                continue
            if _connection_matches_instance(connection, instance):
                return process
    return None


def _connection_matches_instance(connection: object, instance: ServerInstance) -> bool:
    """Return whether a listening socket can receive the probed target traffic."""

    local_address = getattr(connection, "laddr", None)
    if local_address is None or getattr(local_address, "port", None) != instance.port:
        return False

    local_ip = _connection_local_ip(local_address)
    if local_ip in WILDCARD_HOSTS:
        return _wildcard_can_receive_target(local_ip, instance.host)

    return local_ip in _host_addresses(instance.host)


def _wildcard_can_receive_target(local_ip: str, host: str) -> bool:
    """Return whether a wildcard listener covers the resolved target host."""

    if local_ip in {"", "*"}:
        return True
    target_addresses = _host_addresses(host)
    if local_ip == "0.0.0.0":
        return any("." in address for address in target_addresses)
    if local_ip == "::":
        return any(":" in address for address in target_addresses)
    return False


def _host_addresses(host: str) -> set[str]:
    """Resolve a host to concrete addresses for psutil listener matching."""

    if host in WILDCARD_HOSTS:
        return {host}
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return {host}
    addresses = {str(info[4][0]) for info in infos}
    addresses.add(host)
    return addresses


def _connection_local_ip(local_address: object) -> str:
    """Extract the local IP from psutil address tuple or namedtuple values."""

    ip = getattr(local_address, "ip", None)
    if ip is not None:
        return str(ip)
    try:
        return str(local_address[0])  # type: ignore[index]
    except (IndexError, TypeError):
        return ""


def stop_server(
    instance: ServerInstance,
    *,
    shutdown_timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
) -> CommandResult:
    """Request Runtime shutdown, with bounded terminate/kill fallback."""

    health = probe_health(instance)
    if not health.reachable:
        return CommandResult(ok=True, message="not running", instance=instance, health=health)
    if not health.is_vbot:
        return CommandResult(
            ok=False,
            message="port occupied by non-vBot process",
            instance=instance,
            health=health,
        )

    process = find_listening_process(instance)
    if process is None:
        return CommandResult(
            ok=False,
            message="vBot process not found",
            instance=instance,
            health=health,
        )

    cooperative = _request_cooperative_shutdown(instance, process)
    forced = False
    try:
        if not cooperative:
            process.terminate()
        process.wait(timeout=shutdown_timeout_seconds)
    except psutil.TimeoutExpired:
        forced = True
        try:
            process.kill()
            process.wait(timeout=shutdown_timeout_seconds)
        except psutil.TimeoutExpired:
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


def _request_cooperative_shutdown(
    instance: ServerInstance,
    process: psutil.Process | Any,
    *,
    timeout_seconds: float = DEFAULT_CONTROL_REQUEST_TIMEOUT_SECONDS,
) -> bool:
    """Ask the exact listener process to enter its application shutdown path."""

    control = read_server_control(instance.data_dir, instance.port)
    if control is None or control.pid != process.pid:
        return False
    try:
        response = httpx.post(
            _probe_url(instance, CONTROL_SHUTDOWN_PATH),
            headers={CONTROL_TOKEN_HEADER: control.token},
            timeout=timeout_seconds,
            trust_env=False,
        )
    except httpx.RequestError:
        return False
    return response.status_code == httpx.codes.ACCEPTED


@dataclass(frozen=True)
class _SystemctlRun:
    """Minimal result of a systemctl invocation."""

    returncode: int
    stdout: str
    stderr: str


SystemctlRunner = Callable[[list[str]], _SystemctlRun]


def _run_systemctl(args: list[str]) -> _SystemctlRun:
    """Run a systemctl command, mapping a missing binary or a timeout to a clean failure.

    A `restart` waits out the unit's stop+start, so it gets a longer cap than the
    instant probes; a timeout carries its own message so a slow-but-eventually-fine
    restart is never misreported as "systemctl unavailable".
    """

    timeout = (
        _SYSTEMCTL_RESTART_TIMEOUT_SECONDS
        if "restart" in args or "stop" in args
        else _SYSTEMCTL_PROBE_TIMEOUT_SECONDS
    )
    try:
        completed = subprocess.run(args, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return _SystemctlRun(
            returncode=_SYSTEMCTL_TIMEOUT_RETURN_CODE,
            stdout="",
            stderr=f"systemctl timed out after {timeout:.0f}s",
        )
    except OSError:
        return _SystemctlRun(returncode=127, stdout="", stderr="systemctl unavailable")
    return _SystemctlRun(
        returncode=completed.returncode,
        stdout=decode_command_output(completed.stdout).strip(),
        stderr=decode_command_output(completed.stderr).strip(),
    )


def _systemd_user_unit_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def is_systemd_managed(
    service_name: str = DEFAULT_SERVICE_NAME,
    *,
    platform: str = sys.platform,
    runner: SystemctlRunner = _run_systemctl,
    unit_dir: Path | None = None,
) -> bool:
    """Return whether a systemd user unit currently owns the local server.

    Gated on the unit file's existence first, so non-systemd hosts never spawn a
    systemctl probe.
    """

    if not platform.startswith("linux") or not is_valid_systemd_service_name(service_name):
        return False
    units = unit_dir or _systemd_user_unit_dir()
    if not (units / f"{service_name}.service").is_file():
        return False
    active = runner(["systemctl", "--user", "is-active", f"{service_name}.service"])
    return active.returncode == 0 and active.stdout.strip() == "active"


def _await_vbot_health(
    instance: ServerInstance,
    *,
    timeout_seconds: float = _SYSTEMD_RESTART_READY_TIMEOUT_SECONDS,
    interval_seconds: float = _SYSTEMD_RESTART_PROBE_INTERVAL_SECONDS,
) -> HealthProbeResult:
    """Poll the health endpoint until the vBot server answers or the deadline passes."""

    deadline = time.monotonic() + timeout_seconds
    health = probe_health(instance)
    while not health.is_vbot and time.monotonic() < deadline:
        time.sleep(interval_seconds)
        health = probe_health(instance)
    return health


def _systemd_restart(
    instance: ServerInstance,
    service_name: str,
    *,
    runner: SystemctlRunner = _run_systemctl,
    await_health: Callable[[ServerInstance], HealthProbeResult] = _await_vbot_health,
) -> CommandResult:
    """Restart the server through its systemd user unit and confirm health."""

    restarted = runner(["systemctl", "--user", "restart", f"{service_name}.service"])
    if restarted.returncode != 0:
        return CommandResult(
            ok=False,
            message=(
                f"systemctl --user restart {service_name} failed: "
                f"{restarted.stderr or restarted.stdout}"
            ),
            instance=instance,
        )
    health = await_health(instance)
    if health.is_vbot:
        return CommandResult(
            ok=True,
            message="restarted via systemd",
            instance=instance,
            health=health,
            webui=probe_webui(instance),
            log_path=instance.log_path,
        )
    return CommandResult(
        ok=False,
        message="restarted via systemd, but the server did not become healthy in time",
        instance=instance,
        health=health,
        log_path=instance.log_path,
    )


def stop_systemd_server(
    instance: ServerInstance,
    service_name: str,
    *,
    runner: SystemctlRunner = _run_systemctl,
) -> CommandResult:
    """Stop an active systemd-owned server without removing or disabling its unit."""

    if not is_valid_systemd_service_name(service_name):
        return CommandResult(
            ok=False,
            message="invalid systemd service name",
            instance=instance,
        )
    stopped = runner(["systemctl", "--user", "stop", f"{service_name}.service"])
    if stopped.returncode != 0:
        return CommandResult(
            ok=False,
            message=(
                f"systemctl --user stop {service_name} failed: {stopped.stderr or stopped.stdout}"
            ),
            instance=instance,
        )
    return CommandResult(ok=True, message="stopped via systemd", instance=instance)


def start_systemd_server(
    instance: ServerInstance,
    service_name: str,
    *,
    runner: SystemctlRunner = _run_systemctl,
    await_health: Callable[[ServerInstance], HealthProbeResult] = _await_vbot_health,
) -> CommandResult:
    """Start an existing systemd user unit and confirm that vBot becomes healthy."""

    if not is_valid_systemd_service_name(service_name):
        return CommandResult(
            ok=False,
            message="invalid systemd service name",
            instance=instance,
        )
    started = runner(["systemctl", "--user", "start", f"{service_name}.service"])
    if started.returncode != 0:
        return CommandResult(
            ok=False,
            message=(
                f"systemctl --user start {service_name} failed: {started.stderr or started.stdout}"
            ),
            instance=instance,
        )
    health = await_health(instance)
    if health.is_vbot:
        return CommandResult(
            ok=True,
            message="started via systemd",
            instance=instance,
            health=health,
            webui=probe_webui(instance),
            log_path=instance.log_path,
        )
    return CommandResult(
        ok=False,
        message="started via systemd, but the server did not become healthy in time",
        instance=instance,
        health=health,
        log_path=instance.log_path,
    )


def restart_via_systemd_if_managed(
    instance: ServerInstance,
    *,
    service_name: str = DEFAULT_SERVICE_NAME,
    is_managed: Callable[[str], bool] = is_systemd_managed,
    do_restart: Callable[[ServerInstance, str], CommandResult] = _systemd_restart,
) -> CommandResult | None:
    """Restart via systemd when the target is unit-managed; return None otherwise."""

    if not is_managed(service_name):
        return None
    return do_restart(instance, service_name)


def restart_server(
    instance: ServerInstance,
    *,
    service_name: str = DEFAULT_SERVICE_NAME,
    stop: Callable[[ServerInstance], CommandResult] = stop_server,
    start: Callable[[ServerInstance], CommandResult] = start_server,
    is_managed: Callable[[str], bool] = is_systemd_managed,
    do_restart: Callable[[ServerInstance, str], CommandResult] = _systemd_restart,
) -> CommandResult:
    """Restart the local server, delegating to systemd when the unit owns it.

    On a systemd-managed install the managed stop/start would fight the unit (its
    ``Restart=`` directive races the spawned replacement, or systemd's view
    desyncs from the unmanaged process), so route the restart through the unit
    instead. Everywhere else fall back to the managed terminate-then-start path.
    """

    if not is_valid_systemd_service_name(service_name):
        return CommandResult(
            ok=False,
            message=(
                "invalid systemd service name; start with a letter or number, then use only "
                "letters, numbers, '.', '_', '@', or '-', without a .service suffix"
            ),
            instance=instance,
        )

    via_systemd = restart_via_systemd_if_managed(
        instance, service_name=service_name, is_managed=is_managed, do_restart=do_restart
    )
    if via_systemd is not None:
        return via_systemd

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
    service_name: str = DEFAULT_SERVICE_NAME,
    wait_pid: int | None = None,
) -> CommandResult:
    """Detach one private restart attempt from the current server-owned process tree."""

    if not is_valid_systemd_service_name(service_name):
        return CommandResult(ok=False, message="invalid systemd service name", instance=instance)
    parent_pid = _restart_handoff_wait_pid(instance) if wait_pid is None else wait_pid
    arguments = [
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
        "--service-name",
        service_name,
    ]
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
    parser.add_argument("--service-name", required=True)
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
        result = restart_server(instance, service_name=arguments.service_name)
        log = logger.info if result.ok else logger.error
        log("Scheduled update restart result: %s", result.message)
        return 0 if result.ok else 1
    finally:
        manager.close()


def get_status(instance: ServerInstance) -> CommandResult:
    """Return current vBot/API and WebUI status for the instance."""

    health = probe_health(instance)
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
