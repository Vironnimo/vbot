"""Server lifecycle primitives for the vBot CLI."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import psutil  # type: ignore[import-untyped]

from core.utils.config import Config
from core.utils.logging import CONSOLE_LOGGING_ENV_VAR, LogManager, resolve_daily_log_path
from server.main import DEFAULT_HOST, resolve_port

DEFAULT_STARTUP_TIMEOUT_SECONDS = 10.0
DEFAULT_PROBE_TIMEOUT_SECONDS = 0.5
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 5.0
HEALTH_PATH = "/health"
WEBUI_PATH = "/"
WILDCARD_HOSTS = {"", "*", "0.0.0.0", "::"}
CLI_SERVER_LOGGER_NAME = "cli.server_management"


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
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
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
    """Stop a confirmed local vBot server with terminate/kill fallback."""

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

    forced = False
    try:
        process.terminate()
        process.wait(timeout=shutdown_timeout_seconds)
    except psutil.TimeoutExpired:
        forced = True
        process.kill()
        process.wait(timeout=shutdown_timeout_seconds)
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
