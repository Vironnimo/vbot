"""CLI target resolution, lifecycle result records, and HTTP health classification."""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from ipaddress import ip_address
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from core.utils.config import DEFAULT_HOST, Config, resolve_port
from core.utils.logging import resolve_daily_log_path

if TYPE_CHECKING:
    # httpx loads on the first request and psutil on the first local listener
    # check, so commands that never need them (help, parse errors) skip both.
    import httpx
    import psutil  # type: ignore[import-untyped]

DEFAULT_PROBE_TIMEOUT_SECONDS = 0.5


# The kernel completes a local handshake even while the server's Event Loop is
# busy, so connecting to a local target needs only a short budget. A refused
# connection is what takes long: Windows retries the SYN for about 2 s.
LOCAL_CONNECT_TIMEOUT_SECONDS = 0.2


# A busy Event Loop (heavy Run load, a large replay) can hold back a reply for
# seconds. Lifecycle decisions that would treat "no answer" as "not running" give
# a listener this long before concluding it is unresponsive.
PATIENT_PROBE_TIMEOUT_SECONDS = 5.0


HEALTH_PATH = "/health"


#: The server address the CLI connects to when no target option names one.
SERVER_URL_ENVIRONMENT_VARIABLE = "VBOT_SERVER_URL"
_RUN_AGENT_ENVIRONMENT_VARIABLE = "VBOT_RUN_AGENT_ID"
_DEFAULT_PORTS = {"http": 80, "https": 443}


WEBUI_PATH = "/"


# Lifecycle outcomes (`server_management.classify_server`) that `_output` compares
# results with; they live here so loading `_output` does not load the lifecycle stack.
# Another process, not vBot, holds the target port.
NON_VBOT_LISTENER_MESSAGE = "port occupied by non-vBot process"

# A vBot server answers on the target port, but the target data directory's control
# record does not name it: it belongs to another data directory and is left running.
UNRECORDED_SERVER_MESSAGE = "port occupied by a vBot server of another data directory"


@dataclass(frozen=True)
class ServerInstance:
    """One resolved server target.

    ``url`` is the origin every request goes to: ``http://<host>:<port>`` for a
    host and port target, or the normalized ``--url`` origin, possibly
    ``https://``. ``data_dir`` is this computer's instance directory, where the
    local access token of a server on this computer is found.
    """

    host: str
    port: int
    data_dir: Path
    url: str
    log_path: Path


@dataclass(frozen=True)
class HealthProbeResult:
    """Result of probing a target server's vBot health endpoint.

    ``reachable`` means some HTTP answer arrived. ``timed_out`` records only that
    none arrived in time: it does not prove a listener exists, because Windows
    reports a refused connection to a closed port as a connect timeout under a
    short budget. ``unresponsive`` is the conclusion of ``probe_health_patiently``:
    a local listener holds the target but never answered.
    """

    reachable: bool
    is_vbot: bool
    status_code: int | None = None
    error: str | None = None
    timed_out: bool = False
    unresponsive: bool = False


@dataclass(frozen=True)
class WebUIProbeResult:
    """Result of probing whether the WebUI is available from the server."""

    available: bool
    status_code: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class RpcFailure:
    """Evidence about one failed RPC, never a rollback claim for a whole command.

    ``data`` is the server error's structured ``data`` object, when it sent one.
    """

    method: str
    request_state: Literal["not_sent", "unknown", "responded"]
    code: str | None = None
    http_status: int | None = None
    data: Mapping[str, Any] | None = field(default=None, compare=False, hash=False)


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
    url: str | None = None,
) -> ServerInstance:
    """Resolve a CLI target using the same port rules as the server.

    ``url`` (a server address such as ``https://vbot.example.com``) replaces
    ``host`` and ``port``; ``data_dir`` still names this computer's instance
    directory. Raises ``ValueError`` for an address :func:`parse_server_url`
    refuses.
    """

    config = Config(data_dir=Path(data_dir) if data_dir is not None else None)
    resolved_data_dir = config.data_dir.expanduser().resolve()
    log_path = resolve_daily_log_path(resolved_data_dir)
    if url is not None:
        address = parse_server_url(url)
        return ServerInstance(
            host=address.host,
            port=address.port,
            data_dir=resolved_data_dir,
            url=address.origin,
            log_path=log_path,
        )
    resolved_port = resolve_port(config, port)
    return ServerInstance(
        host=host,
        port=resolved_port,
        data_dir=resolved_data_dir,
        url=build_server_base_url(host, resolved_port),
        log_path=log_path,
    )


@dataclass(frozen=True)
class ServerAddress:
    """A normalized server address: its origin, host (without brackets) and port.

    ``notes`` name each reading the normalization needed, one ``Note:`` line each.
    """

    origin: str
    host: str
    port: int
    notes: tuple[str, ...] = ()


def parse_server_url(text: str) -> ServerAddress:
    """Normalize a server address to its ``scheme://host[:port]`` origin.

    An address without a scheme is read as ``https://``, or as ``http://`` for a
    loopback host, and a path, query or fragment is dropped: vBot serves at the
    root of its origin. Both readings are reported in ``notes``. Raises
    ``ValueError`` for another scheme, a user name or password, or a missing or
    malformed host or port.
    """

    from core.access.config import is_loopback_host

    raw = text.strip()
    if not raw:
        raise ValueError("The server address is empty; pass one like https://vbot.example.com")
    if any(ord(char) < 33 or ord(char) == 127 for char in raw):
        raise ValueError(f"The server address {raw!r} contains spaces or control characters")
    notes: list[str] = []
    try:
        if "://" not in raw:
            bare_host = urlsplit(f"//{raw}").hostname or ""
            scheme = "http" if is_loopback_host(bare_host) else "https"
            notes.append(f"Read the server address {raw} as {scheme}://{raw}.")
            raw = f"{scheme}://{raw}"
        parts = urlsplit(raw)
        explicit_port = parts.port
    except ValueError:
        raise ValueError(f"The server address {text.strip()} has an invalid host or port") from None
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise ValueError(
            f"The server address {text.strip()} must start with https://, or http:// for a "
            "server on this computer or a trusted network"
        )
    host = parts.hostname
    if parts.username is not None or parts.password is not None:
        raise ValueError(
            "The server address must not contain a user name or password; pass "
            f"{scheme}://{host or 'host'} without them"
        )
    if not host:
        raise ValueError(f"The server address {text.strip()} names no host")
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise ValueError(f"The server address {text.strip()} has an invalid host") from None
    if explicit_port == 0:
        raise ValueError(f"The server address {text.strip()} has an invalid host or port")
    port = explicit_port or _DEFAULT_PORTS[scheme]
    origin = f"{scheme}://{f'[{host}]' if ':' in host else host}"
    if port != _DEFAULT_PORTS[scheme]:
        origin += f":{port}"
    rest = parts.path.removeprefix("/")
    rest += f"?{parts.query}" if parts.query else ""
    rest += f"#{parts.fragment}" if parts.fragment else ""
    if rest:
        notes.append(f"Ignored /{rest} in the server address; the CLI connects to {origin}.")
    return ServerAddress(origin=origin, host=host, port=port, notes=tuple(notes))


def server_origin(url: str) -> str | None:
    """The normalized origin of an absolute ``http(s)`` URL; ``None`` for anything else."""

    if "://" not in url:
        return None
    try:
        return parse_server_url(url).origin
    except ValueError:
        return None


def selected_server_url(arguments: object) -> str | None:
    """The server address a command targets: ``--url``, else ``VBOT_SERVER_URL``.

    An explicit ``--host`` or ``--port`` overrides the environment variable, and
    so does an Agent's Run (``VBOT_RUN_AGENT_ID``): its commands reach the server
    that runs it, which the instance variables of its shell name.
    """

    url = getattr(arguments, "url", None)
    if url:
        return str(url)
    explicit = getattr(arguments, "_target_arguments", {})
    if "host" in explicit or "port" in explicit:
        return None
    if os.environ.get(_RUN_AGENT_ENVIRONMENT_VARIABLE, "").strip():
        return None
    return os.environ.get(SERVER_URL_ENVIRONMENT_VARIABLE, "").strip() or None


def is_loopback_target(instance: ServerInstance) -> bool:
    """Whether requests to ``instance`` go to this computer's loopback interface."""

    from core.access.config import is_loopback_host

    host = urlsplit(instance.url).hostname
    return host is not None and is_loopback_host(host)


def is_url_target(instance: ServerInstance) -> bool:
    """Whether only ``--url`` names ``instance``: ``--host`` and ``--port`` reach only ``http``."""

    url = urlsplit(instance.url)
    return url.scheme != "http" or url.port != instance.port


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
    """Probe `/health` and classify only the exact vBot health response as vBot.

    Always ask the target: a host socket table can omit a listener in another
    network namespace. A local request gets ``LOCAL_CONNECT_TIMEOUT_SECONDS``
    to connect, bounding the delay of a refused connection on Windows.
    """

    import httpx

    from core.utils.tls import ssl_context_for

    local = is_local_target(instance)
    timeout: httpx.Timeout | float = timeout_seconds
    if local:
        connect = min(timeout_seconds, LOCAL_CONNECT_TIMEOUT_SECONDS)
        timeout = httpx.Timeout(timeout_seconds, connect=connect)
    url = _probe_url(instance, HEALTH_PATH)
    try:
        response = httpx.get(
            url,
            timeout=timeout,
            trust_env=False,
            verify=ssl_context_for(url),
        )
    except httpx.RequestError as exc:
        return HealthProbeResult(
            reachable=False,
            is_vbot=False,
            error=exc.__class__.__name__,
            timed_out=isinstance(exc, httpx.TimeoutException),
        )
    return health_result(response)


def probe_health_patiently(instance: ServerInstance) -> HealthProbeResult:
    """Probe `/health`, telling a missing server from a listener that is only busy.

    The quick probe suits polling loops, but a lifecycle decision made on one
    missed answer would take a stalled Event Loop for a server that is not
    running. When the quick probe times out and a local listener holds the target,
    one longer probe follows; a listener that still does not answer is reported
    ``unresponsive``. Without a local listener, or for a remote target, the quick
    result stands.
    """

    health = probe_health(instance)
    if not health.timed_out or not has_local_listener(instance):
        return health
    health = probe_health(instance, timeout_seconds=PATIENT_PROBE_TIMEOUT_SECONDS)
    if health.timed_out:
        return replace(health, unresponsive=True)
    return health


def health_result(response: httpx.Response) -> HealthProbeResult:
    """Classify one `/health` response; only the exact vBot body counts as vBot."""

    import httpx

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

    import httpx

    from core.utils.tls import ssl_context_for

    url = _probe_url(instance, WEBUI_PATH)
    try:
        response = httpx.get(
            url,
            timeout=timeout_seconds,
            trust_env=False,
            verify=ssl_context_for(url),
        )
    except httpx.RequestError as exc:
        return WebUIProbeResult(available=False, error=exc.__class__.__name__)

    return WebUIProbeResult(
        available=200 <= response.status_code < 400,
        status_code=response.status_code,
    )


def _probe_url(instance: ServerInstance, path: str) -> str:
    """Return the probe URL for health and WebUI checks; a wildcard host is probed on loopback."""

    if is_url_target(instance):
        return f"{instance.url}{path}"
    return f"{build_server_base_url(instance.host, instance.port)}{path}"


WILDCARD_HOSTS = {"", "*", "0.0.0.0", "::"}


def find_listening_process(instance: ServerInstance) -> psutil.Process | None:
    """Find the local process listening on the resolved TCP host and port."""

    import psutil  # type: ignore[import-untyped]

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


def has_local_listener(instance: ServerInstance) -> bool:
    """Return whether a local socket listens on the resolved TCP host and port.

    One system-wide table read, unlike ``find_listening_process``, which needs a
    per-process scan to name the owner. An unreadable table counts as no listener,
    so a lifecycle decision falls back to the quick probe's verdict.
    """

    import psutil  # type: ignore[import-untyped]

    if not is_local_target(instance):
        return False
    try:
        connections = psutil.net_connections(kind="tcp")
    except psutil.Error, OSError:
        return False
    return any(
        connection.status == psutil.CONN_LISTEN
        and _connection_matches_instance(connection, instance)
        for connection in connections
    )


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
    import psutil  # type: ignore[import-untyped]

    try:
        interfaces = psutil.net_if_addrs()
    except psutil.Error, OSError:
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
    except IndexError, TypeError:
        return ""
