"""CLI server target: resolution, local listener ownership, health and WebUI probes, status."""

from __future__ import annotations

import json
import socket
import ssl
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from cli import _server_target, server_management
from cli.server_management import (
    UNRECORDED_SERVER_MESSAGE,
    UNRESPONSIVE_LISTENER_MESSAGE,
    HealthProbeResult,
    ServerInstance,
    WebUIProbeResult,
    get_status,
    probe_health,
    probe_health_patiently,
    probe_webui,
    resolve_instance,
)
from core.utils.config import DEFAULT_PORT
from core.utils.logging import resolve_daily_log_path
from tests.cli.cli_test_support import make_instance


def listening(ip: str | None, port: int) -> SimpleNamespace:
    """Return a process with one listening socket; ``ip=None`` omits the address field."""

    address = SimpleNamespace(port=port) if ip is None else SimpleNamespace(ip=ip, port=port)
    connection = SimpleNamespace(status=server_management.psutil.CONN_LISTEN, laddr=address)
    return SimpleNamespace(net_connections=lambda kind: [connection])


@pytest.mark.parametrize(
    ("listeners", "owner"),
    [
        pytest.param([(None, 9002), (None, 9001)], 1, id="port-without-address"),
        pytest.param([("127.0.0.2", 9001), ("127.0.0.1", 9001)], 1, id="exact-address"),
        pytest.param([("0.0.0.0", 9001)], 0, id="ipv4-wildcard"),
        pytest.param([("::", 9001)], None, id="ipv6-wildcard-misses-ipv4-target"),
    ],
)
def test_find_listening_process_matches_the_target_address_and_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    listeners: list[tuple[str | None, int]],
    owner: int | None,
) -> None:
    instance = make_instance(tmp_path, port=9001)
    processes = [listening(ip, port) for ip, port in listeners]
    monkeypatch.setattr(server_management.psutil, "process_iter", lambda: processes)

    found = _server_target.find_listening_process(instance)

    assert found is (None if owner is None else processes[owner])


@pytest.mark.parametrize(
    ("host", "resolved", "wildcard", "expected"),
    [
        ("192.0.2.10", ["192.0.2.10"], "0.0.0.0", True),
        ("192.0.2.40", ["192.0.2.40"], "0.0.0.0", False),
        ("local.example", ["192.0.2.10"], "0.0.0.0", True),
        ("remote.example", ["192.0.2.40"], "0.0.0.0", False),
        ("mixed.example", ["192.0.2.10", "192.0.2.40"], "0.0.0.0", False),
        ("2001:db8::10", ["2001:db8::10"], "::", True),
        ("2001:db8::40", ["2001:db8::40"], "::", False),
        ("remote.example", ["2001:db8::40"], "::", False),
        ("localhost", ["127.0.0.1", "::1"], "::", True),
        ("0.0.0.0", ["0.0.0.0"], "0.0.0.0", True),
    ],
)
def test_wildcard_listener_only_owns_locally_addressed_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host: str,
    resolved: list[str],
    wildcard: str,
    expected: bool,
) -> None:
    instance = replace(make_instance(tmp_path), host=host)
    process = listening(wildcard, instance.port)
    monkeypatch.setattr(
        _server_target.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET6 if ":" in address else socket.AF_INET, 1, 6, "", (address, 0))
            for address in resolved
        ],
    )
    monkeypatch.setattr(
        server_management.psutil,
        "net_if_addrs",
        lambda: {
            "ethernet": [
                SimpleNamespace(family=socket.AF_INET, address="192.0.2.10"),
                SimpleNamespace(family=socket.AF_INET6, address="2001:db8::10"),
            ]
        },
    )
    monkeypatch.setattr(server_management.psutil, "process_iter", lambda: [process])

    assert _server_target.find_listening_process(instance) is (process if expected else None)


@pytest.mark.parametrize(
    ("port", "environment", "settings", "expected"),
    [
        pytest.param(8700, "8600", 8500, 8700, id="explicit-port"),
        pytest.param(None, "8600", 8500, 8600, id="environment"),
        pytest.param(None, None, 8500, 8500, id="settings"),
        pytest.param(None, None, None, DEFAULT_PORT, id="default"),
    ],
)
def test_resolve_instance_uses_the_server_port_precedence_and_daily_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    port: int | None,
    environment: str | None,
    settings: int | None,
    expected: int,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    if settings is not None:
        (data_dir / "settings.json").write_text(
            json.dumps({"format_version": 1, "server_port": settings}), encoding="utf-8"
        )
    if environment is None:
        monkeypatch.delenv("VBOT_SERVER_PORT", raising=False)
    else:
        monkeypatch.setenv("VBOT_SERVER_PORT", environment)

    instance = resolve_instance(host="localhost", port=port, data_dir=data_dir)

    assert instance.port == expected
    assert instance.url == f"http://localhost:{expected}"
    assert instance.data_dir == data_dir.resolve()
    assert instance.log_path == resolve_daily_log_path(data_dir.resolve())
    assert instance.log_path.suffix == ".log"


@pytest.mark.parametrize(
    ("host", "expected_url"),
    [
        ("::1", "http://[::1]:8420"),
        ("[::1]", "http://[::1]:8420"),
        ("::", "http://[::1]:8420"),
        ("0.0.0.0", "http://127.0.0.1:8420"),
    ],
)
def test_resolve_instance_builds_connectable_ipv6_safe_url(
    tmp_path: Path, host: str, expected_url: str
) -> None:
    instance = resolve_instance(host=host, port=8420, data_dir=tmp_path)

    assert instance.url == expected_url


@pytest.mark.parametrize(
    ("text", "origin", "host", "port", "noted"),
    [
        pytest.param(
            "https://vbot.example.com",
            "https://vbot.example.com",
            "vbot.example.com",
            443,
            False,
            id="https",
        ),
        pytest.param(
            "vbot.example.com",
            "https://vbot.example.com",
            "vbot.example.com",
            443,
            True,
            id="no-scheme",
        ),
        pytest.param(
            "localhost:9000",
            "http://localhost:9000",
            "localhost",
            9000,
            True,
            id="loopback-no-scheme",
        ),
        pytest.param(
            "HTTPS://VBot.Example.com:443/app/?x=1#y",
            "https://vbot.example.com",
            "vbot.example.com",
            443,
            True,
            id="path-dropped",
        ),
        pytest.param("https://[::1]:8443", "https://[::1]:8443", "::1", 8443, False, id="ipv6"),
        pytest.param(
            "https://bücher.example",
            "https://xn--bcher-kva.example",
            "xn--bcher-kva.example",
            443,
            False,
            id="idn",
        ),
    ],
)
def test_a_server_address_names_the_target_by_its_origin(
    tmp_path: Path, text: str, origin: str, host: str, port: int, noted: bool
) -> None:
    address = _server_target.parse_server_url(text)
    instance = resolve_instance(url=text, data_dir=tmp_path)

    assert (address.origin, bool(address.notes)) == (origin, noted)
    assert (instance.url, instance.host, instance.port) == (origin, host, port)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "ftp://vbot.example.com",
        "https://user:secret@vbot.example.com",
        "https://x:0",
        "https://",
    ],
)
def test_an_unreadable_server_address_is_refused(text: str) -> None:
    with pytest.raises(ValueError) as refused:
        _server_target.parse_server_url(text)

    assert "secret" not in str(refused.value)


@pytest.mark.parametrize(
    ("argv", "environment", "expected"),
    [
        pytest.param(
            ["--url", "https://a.example"],
            {"VBOT_SERVER_URL": "https://b.example"},
            "https://a.example",
            id="flag-wins",
        ),
        pytest.param(
            [], {"VBOT_SERVER_URL": "https://b.example"}, "https://b.example", id="environment"
        ),
        pytest.param(
            ["--port", "8421"], {"VBOT_SERVER_URL": "https://b.example"}, None, id="explicit-port"
        ),
        pytest.param(
            [],
            {"VBOT_SERVER_URL": "https://b.example", "VBOT_RUN_AGENT_ID": "coder"},
            None,
            id="agent-run",
        ),
    ],
)
def test_the_server_address_comes_from_url_then_the_environment(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    environment: dict[str, str],
    expected: str | None,
) -> None:
    from cli.parser import parse_args

    monkeypatch.delenv("VBOT_RUN_AGENT_ID", raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    assert _server_target.selected_server_url(parse_args(["agent", "list", *argv])) == expected


def test_a_server_address_and_a_port_cannot_both_name_the_target(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from cli.parser import parse_args

    with pytest.raises(SystemExit) as refused:
        parse_args(["--url", "https://vbot.example.com", "agent", "list", "--port", "8421"])

    assert refused.value.code == 2
    assert "drop --host and --port" in capsys.readouterr().err


def connect_error(url: str, **_kwargs: Any) -> httpx.Response:
    raise httpx.ConnectError("offline", request=httpx.Request("GET", url))


def respond(response: httpx.Response) -> Callable[..., httpx.Response]:
    return lambda _url, **_kwargs: response


def read_timeout(url: str, **_kwargs: Any) -> httpx.Response:
    raise httpx.ReadTimeout("busy", request=httpx.Request("GET", url))


def socket_table(
    monkeypatch: pytest.MonkeyPatch, *listeners: tuple[str, int]
) -> list[SimpleNamespace]:
    """Make the system socket table list exactly these ``(ip, port)`` listeners."""

    table = [
        SimpleNamespace(
            status=server_management.psutil.CONN_LISTEN, laddr=SimpleNamespace(ip=ip, port=port)
        )
        for ip, port in listeners
    ]
    monkeypatch.setattr(server_management.psutil, "net_connections", lambda kind: table)
    return table


def read_budget(timeout: httpx.Timeout | float) -> float | None:
    """The response budget of a request timeout, as plain seconds or ``httpx.Timeout``."""

    return timeout.read if isinstance(timeout, httpx.Timeout) else timeout


@pytest.mark.parametrize(
    ("get", "expected"),
    [
        pytest.param(
            respond(httpx.Response(200, json={"status": "ok"})),
            HealthProbeResult(reachable=True, is_vbot=True, status_code=200),
            id="vbot",
        ),
        pytest.param(
            respond(httpx.Response(200, json={"status": "ok", "extra": True})),
            HealthProbeResult(reachable=True, is_vbot=False, status_code=200),
            id="extra-field",
        ),
        pytest.param(
            respond(httpx.Response(200, json={"status": "up"})),
            HealthProbeResult(reachable=True, is_vbot=False, status_code=200),
            id="other-status",
        ),
        pytest.param(
            respond(httpx.Response(503, json={"status": "ok"})),
            HealthProbeResult(reachable=True, is_vbot=False, status_code=503),
            id="error-status",
        ),
        pytest.param(
            respond(httpx.Response(200, content=b"not-json")),
            HealthProbeResult(reachable=True, is_vbot=False, status_code=200),
            id="not-json",
        ),
        pytest.param(
            connect_error,
            HealthProbeResult(reachable=False, is_vbot=False, error="ConnectError"),
            id="unreachable",
        ),
        pytest.param(
            read_timeout,
            HealthProbeResult(reachable=False, is_vbot=False, error="ReadTimeout", timed_out=True),
            id="timeout",
        ),
    ],
)
def test_probe_health_classifies_only_the_exact_health_answer_as_vbot(
    instance: ServerInstance,
    monkeypatch: pytest.MonkeyPatch,
    get: Callable[..., httpx.Response],
    expected: HealthProbeResult,
) -> None:
    socket_table(monkeypatch, (instance.host, instance.port))
    monkeypatch.setattr(httpx, "get", get)

    assert probe_health(instance) == expected


# A local handshake completes in the kernel, so only a remote target waits to connect.
LOCAL_PROBE_TIMEOUT = httpx.Timeout(0.5, connect=0.2)


@pytest.mark.parametrize(
    ("host", "listeners", "requested"),
    [
        pytest.param("127.0.0.1", [], LOCAL_PROBE_TIMEOUT, id="listener-absent-from-host-table"),
        pytest.param("127.0.0.1", [("127.0.0.1", 9999)], LOCAL_PROBE_TIMEOUT, id="other-port-only"),
        pytest.param("127.0.0.1", [("127.0.0.1", 8420)], LOCAL_PROBE_TIMEOUT, id="listener"),
        # A wildcard or dual-stack socket may accept the target's traffic.
        pytest.param("127.0.0.1", [("::", 8420)], LOCAL_PROBE_TIMEOUT, id="any-address"),
        pytest.param("127.0.0.1", None, LOCAL_PROBE_TIMEOUT, id="unreadable-table"),
        pytest.param("192.0.2.40", [], 0.5, id="remote-target"),
    ],
)
def test_probe_health_contacts_the_target_independently_of_the_host_socket_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host: str,
    listeners: list[tuple[str, int]] | None,
    requested: httpx.Timeout | float,
) -> None:
    """A table cannot prove absence; every local probe keeps its short connect budget."""

    instance = make_instance(tmp_path, host=host)
    if listeners is None:

        def unreadable(kind: str) -> list[SimpleNamespace]:
            raise server_management.psutil.AccessDenied()

        monkeypatch.setattr(server_management.psutil, "net_connections", unreadable)
    else:
        socket_table(monkeypatch, *listeners)
    timeouts: list[httpx.Timeout | float] = []

    def get(url: str, *, timeout: httpx.Timeout | float, **_kwargs: Any) -> httpx.Response:
        timeouts.append(timeout)
        return httpx.Response(200, json={"status": "ok"})

    monkeypatch.setattr(httpx, "get", get)

    health = probe_health(instance)

    assert timeouts == [requested]
    assert health.is_vbot


BUSY_ABSENT = HealthProbeResult(reachable=False, is_vbot=False, error="ReadTimeout", timed_out=True)


@pytest.mark.parametrize(
    ("answers", "listener", "requested_timeouts", "expected"),
    [
        pytest.param(
            [respond(httpx.Response(200, json={"status": "ok"}))],
            "127.0.0.1",
            [0.5],
            HealthProbeResult(reachable=True, is_vbot=True, status_code=200),
            id="quick-answer-needs-no-retry",
        ),
        pytest.param(
            [read_timeout, respond(httpx.Response(200, json={"status": "ok"}))],
            "127.0.0.1",
            [0.5, 5.0],
            HealthProbeResult(reachable=True, is_vbot=True, status_code=200),
            id="busy-listener-answers-late",
        ),
        pytest.param(
            [read_timeout, read_timeout],
            "127.0.0.1",
            [0.5, 5.0],
            replace(BUSY_ABSENT, unresponsive=True),
            id="listener-never-answers",
        ),
        pytest.param(
            [read_timeout, connect_error],
            "127.0.0.1",
            [0.5, 5.0],
            HealthProbeResult(reachable=False, is_vbot=False, error="ConnectError"),
            id="listener-closes-during-the-retry",
        ),
        # Windows reports a refused connection to a closed port as a connect timeout
        # within the quick budget, so a timeout alone must not mean a listener.
        pytest.param(
            [read_timeout], "127.0.0.2", [0.5], BUSY_ABSENT, id="other-address-listener-is-absent"
        ),
    ],
)
def test_probe_health_patiently_tells_a_busy_listener_from_an_absent_server(
    instance: ServerInstance,
    monkeypatch: pytest.MonkeyPatch,
    answers: list[Callable[..., httpx.Response]],
    listener: str,
    requested_timeouts: list[float],
    expected: HealthProbeResult,
) -> None:
    timeouts: list[float | None] = []
    scripted = iter(answers)

    def get(url: str, *, timeout: httpx.Timeout | float, **kwargs: Any) -> httpx.Response:
        timeouts.append(read_budget(timeout))
        return next(scripted)(url, **kwargs)

    monkeypatch.setattr(httpx, "get", get)
    socket_table(monkeypatch, (listener, instance.port))

    assert probe_health_patiently(instance) == expected
    assert timeouts == requested_timeouts


@pytest.mark.parametrize(
    ("get", "expected"),
    [
        (respond(httpx.Response(200)), WebUIProbeResult(available=True, status_code=200)),
        (respond(httpx.Response(302)), WebUIProbeResult(available=True, status_code=302)),
        (respond(httpx.Response(404)), WebUIProbeResult(available=False, status_code=404)),
        (respond(httpx.Response(500)), WebUIProbeResult(available=False, status_code=500)),
        (connect_error, WebUIProbeResult(available=False, error="ConnectError")),
    ],
    ids=["200", "302", "404", "500", "unreachable"],
)
def test_probe_webui_classifies_the_root_answer(
    instance: ServerInstance,
    monkeypatch: pytest.MonkeyPatch,
    get: Callable[..., httpx.Response],
    expected: WebUIProbeResult,
) -> None:
    monkeypatch.setattr(httpx, "get", get)

    assert probe_webui(instance) == expected


@pytest.mark.parametrize(
    ("probe", "url", "response", "timeout"),
    [
        (
            probe_health,
            "http://127.0.0.1:8420/health",
            httpx.Response(200, json={"status": "ok"}),
            LOCAL_PROBE_TIMEOUT,
        ),
        (
            probe_webui,
            "http://127.0.0.1:8420/",
            httpx.Response(200),
            server_management.DEFAULT_PROBE_TIMEOUT_SECONDS,
        ),
    ],
    ids=["health", "webui"],
)
def test_probes_request_the_wildcard_target_directly_on_loopback_without_proxies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: Callable[[ServerInstance], object],
    url: str,
    response: httpx.Response,
    timeout: httpx.Timeout | float,
) -> None:
    instance = replace(make_instance(tmp_path), host="0.0.0.0", url="http://0.0.0.0:8420")
    socket_table(monkeypatch, ("0.0.0.0", 8420))
    captured: list[dict[str, Any]] = []

    def fake_get(
        url: str, *, timeout: httpx.Timeout | float, trust_env: bool, verify: ssl.SSLContext
    ) -> httpx.Response:
        captured.append({"url": url, "timeout": timeout, "trust_env": trust_env})
        # A plain-http probe gets the fail-closed context that skips parsing the CA bundle.
        assert verify.get_ca_certs() == []
        return response

    monkeypatch.setattr(httpx, "get", fake_get)

    probe(instance)

    assert captured == [{"url": url, "timeout": timeout, "trust_env": False}]


@pytest.mark.parametrize(
    ("health", "recorded", "ok", "message", "webui"),
    [
        pytest.param(
            HealthProbeResult(reachable=True, is_vbot=True, status_code=200),
            True,
            True,
            "running",
            WebUIProbeResult(True, 200),
            id="running",
        ),
        pytest.param(
            HealthProbeResult(reachable=True, is_vbot=True, status_code=200),
            False,
            False,
            UNRECORDED_SERVER_MESSAGE,
            WebUIProbeResult(False),
            id="another-data-directory",
        ),
        pytest.param(
            HealthProbeResult(reachable=True, is_vbot=False, status_code=200),
            False,
            False,
            "port occupied by non-vBot process",
            WebUIProbeResult(False),
            id="non-vbot",
        ),
        pytest.param(
            HealthProbeResult(reachable=False, is_vbot=False, error="ConnectError"),
            False,
            True,
            "not running",
            WebUIProbeResult(False),
            id="not-running",
        ),
        pytest.param(
            HealthProbeResult(
                reachable=False,
                is_vbot=False,
                error="ReadTimeout",
                timed_out=True,
                unresponsive=True,
            ),
            False,
            False,
            UNRESPONSIVE_LISTENER_MESSAGE,
            None,
            id="unresponsive",
        ),
    ],
)
def test_get_status_reports_health_and_probes_the_webui_only_for_vbot(
    instance: ServerInstance,
    monkeypatch: pytest.MonkeyPatch,
    health: HealthProbeResult,
    recorded: bool,
    ok: bool,
    message: str,
    webui: WebUIProbeResult | None,
) -> None:
    monkeypatch.setattr(server_management, "probe_health_patiently", lambda _instance: health)
    monkeypatch.setattr(
        server_management,
        "_resolve_control_process",
        lambda _instance: object() if recorded else None,
    )
    probes: list[ServerInstance] = []

    def webui_probe(target: ServerInstance) -> WebUIProbeResult:
        probes.append(target)
        return WebUIProbeResult(True, 200)

    monkeypatch.setattr(server_management, "probe_webui", webui_probe)

    result = get_status(instance)

    assert (result.ok, result.message, result.health, result.webui) == (ok, message, health, webui)
    assert probes == ([instance] if recorded else [])
    assert result.log_path == (None if message == UNRECORDED_SERVER_MESSAGE else instance.log_path)
