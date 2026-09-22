"""CLI target resolution, lifecycle result records, and HTTP health classification."""

from __future__ import annotations

import socket
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import Literal

import httpx
import psutil  # type: ignore[import-untyped]

from core.utils.config import DEFAULT_HOST, Config, resolve_port
from core.utils.logging import resolve_daily_log_path

DEFAULT_PROBE_TIMEOUT_SECONDS = 0.5


HEALTH_PATH = "/health"


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
class RpcFailure:
    """Evidence about one failed RPC, never a rollback claim for a whole command."""

    method: str
    request_state: Literal["not_sent", "unknown", "responded"]
    code: str | None = None
    http_status: int | None = None


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
    attention: tuple[str, ...] = ()
    failure: RpcFailure | None = None


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
        url=build_server_base_url(host, resolved_port),
        log_path=resolve_daily_log_path(resolved_data_dir),
    )


def build_server_base_url(host: str, port: int) -> str:
    """Build the direct, IPv6-safe HTTP base URL for one server target."""

    connect_host = host
    if connect_host in {"", "*", "0.0.0.0"}:
        connect_host = "127.0.0.1"
    elif connect_host == "::":
        connect_host = "::1"
    connect_host = connect_host.removeprefix("[").removesuffix("]")
    if ":" in connect_host:
        connect_host = f"[{connect_host}]"
    return f"http://{connect_host}:{port}"


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

    return f"{build_server_base_url(instance.host, instance.port)}{path}"


WILDCARD_HOSTS = {"", "*", "0.0.0.0", "::"}


def find_listening_process(instance: ServerInstance) -> psutil.Process | None:
    """Find the local process listening on the resolved TCP host and port."""

    if not is_local_target(instance):
        return None
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


def is_local_target(instance: ServerInstance) -> bool:
    """Require local addresses before treating a target as a local process owner.

    A wildcard socket only covers this machine, not every same-family address.
    Reject mixed local/remote DNS answers too: the health request could reach
    any returned address, so it cannot identify a local process reliably.
    """

    host = instance.host.removeprefix("[").removesuffix("]")
    if host in WILDCARD_HOSTS:
        return True
    addresses = set()
    for value in _host_addresses(host):
        try:
            addresses.add(ip_address(value.partition("%")[0]))
        except ValueError:
            continue
    if not addresses:
        return False
    if all(address.is_loopback for address in addresses):
        return True
    try:
        interfaces = psutil.net_if_addrs()
    except (psutil.Error, OSError):
        return False
    local_addresses = set()
    for entries in interfaces.values():
        for entry in entries:
            if entry.family not in {socket.AF_INET, socket.AF_INET6}:
                continue
            try:
                local_addresses.add(ip_address(entry.address.partition("%")[0]))
            except ValueError:
                continue
    return all(address.is_loopback or address in local_addresses for address in addresses)


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
