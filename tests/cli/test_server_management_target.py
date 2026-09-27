"""CLI server target: resolution, local listener ownership, health and WebUI probes, status."""

from __future__ import annotations

import json
import socket
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from cli import _server_target, server_management
from cli.server_management import (
    HealthProbeResult,
    ServerInstance,
    WebUIProbeResult,
    get_status,
    probe_health,
    probe_webui,
    resolve_instance,
)
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
        pytest.param(None, None, None, 8420, id="default"),
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


def connect_error(url: str, **_kwargs: Any) -> httpx.Response:
    raise httpx.ConnectError("offline", request=httpx.Request("GET", url))


def respond(response: httpx.Response) -> Callable[..., httpx.Response]:
    return lambda _url, **_kwargs: response


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
    ],
)
def test_probe_health_classifies_only_the_exact_health_answer_as_vbot(
    instance: ServerInstance,
    monkeypatch: pytest.MonkeyPatch,
    get: Callable[..., httpx.Response],
    expected: HealthProbeResult,
) -> None:
    monkeypatch.setattr(server_management.httpx, "get", get)

    assert probe_health(instance) == expected


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
    monkeypatch.setattr(server_management.httpx, "get", get)

    assert probe_webui(instance) == expected


@pytest.mark.parametrize(
    ("probe", "url", "response"),
    [
        (probe_health, "http://127.0.0.1:8420/health", httpx.Response(200, json={"status": "ok"})),
        (probe_webui, "http://127.0.0.1:8420/", httpx.Response(200)),
    ],
    ids=["health", "webui"],
)
def test_probes_request_the_wildcard_target_directly_on_loopback_without_proxies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: Callable[[ServerInstance], object],
    url: str,
    response: httpx.Response,
) -> None:
    instance = replace(make_instance(tmp_path), host="0.0.0.0", url="http://0.0.0.0:8420")
    captured: list[dict[str, Any]] = []

    def fake_get(url: str, *, timeout: float, trust_env: bool) -> httpx.Response:
        captured.append({"url": url, "timeout": timeout, "trust_env": trust_env})
        return response

    monkeypatch.setattr(server_management.httpx, "get", fake_get)

    probe(instance)

    assert captured == [
        {
            "url": url,
            "timeout": server_management.DEFAULT_PROBE_TIMEOUT_SECONDS,
            "trust_env": False,
        }
    ]


@pytest.mark.parametrize(
    ("health", "ok", "message", "webui"),
    [
        pytest.param(
            HealthProbeResult(reachable=True, is_vbot=True, status_code=200),
            True,
            "running",
            WebUIProbeResult(True, 200),
            id="running",
        ),
        pytest.param(
            HealthProbeResult(reachable=True, is_vbot=False, status_code=200),
            False,
            "port occupied by non-vBot process",
            WebUIProbeResult(False),
            id="non-vbot",
        ),
        pytest.param(
            HealthProbeResult(reachable=False, is_vbot=False, error="ConnectError"),
            True,
            "not running",
            WebUIProbeResult(False),
            id="not-running",
        ),
    ],
)
def test_get_status_reports_health_and_probes_the_webui_only_for_vbot(
    instance: ServerInstance,
    monkeypatch: pytest.MonkeyPatch,
    health: HealthProbeResult,
    ok: bool,
    message: str,
    webui: WebUIProbeResult,
) -> None:
    monkeypatch.setattr(server_management, "probe_health", lambda _instance: health)
    monkeypatch.setattr(
        server_management, "probe_webui", lambda _instance: WebUIProbeResult(True, 200)
    )

    result = get_status(instance)

    assert (result.ok, result.message, result.health, result.webui) == (ok, message, health, webui)
    assert result.log_path == instance.log_path
