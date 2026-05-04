"""Server lifecycle primitives for the vBot CLI."""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import httpx
import psutil  # type: ignore[import-untyped]

from core.utils.config import Config
from server.main import DEFAULT_HOST, resolve_port

DEFAULT_STARTUP_TIMEOUT_SECONDS = 10.0
DEFAULT_PROBE_TIMEOUT_SECONDS = 0.5
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 5.0
HEALTH_PATH = "/health"
SERVER_LOG_NAME = "server.log"
WEBUI_PATH = "/"


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
        log_path=resolved_data_dir / "logs" / SERVER_LOG_NAME,
    )


def probe_health(
    instance: ServerInstance,
    *,
    timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
) -> HealthProbeResult:
    """Probe `/health` and classify only the exact vBot health response as vBot."""

    try:
        response = httpx.get(f"{instance.url}{HEALTH_PATH}", timeout=timeout_seconds)
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
        response = httpx.get(f"{instance.url}{WEBUI_PATH}", timeout=timeout_seconds)
    except httpx.RequestError as exc:
        return WebUIProbeResult(available=False, error=exc.__class__.__name__)

    return WebUIProbeResult(
        available=200 <= response.status_code < 400,
        status_code=response.status_code,
    )


def start_server_process(instance: ServerInstance) -> subprocess.Popen[bytes]:
    """Start the foreground server entrypoint as a background subprocess."""

    instance.log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = instance.log_path.open("ab")
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
            log_file,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    return _open_server_process(args, log_file, start_new_session=True)


def _open_server_process(
    args: list[str],
    log_file: BinaryIO,
    *,
    creationflags: int = 0,
    start_new_session: bool = False,
) -> subprocess.Popen[bytes]:
    """Open the server subprocess with typed subprocess arguments."""

    try:
        return subprocess.Popen(
            args,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
    finally:
        log_file.close()


def start_server(
    instance: ServerInstance,
    *,
    startup_timeout_seconds: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
    probe_interval_seconds: float = 0.1,
) -> CommandResult:
    """Start the server and wait until vBot health is reachable."""

    initial_health = probe_health(instance)
    if initial_health.is_vbot:
        return CommandResult(
            ok=True,
            message="already running",
            instance=instance,
            health=initial_health,
            webui=probe_webui(instance),
            log_path=instance.log_path,
        )
    if initial_health.reachable:
        return CommandResult(
            ok=False,
            message="port occupied by non-vBot process",
            instance=instance,
            health=initial_health,
            log_path=instance.log_path,
        )

    process = start_server_process(instance)
    deadline = time.monotonic() + startup_timeout_seconds
    health = initial_health
    while time.monotonic() < deadline:
        health = probe_health(instance)
        if health.is_vbot:
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
            return CommandResult(
                ok=False,
                message="port occupied by non-vBot process",
                instance=instance,
                health=health,
                log_path=instance.log_path,
                process_id=process.pid,
            )
        if process.poll() is not None:
            return CommandResult(
                ok=False,
                message="server process exited before readiness",
                instance=instance,
                health=health,
                log_path=instance.log_path,
                process_id=process.pid,
            )
        time.sleep(probe_interval_seconds)

    return CommandResult(
        ok=False,
        message="server readiness timed out",
        instance=instance,
        health=health,
        log_path=instance.log_path,
        process_id=process.pid,
    )


def find_listening_process(instance: ServerInstance) -> psutil.Process | None:
    """Find the local process listening on the resolved TCP port."""

    for process in psutil.process_iter():
        try:
            connections = process.net_connections(kind="tcp")
        except psutil.Error:
            continue
        for connection in connections:
            if connection.status != psutil.CONN_LISTEN:
                continue
            if connection.laddr.port == instance.port:
                return process
    return None


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
            log_path=instance.log_path,
        )
    return CommandResult(
        ok=True,
        message="not running",
        instance=instance,
        health=health,
        log_path=instance.log_path,
    )
