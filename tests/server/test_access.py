"""The access guard at the server edge: modes, peers, credentials, pairing and devices."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from collections.abc import AsyncIterator, Iterator, MutableMapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient  # type: ignore[import-not-found]
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.websockets import WebSocketDisconnect

import server.app as server_app
from core.access.config import AccessConfig, access_config
from core.access.devices import DeviceStore
from server.access import (
    AccessError,
    AccessGuard,
    AccessService,
    Principal,
    _pairing,
    current_principal,
)
from server.access._limits import RefusalLog, SlidingWindow
from server.app import create_app
from server.rpc import methods
from server.rpc.methods import dispatch_rpc
from tests.server.app_test_support import ServerStubRuntime

_PUBLIC_URL = "https://vbot.example.org"
_PROXY = "10.0.0.2"
_LOCAL_TOKEN = "vbl_" + "L" * 43
_HOST = {"Authorization": f"Bearer {_LOCAL_TOKEN}"}
#: A browser on the internet as the trusted reverse proxy forwards it.
_INTERNET = {"x-test-peer": _PROXY, "X-Forwarded-For": "203.0.113.7", "X-Forwarded-Proto": "https"}
#: A route that needs a principal and answers 404 once admitted.
_PROTECTED = "/api/runs/missing/events"


class _Network:
    """Stands in for the network: a test-only header names each request's peer address."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] in {"http", "websocket"}:
            headers = list(scope["headers"])
            peer = next((value for name, value in headers if name == b"x-test-peer"), None)
            if peer is not None:
                scope = {
                    **scope,
                    "client": (peer.decode(), 40000),
                    "headers": [(name, value) for name, value in headers if name != b"x-test-peer"],
                }
        await self.app(scope, receive, send)


def _authenticated(**options: Any) -> AccessConfig:
    return access_config(
        "authenticated", public_url=_PUBLIC_URL, trusted_proxies=[_PROXY], **options
    )


@pytest.fixture
def served(tmp_path: Path) -> Iterator[Any]:
    """Serve an app in the given access mode; yields a factory of (app, client)."""
    clients: list[TestClient] = []

    def serve(access: AccessConfig | None = None) -> tuple[Any, TestClient]:
        app = create_app(
            runtime=ServerStubRuntime(tmp_path / f"data-{len(clients)}"),
            access=access,
            local_access_token=_LOCAL_TOKEN,
        )
        client = TestClient(_Network(app))
        client.__enter__()
        clients.append(client)
        return app, client

    yield serve
    for client in clients:
        client.__exit__(None, None, None)


def _rpc(
    client: TestClient, method: str, params: dict[str, Any] | None = None, /, **headers: str
) -> Any:
    response = client.post(
        "/api/rpc", json={"method": method, "params": params or {}}, headers=headers or _HOST
    )
    return response.json()


def _pair(
    client: TestClient, kind: str = "browser", name: str = "Phone", **headers: str
) -> dict[str, Any]:
    response = client.post(
        "/api/auth/pair/start", json={"kind": kind, "name": name}, headers=headers or _INTERNET
    )
    assert response.status_code == 200, response.text
    started: dict[str, Any] = response.json()
    return started


def _poll(client: TestClient, started: dict[str, Any], **headers: str):
    return client.post(
        "/api/auth/pair/poll",
        json={"request_id": started["request_id"], "poll_secret": started["poll_secret"]},
        headers=headers or _INTERNET,
    )


def _paired_token(client: TestClient, kind: str = "cli") -> tuple[str, str]:
    """Pair a native device approved by the host; returns its id and token."""
    started = _pair(client, kind=kind)
    assert _rpc(client, "devices.approve", {"code": started["code"]})["ok"]
    approved = _poll(client, started).json()
    return approved["device"]["id"], approved["token"]


@pytest.mark.parametrize(
    ("access", "headers", "url", "status"),
    [
        # local: this computer only, never through a proxy.
        (None, {"x-test-peer": "127.0.0.1"}, _PROTECTED, 404),
        (None, {"x-test-peer": "::1"}, _PROTECTED, 404),
        (None, {"x-test-peer": "192.168.1.5"}, _PROTECTED, 403),
        (None, {"x-test-peer": "127.0.0.1", "X-Forwarded-For": "192.168.1.5"}, _PROTECTED, 403),
        # lan: private networks and added ones; peer rules hold on exempt routes too.
        (access_config("lan"), {"x-test-peer": "192.168.1.5"}, _PROTECTED, 404),
        (access_config("lan"), {"x-test-peer": "10.1.2.3"}, _PROTECTED, 404),
        (access_config("lan"), {"x-test-peer": "fd00::5"}, _PROTECTED, 404),
        (access_config("lan"), {"x-test-peer": "fe80::1"}, _PROTECTED, 404),
        (access_config("lan"), {"x-test-peer": "::ffff:192.168.1.5"}, _PROTECTED, 404),
        (access_config("lan"), {"x-test-peer": "203.0.113.7"}, _PROTECTED, 403),
        (access_config("lan"), {"x-test-peer": "::ffff:203.0.113.7"}, _PROTECTED, 403),
        (access_config("lan"), {"x-test-peer": "203.0.113.7"}, "/health", 403),
        (access_config("lan"), {"x-test-peer": "100.64.1.2"}, _PROTECTED, 403),
        (
            access_config("lan", allow_networks=["100.64.0.0/10"]),
            {"x-test-peer": "100.64.1.2"},
            _PROTECTED,
            404,
        ),
        (
            access_config("lan"),
            {"x-test-peer": "192.168.1.5", "Forwarded": "for=192.168.1.9"},
            "/health",
            403,
        ),
        (access_config("lan"), {"x-test-peer": "192.168.1.5", "Via": "1.1 proxy"}, "/health", 403),
        (
            access_config("lan"),
            {"x-test-peer": "192.168.1.5", "CF-Connecting-IP": "203.0.113.7"},
            "/health",
            403,
        ),
        # authenticated: HTTPS from everyone but this computer, a credential everywhere.
        (_authenticated(), {"x-test-peer": "127.0.0.1"}, _PROTECTED, 401),
        (_authenticated(), {"x-test-peer": "127.0.0.1"}, "/health", 200),
        (_authenticated(), {"x-test-peer": "203.0.113.7"}, "/health", 403),
        (_authenticated(), {"x-test-peer": "203.0.113.7"}, f"https://testserver{_PROTECTED}", 401),
        (_authenticated(), _INTERNET, _PROTECTED, 401),
        (_authenticated(), _INTERNET, "/health", 200),
        (_authenticated(), {**_INTERNET, "X-Forwarded-Proto": "http"}, "/health", 403),
        (_authenticated(), {**_INTERNET, "X-Forwarded-Proto": "https, http"}, "/health", 403),
        (_authenticated(), {**_INTERNET, "X-Forwarded-For": "not-an-address"}, "/health", 403),
        (
            _authenticated(),
            {"x-test-peer": _PROXY, "X-Forwarded-Proto": "https"},
            "/health",
            403,
        ),
        (_authenticated(), {**_INTERNET, "X-Forwarded-For": _PROXY}, "/health", 403),
        (
            access_config("authenticated", public_url=_PUBLIC_URL, trusted_proxies=["10.0.0.0/24"]),
            {**_INTERNET, "X-Forwarded-For": "10.0.0.8, 10.0.0.9"},
            "/health",
            403,
        ),
        (
            access_config("authenticated", public_url=_PUBLIC_URL, trusted_proxies=["10.0.0.0/24"]),
            {**_INTERNET, "X-Forwarded-For": "198.51.100.1, 203.0.113.7, 10.0.0.9"},
            "/health",
            200,
        ),
        (
            _authenticated(),
            {"x-test-peer": "10.0.0.3", "X-Forwarded-For": "203.0.113.7"},
            "/health",
            403,
        ),
    ],
)
def test_each_mode_admits_only_its_peers_and_transports(
    served: Any, access: AccessConfig | None, headers: dict[str, str], url: str, status: int
) -> None:
    _app, client = served(access)

    response = client.get(url, headers=headers)

    assert response.status_code == status
    if status in {401, 403}:
        code = "auth_required" if status == 401 else "access_denied"
        assert response.json() == {
            "ok": False,
            "error": {"code": code, "message": response.json()["error"]["message"]},
        }
        assert response.headers["cache-control"] == "no-store"


def test_every_route_but_the_exempt_ones_needs_a_principal_when_authenticated(
    served: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(server_app, "WEBUI_DIST_DIR", _write_webui_build(tmp_path))
    app, client = served(_authenticated())
    exempt = {
        ("GET", "/health"),
        ("HEAD", "/health"),
        ("GET", "/api/auth/status"),
        ("POST", "/api/auth/pair/start"),
        ("POST", "/api/auth/pair/poll"),
        ("GET", "/api/oauth/callback"),
        ("POST", "/_vbot/control/shutdown"),
        ("GET", "/auth/ticket/{ticket}"),
        ("HEAD", "/auth/ticket/{ticket}"),
    }
    capability_prefixes = (
        "/api/files/",
        "/api/preview-assets/",
        "/api/extension-assets/",
        "/api/extension-runs/",
    )
    shell = {("GET", "/"), ("HEAD", "/"), ("GET", "/{path:path}"), ("HEAD", "/{path:path}")}

    answered: dict[tuple[str, str], int] = {}
    for route in app.routes:
        if isinstance(route, WebSocketRoute):
            with (
                pytest.raises(WebSocketDisconnect) as closed,
                client.websocket_connect(_concrete(route.path)) as websocket,
            ):
                websocket.receive_text()
            assert closed.value.code == 4401, route.path
            continue
        if isinstance(route, Mount):
            answered[("GET", route.path)] = client.get(f"{route.path}/app.js").status_code
            continue
        assert isinstance(route, Route)
        for method in sorted(route.methods or ()):
            response = client.request(method, _concrete(route.path))
            answered[(method, route.path)] = response.status_code
            capability = method in {"GET", "HEAD"} and route.path.startswith(capability_prefixes)
            if (method, route.path) in exempt or capability:
                assert response.status_code != 401, (method, route.path)
            elif (method, route.path) in shell:
                assert response.status_code == 200, (method, route.path)
                if method == "GET":
                    policy = response.headers["content-security-policy"]
                    assert "frame-ancestors 'none'" in policy
                    # Remote images only over https: plain http would expose the reader.
                    assert "img-src 'self' data: blob: https:;" in policy
                    assert response.headers["referrer-policy"] == "same-origin"
            else:
                assert response.status_code == 401, (method, route.path)
                assert response.headers["www-authenticate"] == "Bearer"

    assert answered[("GET", "/assets")] == 200
    assert answered[("POST", "/api/rpc")] == 401
    # A capability URL exempts only reads of its exact route shapes.
    assert client.post("/api/files/x").status_code == 401
    assert client.get("/api/files/x/y").status_code == 401
    assert client.get("/api/extension-runs/x/other").status_code == 401
    # No generated API documentation reveals the route table.
    assert client.get("/openapi.json").headers["content-type"].startswith("text/html")
    assert client.get("/docs").headers["content-type"].startswith("text/html")


def test_security_headers_and_the_public_origin(served: Any) -> None:
    _app, client = served(_authenticated())

    https = client.get("https://testserver/health", headers={"x-test-peer": "203.0.113.7"})
    plain = client.get("/health")
    public = client.get("/health", headers={**_INTERNET, "Origin": _PUBLIC_URL})
    foreign = client.get("/health", headers={**_INTERNET, "Origin": "https://attacker.example"})

    assert https.headers["x-content-type-options"] == "nosniff"
    assert https.headers["strict-transport-security"] == "max-age=31536000"
    assert "strict-transport-security" not in plain.headers
    assert (public.status_code, foreign.status_code) == (200, 403)


def test_only_known_host_names_are_answered(served: Any, caplog: pytest.LogCaptureFixture) -> None:
    _app, lan = served(access_config("lan", allow_hosts=["vbot.home.arpa"]))
    _app, internet = served(_authenticated())
    peer = {"x-test-peer": "192.168.1.5"}

    def status(client: TestClient, host: str, headers: dict[str, str]) -> int:
        return client.get("/health", headers={**headers, "Host": host}).status_code

    known = [
        status(lan, host, peer)
        for host in (
            "testserver",
            "localhost:8765",
            "127.0.0.1:8765",
            "[::1]:8765",
            "192.168.1.20",
            "VBOT.home.arpa.:8765",
        )
    ]
    # DNS rebinding: an attacker's name that resolves to this server.
    rebound = lan.get("/health", headers={**peer, "Host": "rebind.attacker.example"})
    doubled = lan.get(
        "/health",
        headers=[("x-test-peer", "192.168.1.5"), ("Host", "testserver"), ("Host", "evil.example")],
    )
    public = status(internet, "vbot.example.org", _INTERNET)
    foreign = status(internet, "attacker.example", _INTERNET)

    assert known == [200] * 6
    assert rebound.status_code == 403
    assert rebound.json()["error"] == {
        "code": "access_denied",
        "message": "Access denied: unknown host name",
    }
    assert doubled.status_code == 403
    assert (public, foreign) == (200, 403)
    assert (
        "Access refused (reason=host_not_allowed client=192.168.1.5 path=/health "
        "host=rebind.attacker.example)"
    ) in caplog.text


def test_the_local_access_token_counts_only_from_direct_loopback(served: Any) -> None:
    _app, client = served(_authenticated())
    same_host_proxy = served(
        access_config("authenticated", public_url=_PUBLIC_URL, trusted_proxies=["127.0.0.1"])
    )[1]
    wrong = {"Authorization": "Bearer vbl_" + "W" * 43}

    direct = client.get(_PROTECTED, headers=_HOST)
    forwarded = client.get(_PROTECTED, headers={**_HOST, "X-Forwarded-For": "127.0.0.1"})
    proxied = client.get(_PROTECTED, headers={**_HOST, **_INTERNET})
    remote_tls = client.get(
        f"https://testserver{_PROTECTED}", headers={**_HOST, "x-test-peer": "192.168.1.5"}
    )
    through_same_host_proxy = same_host_proxy.get(
        _PROTECTED,
        headers={**_HOST, "X-Forwarded-For": "203.0.113.7", "X-Forwarded-Proto": "https"},
    )
    mistyped = client.get(_PROTECTED, headers=wrong)
    status = client.get("/api/auth/status", headers=_HOST).json()

    assert direct.status_code == 404
    assert [
        response.status_code
        for response in (forwarded, proxied, remote_tls, through_same_host_proxy, mistyped)
    ] == [401] * 5
    assert status == {
        "mode": "authenticated",
        "mode_label": "With login (internet)",
        "authenticated": True,
        "principal": "host",
        "device": None,
    }


def test_modes_without_login_ignore_credentials(served: Any) -> None:
    _app, client = served(access_config("lan"))
    lan = {"x-test-peer": "192.168.1.5"}

    bad_token = client.get(_PROTECTED, headers={**lan, "Authorization": "Bearer vbt_bad"})
    status = client.get("/api/auth/status", headers=lan).json()
    pairing = client.post("/api/auth/pair/start", json={"kind": "cli", "name": "x"}, headers=lan)
    ticket = _rpc(client, "auth.login_ticket", **lan)

    assert bad_token.status_code == 404
    assert status["mode"] == "lan" and status["principal"] == "open"
    assert pairing.status_code == 409 and pairing.json()["error"]["code"] == "auth_disabled"
    assert ticket["error"]["code"] == "auth_disabled"
    # Devices of an earlier authenticated period stay manageable from the home network.
    assert _rpc(client, "devices.list", **lan) == {"ok": True, "result": {"devices": []}}


def test_a_browser_pairs_once_by_code_and_signs_in_with_its_cookie(
    served: Any, tmp_path: Path
) -> None:
    _app, client = served(_authenticated())

    started = _pair(client, name="  Anna's   phone ")
    pending = _poll(client, started).json()
    shown = _rpc(client, "devices.pending", {"code": started["code"].replace("-", "").lower()})
    approved = _rpc(client, "devices.approve", {"code": started["code"]})
    claimed = _poll(client, started)
    # A device whose approved answer got lost asks again and gets the same one.
    again = _poll(client, started)
    cookie = _cookie(claimed)
    signed_in = client.get(
        "/api/auth/status", headers={**_INTERNET, "Cookie": f"__Host-vbot_device={cookie}"}
    )
    listed = _rpc(client, "devices.list", **_INTERNET, Cookie=f"__Host-vbot_device={cookie}")
    # A second cookie of the same name may be planted; which one is meant is unknown.
    shadowed = client.get(
        "/api/auth/status",
        headers={**_INTERNET, "Cookie": f"__Host-vbot_device={cookie}; __Host-vbot_device=x"},
    )

    assert re.fullmatch(r"[2-9A-HJKMNP-TV-Z]{4}-[2-9A-HJKMNP-TV-Z]{4}", started["code"])
    assert (started["expires_in"], started["poll_interval"]) == (600, 2)
    assert pending["status"] == "pending"
    assert shown["result"] | {"requested_at": None, "expires_in": None} == {
        "code": started["code"],
        "name": "Anna's phone",
        "kind": "browser",
        "client": "203.0.113.7",
        "requested_at": None,
        "expires_in": None,
    }
    assert approved["result"] == {"status": "approved", "name": "Anna's phone"}
    body = claimed.json()
    assert body["status"] == "approved" and "token" not in body
    set_cookie = claimed.headers["set-cookie"]
    for attribute in ("HttpOnly", "Secure", "SameSite=strict", "Path=/", "Max-Age=34560000"):
        assert attribute.lower() in set_cookie.lower()
    assert again.json() == body and _cookie(again) == cookie
    assert signed_in.json()["device"] == body["device"]
    assert signed_in.json()["principal"] == "device"
    (device,) = listed["result"]["devices"]
    assert device["current"] is True and device["approved_by"] == "host"
    assert device["via"] == "pairing"
    assert device["last_client"] == "203.0.113.7"
    assert shadowed.json()["authenticated"] is False
    assert "set-cookie" not in shadowed.headers
    stored = json.loads((tmp_path / "data-0" / "access" / "devices.json").read_text("utf-8"))
    assert cookie not in json.dumps(stored)


def test_a_denied_or_unknown_pairing_ends_without_a_device(served: Any) -> None:
    _app, client = served(_authenticated())
    started = _pair(client, kind="desktop")

    unknown = _rpc(client, "devices.approve", {"code": "2222-2222"})
    denied = _rpc(client, "devices.deny", {"code": started["code"]})
    polled = _poll(client, started).json()
    forged = client.post(
        "/api/auth/pair/poll",
        json={"request_id": started["request_id"], "poll_secret": "guess"},
        headers=_INTERNET,
    ).json()
    malformed = client.post(
        "/api/auth/pair/poll",
        content=json.dumps({"request_id": started["request_id"], "poll_secret": "\ud800"}),
        headers={**_INTERNET, "Content-Type": "application/json"},
    )

    assert unknown["error"]["code"] == "pairing_not_found"
    assert denied["result"]["status"] == "denied"
    assert polled == {"status": "denied"}
    assert forged == {"status": "expired"}
    assert malformed.status_code == 400
    assert malformed.json()["error"]["code"] == "invalid_request"
    assert _rpc(client, "devices.list")["result"]["devices"] == []


def test_native_devices_get_their_token_once_and_lose_it_with_their_approvals_when_revoked(
    served: Any,
) -> None:
    _app, client = served(_authenticated())
    device_id, token = _paired_token(client)
    bearer = {**_INTERNET, "Authorization": f"Bearer {token}"}
    other_id, _other = _paired_token(client, kind="desktop")
    waiting = _pair(client, kind="cli")

    admitted = client.get(_PROTECTED, headers=bearer)
    renamed = _rpc(client, "devices.rename", {"id": device_id, "name": "Laptop"}, **bearer)
    approved = _rpc(client, "devices.approve", {"code": waiting["code"]}, **bearer)
    ticket = _rpc(client, "auth.login_ticket", **bearer)["result"]
    others = _rpc(client, "devices.revoke_others", **bearer)
    logged_out = _rpc(client, "devices.logout", **bearer)
    refused = client.get(_PROTECTED, headers=bearer)
    host_logout = _rpc(client, "devices.logout")
    unclaimed = _poll(client, waiting).json()
    redeemed = client.get(ticket["path"], headers=_INTERNET, follow_redirects=False)

    assert admitted.status_code == 404
    assert renamed["result"]["device"]["name"] == "Laptop"
    assert approved["result"]["status"] == "approved"
    assert others["result"] == {"revoked": [other_id]}
    assert logged_out["result"] == {"revoked": [device_id]}
    assert refused.status_code == 401
    assert host_logout["error"]["code"] == "invalid_request"
    # What the revoked device approved but nobody claimed yet goes with it.
    assert unclaimed == {"status": "denied"}
    assert redeemed.status_code == 400
    assert _rpc(client, "devices.list")["result"]["devices"] == []


def test_a_login_ticket_signs_in_one_browser_once_and_lands_where_it_asked(served: Any) -> None:
    _app, client = served(_authenticated())
    landing = "/?accessor=desktop&open_agent=main&open_session=s_1%2Fx"
    dead = "__Host-vbot_device=vbt_dev_AAAAAAAAAAAAAAAA_" + "x" * 43

    issued = _rpc(client, "auth.login_ticket", {"name": "Desktop browser", "next": landing})
    # A browser that still sends a dead cookie gets a new device, not a cleared cookie.
    redeemed = client.get(
        issued["result"]["path"], headers={**_INTERNET, "Cookie": dead}, follow_redirects=False
    )
    reused = client.get(issued["result"]["path"], headers=_INTERNET, follow_redirects=False)
    signed_in = {**_INTERNET, "Cookie": f"__Host-vbot_device={_cookie(redeemed)}"}
    status = client.get("/api/auth/status", headers=signed_in)
    # A browser that is signed in already keeps its device.
    again = _rpc(client, "auth.login_ticket", {"name": "Another window"})["result"]
    kept = client.get(again["path"], headers=signed_in, follow_redirects=False)
    rejected = {
        value: _rpc(client, "auth.login_ticket", {"next": value})["error"]["code"]
        for value in (
            "https://attacker.example/",
            "//attacker.example",
            "/\\attacker.example",
            "/a//b",
            "/#fragment",
            "?a=b",
            "/a b",
            "/a\n",
            "/caf\u00e9",
            "/%zz",
            "/auth/ticket/other",
            "/" + "a" * 2048,
            5,
        )
    }

    issued_result = issued["result"]
    assert issued_result == {
        "ticket": issued_result["ticket"],
        "path": f"/auth/ticket/{issued_result['ticket']}",
        "expires_in": 60,
    }
    assert redeemed.status_code == 303 and redeemed.headers["location"] == landing
    assert redeemed.headers.get_list("set-cookie") == [redeemed.headers["set-cookie"]]
    assert redeemed.headers["cache-control"] == "no-store"
    assert redeemed.headers["referrer-policy"] == "no-referrer"
    assert reused.status_code == 400 and "not valid" in reused.text
    assert status.json()["device"]["name"] == "Desktop browser"
    assert kept.status_code == 303 and kept.headers["location"] == "/"
    assert "set-cookie" not in kept.headers
    (device,) = _rpc(client, "devices.list")["result"]["devices"]
    assert (device["via"], device["approved_by"]) == ("ticket", "host")
    assert set(rejected.values()) == {"invalid_request"}


def test_browsers_signed_in_by_ticket_go_with_the_device_that_issued_it(served: Any) -> None:
    app, client = served(_authenticated())

    def ticket_browser(**headers: str) -> tuple[str, dict[str, str]]:
        """Sign a browser in with a ticket of ``headers``' principal; returns id and headers."""
        issued = _rpc(client, "auth.login_ticket", **headers)["result"]
        redeemed = client.get(issued["path"], headers=_INTERNET, follow_redirects=False)
        browser = {**_INTERNET, "Cookie": f"__Host-vbot_device={_cookie(redeemed)}"}
        return client.get("/api/auth/status", headers=browser).json()["device"]["id"], browser

    def device_changes() -> int:
        return sum(
            event["type"] == "resource_changed" and event["payload"] == {"kind": "devices"}
            for event in app.state.event_bus.events
        )

    desktop_id, desktop_token = _paired_token(client, kind="desktop")
    desktop = {**_INTERNET, "Authorization": f"Bearer {desktop_token}"}
    window_id, window = ticket_browser(**desktop)
    nested_id, _nested = ticket_browser(**window)
    waiting = _pair(client, kind="cli")
    assert _rpc(client, "devices.approve", {"code": waiting["code"]}, **desktop)["ok"]
    approved_id = _poll(client, waiting).json()["device"]["id"]
    changes = device_changes()

    revoked = _rpc(client, "devices.revoke", {"id": desktop_id})
    announced = device_changes() - changes
    remaining = [device["id"] for device in _rpc(client, "devices.list")["result"]["devices"]]
    second_id, second_token = _paired_token(client, kind="desktop")
    second_window_id, second_window = ticket_browser(
        **_INTERNET, Authorization=f"Bearer {second_token}"
    )
    others = _rpc(client, "devices.revoke_others", **second_window)
    current = client.get("/api/auth/status", headers=second_window).json()["device"]

    # A pairing the revoked device approved and its device claimed stays.
    assert revoked["result"] == {"revoked": [desktop_id, window_id, nested_id]}
    assert announced == 1
    assert remaining == [approved_id]
    # Signing out every other device keeps the calling one, also when one of them signed it in.
    assert sorted(others["result"]["revoked"]) == sorted([approved_id, second_id])
    assert current["id"] == second_window_id


@pytest.mark.parametrize("cancel_revocation", [False, True])
def test_ticket_redemption_in_progress_is_included_in_issuer_revocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_revocation: bool
) -> None:
    async def scenario() -> None:
        service = AccessService(_authenticated(), local_token=None)
        await service.start(tmp_path)
        issuer_id, issuer_token = await _service_device(service)
        facts = service.inspect(_internet_scope())
        principal = service.authenticate(
            [(b"authorization", f"Bearer {issuer_token}".encode())], facts
        ).principal
        assert principal is not None
        ticket = service.issue_ticket(principal, "Ticket browser")["ticket"]
        loop = asyncio.get_running_loop()
        writing = asyncio.Event()
        revoking = asyncio.Event()
        release = threading.Event()
        write = DeviceStore._write

        def pause_ticket_write(store: DeviceStore, devices: Any) -> None:
            if any(device.via == "ticket" for device in devices.values()):
                loop.call_soon_threadsafe(writing.set)
                assert release.wait(2), "ticket write was not released"
            write(store, devices)

        monkeypatch.setattr(DeviceStore, "_write", pause_ticket_write)
        redemption = asyncio.create_task(service.redeem_ticket(ticket, facts))

        async def revoke() -> list[Any]:
            revoking.set()
            return await service.revoke_devices([issuer_id], Principal("host"))

        revocation = None
        try:
            await writing.wait()
            revocation = asyncio.create_task(revoke())
            await revoking.wait()
            if cancel_revocation:
                revocation.cancel()
            release.set()
            redeemed = await redemption
            assert redeemed is not None and redeemed.token is not None
            if cancel_revocation:
                with pytest.raises(asyncio.CancelledError):
                    await revocation
            else:
                removed = await revocation
                assert len(removed) == 2 and removed[0].id == issuer_id
            assert service.list_devices(Principal("host"))["devices"] == []
            assert (
                service.authenticate(
                    [(b"authorization", f"Bearer {redeemed.token}".encode())], facts
                ).principal
                is None
            )
        finally:
            release.set()
            await asyncio.gather(
                redemption,
                *([revocation] if revocation is not None else []),
                return_exceptions=True,
            )
            await service.close()

    asyncio.run(scenario())


def test_an_unavailable_device_store_answers_503_and_keeps_credentials(
    served: Any, tmp_path: Path
) -> None:
    store = tmp_path / "data-0" / "access" / "devices.json"
    store.parent.mkdir(parents=True)
    store.write_text("{not json", encoding="utf-8")
    _app, client = served(_authenticated())
    token = "vbt_dev_aaaaaaaaaaaaaaaa_" + "x" * 43
    bearer = {**_INTERNET, "Authorization": f"Bearer {token}"}
    cookie = {**_INTERNET, "Cookie": f"__Host-vbot_device={token}"}

    protected = client.get(_PROTECTED, headers=bearer)
    status = client.get("/api/auth/status", headers=cookie)
    ticket = client.get("/auth/ticket/" + "t" * 43, headers=cookie, follow_redirects=False)
    with (
        pytest.raises(WebSocketDisconnect) as closed,
        client.websocket_connect("/ws", headers=bearer) as websocket,
    ):
        websocket.receive_text()
    # Credentials that cannot be checked are no failed sign-ins: nothing throttles them.
    for _attempt in range(25):
        client.get(_PROTECTED, headers=bearer)
    later = client.get(_PROTECTED, headers=bearer)
    malformed = client.get(_PROTECTED, headers={**_INTERNET, "Authorization": "Bearer vbt_bad"})
    host = client.get(_PROTECTED, headers=_HOST)

    assert protected.status_code == 503
    assert protected.json()["error"]["code"] == "devices_unavailable"
    assert status.status_code == 503 and "set-cookie" not in status.headers
    assert ticket.status_code == 503 and "set-cookie" not in ticket.headers
    assert closed.value.code == 4503
    assert later.status_code == 503
    assert malformed.status_code == 401
    assert host.status_code == 404


def test_dead_device_cookies_are_cleared_and_repeated_failures_are_throttled(
    served: Any, caplog: pytest.LogCaptureFixture
) -> None:
    _app, client = served(_authenticated())
    dead = {**_INTERNET, "Cookie": "__Host-vbot_device=vbt_dev_AAAAAAAAAAAAAAAA_" + "x" * 43}

    first = client.get(_PROTECTED, headers=dead)
    for _attempt in range(19):
        client.get(_PROTECTED, headers=dead)
    throttled = client.get(_PROTECTED, headers=dead)
    host = client.get(_PROTECTED, headers=_HOST)

    assert first.status_code == 401
    assert first.headers["set-cookie"].startswith("__Host-vbot_device=;")
    assert throttled.status_code == 429 and int(throttled.headers["retry-after"]) > 0
    assert host.status_code == 404
    refusals = [r.getMessage() for r in caplog.records if "Access refused" in r.getMessage()]
    assert refusals == [
        "Access refused (reason=invalid_token client=203.0.113.7 path=/api/runs/...)"
    ]
    assert "x" * 43 not in caplog.text


def test_pairing_requests_are_rate_limited_per_client(tmp_path: Path) -> None:
    async def scenario() -> dict[str, list[int]]:
        service = AccessService(_authenticated(), local_token=None)
        await service.start(tmp_path)

        def starts(count: int, *clients: str) -> list[int]:
            answers: list[int] = []
            for attempt in range(count):
                client = clients[attempt % len(clients)]
                scope = _local_scope() if client == "local" else _internet_scope(client=client)
                try:
                    service.pair_start({"kind": "cli", "name": "x"}, service.inspect(scope))
                    answers.append(200)
                except AccessError as exc:
                    answers.append(exc.status)
            return answers

        answers = {
            "one_client": starts(11, "203.0.113.7"),
            # An IPv6 client counts by its /64: its addresses share one budget.
            "one_v6_network": starts(11, *(f"2001:db8:1:2::{n}" for n in range(1, 12))),
            "next_v6_network": starts(1, "2001:db8:1:3::1"),
            # The rest of the shared budget, then one more client.
            "everyone": starts(80, *(f"198.51.100.{n}" for n in range(8))),
            # This computer's own clients pair even when the shared budget is spent.
            "this_computer": starts(11, "local"),
        }
        # Choosing another loopback source cannot multiply the local budget.
        scope = {**_local_scope(), "client": ("127.0.0.2", 40000)}
        with pytest.raises(AccessError) as another_loopback:
            service.pair_start({"kind": "cli", "name": "x"}, service.inspect(scope))
        assert another_loopback.value.code == "rate_limited"
        await service.close()
        return answers

    answers = asyncio.run(scenario())

    assert answers["one_client"] == [200] * 10 + [429]
    assert answers["one_v6_network"] == [200] * 10 + [429]
    assert answers["next_v6_network"] == [200]
    assert answers["everyone"] == [200] * 79 + [429]
    assert answers["this_computer"] == [200] * 10 + [429]


def test_a_full_pairing_table_makes_room_at_the_expense_of_the_crowding_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_pairing, "MAX_PENDING_PAIRINGS", 3)
    monkeypatch.setattr(_pairing, "MAX_LOCAL_PENDING_PAIRINGS", 3)

    async def scenario() -> dict[str, Any]:
        service = AccessService(_authenticated(), local_token=None)
        await service.start(tmp_path)

        def start(client: str) -> dict[str, Any]:
            scope = _local_scope() if client == "local" else _internet_scope(client=client)
            return service.pair_start({"kind": "cli", "name": "x"}, service.inspect(scope))

        async def status(started: dict[str, Any]) -> str:
            payload, _cookie = await service.pair_poll(started, service.inspect(_local_scope()))
            return str(payload["status"])

        crowding = [start("203.0.113.7"), start("203.0.113.7")]
        other = start("198.51.100.4")
        newcomer = start("198.51.100.5")
        local = [start("local") for _attempt in range(3)]
        replacement = start("local")
        outcome: dict[str, Any] = {
            "crowding": [await status(started) for started in crowding],
            "other": await status(other),
            "newcomer": await status(newcomer),
            "local": [await status(started) for started in local],
            "local_replacement": await status(replacement),
        }
        for started in (crowding[1], other, newcomer):
            service.decide_pairing(started["code"], Principal("host"), approved=True)
        try:
            start("198.51.100.6")
        except AccessError as exc:
            outcome["approved_table"] = exc.status
        await service.close()
        return outcome

    assert asyncio.run(scenario()) == {
        "crowding": ["expired", "pending"],
        "other": "pending",
        "newcomer": "pending",
        # This computer's requests are not counted and never make room.
        "local": ["expired", "pending", "pending"],
        "local_replacement": "pending",
        # Approved requests wait for their device; nothing gives way.
        "approved_table": 429,
    }


def test_websockets_are_refused_with_close_codes_and_end_when_the_device_is_revoked(
    served: Any,
) -> None:
    _app, client = served(_authenticated())
    _lan_app, lan = served(access_config("lan"))
    device_id, token = _paired_token(client)

    with (
        pytest.raises(WebSocketDisconnect) as unauthenticated,
        client.websocket_connect("/ws", headers=_INTERNET) as websocket,
    ):
        websocket.receive_text()
    with (
        pytest.raises(WebSocketDisconnect) as foreign,
        lan.websocket_connect("/ws", headers={"x-test-peer": "203.0.113.7"}) as websocket,
    ):
        websocket.receive_text()
    # Some proxies forward a WebSocket upgrade as wss.
    with client.websocket_connect(
        "/ws", headers={**_INTERNET, "X-Forwarded-Proto": "wss", "Authorization": f"Bearer {token}"}
    ) as websocket:
        hello = websocket.receive_json()
        revoked = _rpc(client, "devices.revoke", {"id": device_id})
        with pytest.raises(WebSocketDisconnect) as ended:
            while True:
                websocket.receive_text()

    assert (unauthenticated.value.code, foreign.value.code) == (4401, 4403)
    assert hello["type"] == "connection_ready"
    assert revoked["result"] == {"revoked": [device_id]}
    assert ended.value.code == 4401


def test_only_the_rpc_request_itself_acts_for_its_principal(
    served: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client = served(_authenticated())
    seen: dict[str, Principal | None] = {}

    async def later() -> Principal | None:
        return current_principal()

    async def probe(_state: Any, _params: Any) -> dict[str, Any]:
        seen["request"] = current_principal()
        # Runs and other work a request starts inherit its context, not its principal.
        seen["spawned"] = await asyncio.get_running_loop().create_task(later())
        return {}

    monkeypatch.setitem(methods.METHODS, "test.principal", probe)
    answered = _rpc(client, "test.principal")
    direct = asyncio.run(dispatch_rpc(app.state, {"method": "devices.list", "params": {}}))

    assert answered == {"ok": True, "result": {}}
    assert seen == {"request": Principal("host"), "spawned": None}
    assert direct["error"]["code"] == "access_denied"


def _cookie(response: Any) -> str:
    match = re.match(r"__Host-vbot_device=([^;]+)", response.headers["set-cookie"])
    assert match is not None
    return match.group(1)


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def _write_webui_build(tmp_path: Path) -> Path:
    dist_dir = tmp_path / "webui" / "dist"
    (dist_dir / "assets").mkdir(parents=True)
    (dist_dir / "index.html").write_text('<div id="app"></div>', encoding="utf-8")
    (dist_dir / "assets" / "app.js").write_text("console.log('webui');", encoding="utf-8")
    return dist_dir


def _internet_scope(
    path: str = "/api/auth/pair/start",
    *headers: tuple[bytes, bytes],
    spec: str = "2.3",
    client: str = "203.0.113.7",
) -> dict[str, Any]:
    """An HTTP scope as uvicorn builds it for a browser behind the trusted proxy."""
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [
            (b"host", b"vbot.example.org"),
            (b"x-forwarded-for", client.encode()),
            (b"x-forwarded-proto", b"https"),
            *headers,
        ],
        "client": (_PROXY, 40000),
        "server": ("127.0.0.1", 8765),
    }


def _local_scope(path: str = "/api/auth/pair/start") -> dict[str, Any]:
    """An HTTP scope of a client on this computer, connected directly."""
    return {
        **_internet_scope(path),
        "headers": [(b"host", b"127.0.0.1:8765")],
        "client": ("127.0.0.1", 40000),
    }


async def _service_device(service: AccessService, kind: str = "cli") -> tuple[str, str]:
    """Pair a device through the service as the host approves it; returns id and token."""
    facts = service.inspect(_internet_scope("/api/auth/pair/start"))
    started = service.pair_start({"kind": kind, "name": "Laptop"}, facts)
    service.decide_pairing(started["code"], Principal("host"), approved=True)
    approved, _cookie_token = await service.pair_poll(started, facts)
    return approved["device"]["id"], approved["token"]


@pytest.mark.parametrize("spec", ["2.3", "2.4"])
def test_a_device_event_stream_ends_when_the_device_is_revoked(tmp_path: Path, spec: str) -> None:
    from core.utils.workers import settle_before_cancelling

    async def events() -> AsyncIterator[str]:
        yield "data: ready\n\n"
        await asyncio.Event().wait()

    answering = asyncio.Event()
    admission = asyncio.Lock()
    body_waiting = asyncio.Event()
    admission_waiting = asyncio.Event()
    durable_started = asyncio.Event()
    effects: list[str] = []

    async def durable_change() -> None:
        durable_started.set()
        await answering.wait()
        effects.append("durable")

    async def app(scope: Any, receive: Any, send: Any) -> None:
        if scope["path"].endswith("/events"):
            await StreamingResponse(events(), media_type="text/event-stream")(scope, receive, send)
            return
        if scope["path"].endswith("/body"):
            await receive()
            effects.append("body")
        elif scope["path"].endswith("/admission"):
            admission_waiting.set()
            async with admission:
                effects.append("admission")
        elif scope["path"].endswith("/durable"):
            await settle_before_cancelling(durable_change())
        # A slow answer: it starts only after the revocation.
        await answering.wait()
        await JSONResponse({"ok": True})(scope, receive, send)

    async def scenario() -> tuple[dict[str, list[dict[str, Any]]], list[asyncio.Task[None]], int]:
        service = AccessService(_authenticated(), local_token=None)
        await service.start(tmp_path)
        device_id, token = await _service_device(service)
        sent: dict[str, list[dict[str, Any]]] = {
            name: [] for name in ("stream", "answer", "body", "admission", "durable")
        }
        streaming = asyncio.Event()
        never = asyncio.Event()

        def sender(name: str) -> Any:
            async def send(message: MutableMapping[str, Any]) -> None:
                sent[name].append(dict(message))
                streaming.set()

            return send

        async def receive() -> dict[str, Any]:
            await never.wait()
            return {"type": "http.disconnect"}

        async def delayed_body() -> dict[str, Any]:
            body_waiting.set()
            try:
                await answering.wait()
            except asyncio.CancelledError:
                # A body arriving concurrently with revocation must not reach
                # dispatch, even if its reader settles before returning.
                await answering.wait()
            return {"type": "http.request", "body": b"{}", "more_body": False}

        def request(path: str, name: str) -> asyncio.Task[None]:
            bearer = (b"authorization", f"Bearer {token}".encode())
            scope = _internet_scope(path, bearer, spec=spec)
            guard = AccessGuard(app, service=service)
            return asyncio.create_task(
                guard(scope, delayed_body if name == "body" else receive, sender(name))
            )

        stream = request("/api/runs/run_1/events", "stream")
        answer = request("/api/runs/run_1", "answer")
        async with admission:
            body = request("/api/probe/body", "body")
            waiting = request("/api/probe/admission", "admission")
            durable = request("/api/probe/durable", "durable")
            await asyncio.gather(
                streaming.wait(),
                body_waiting.wait(),
                admission_waiting.wait(),
                durable_started.wait(),
            )
            open_requests = service.connections.count(device_id)
            await service.revoke_devices([device_id], Principal("host"))
        answering.set()
        tasks = [stream, answer, body, waiting, durable]
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
        await service.close()
        return sent, tasks, open_requests

    sent, tasks, open_requests = asyncio.run(scenario())

    assert open_requests == 5
    # The stream ends cleanly; the request task carries no leftover cancellation.
    assert sent["stream"][0]["type"] == "http.response.start"
    assert sent["stream"][-1] == {"type": "http.response.body", "body": b"", "more_body": False}
    assert all(not task.cancelled() and task.cancelling() == 0 for task in tasks)
    assert effects == ["durable"]
    # An answer that starts after the revocation is refused instead.
    for name in ("answer", "body", "admission", "durable"):
        start, body = sent[name]
        assert start["status"] == 401
        assert json.loads(body["body"])["error"]["code"] == "auth_required"


def test_pairing_requests_and_login_tickets_expire(tmp_path: Path) -> None:
    clock = [1_000.0]
    host = Principal("host")

    async def scenario() -> dict[str, Any]:
        service = AccessService(_authenticated(), local_token=None, clock=lambda: clock[0])
        await service.start(tmp_path)
        facts = service.inspect(_internet_scope("/api/auth/pair/start"))
        started = service.pair_start({"kind": "browser", "name": "Phone"}, facts)
        native = service.pair_start({"kind": "cli", "name": "Laptop"}, facts)
        service.decide_pairing(native["code"], host, approved=True)
        # Concurrent polls share one claim: one device, one token.
        claimed, concurrent = await asyncio.gather(
            service.pair_poll(native, facts), service.pair_poll(native, facts)
        )
        ticket = service.issue_ticket(host, "Desktop browser")["ticket"]
        kept = service.issue_ticket(host, None)["ticket"]
        clock[0] += 59
        outcomes: dict[str, Any] = {
            "ticket_in_time": await service.redeem_ticket(kept, facts) is not None,
            "ticket_reused": await service.redeem_ticket(kept, facts),
            "pending_in_time": (await service.pair_poll(started, facts))[0]["status"],
            "claim_shared": claimed == concurrent and "token" in claimed[0],
            "claim_repeated": await service.pair_poll(native, facts) == claimed,
        }
        clock[0] += 2
        outcomes["ticket_late"] = await service.redeem_ticket(ticket, facts)
        outcomes["claim_late"] = (await service.pair_poll(native, facts))[0]
        clock[0] += 540
        outcomes["pairing_late"] = (await service.pair_poll(started, facts))[0]
        with pytest.raises(AccessError) as late_code:
            service.pending_pairing(started["code"], host)
        outcomes["approve_late"] = late_code.value.code
        outcomes["devices"] = len(service.list_devices(host)["devices"])
        await service.close()
        return outcomes

    assert asyncio.run(scenario()) == {
        "ticket_in_time": True,
        "ticket_reused": None,
        "pending_in_time": "pending",
        "claim_shared": True,
        "claim_repeated": True,
        "ticket_late": None,
        # The claimed answer, with its token, is kept for the grace period only.
        "claim_late": {"status": "expired"},
        "pairing_late": {"status": "expired"},
        "approve_late": "pairing_not_found",
        "devices": 2,
    }


def test_devices_idle_for_ninety_days_are_removed_at_start(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="vbot.server.access")
    paired_at = datetime(2026, 1, 1, tzinfo=UTC)
    now = [paired_at]
    host = Principal("host")

    def service() -> AccessService:
        return AccessService(_authenticated(), local_token=None, utc_now=lambda: now[0])

    async def scenario() -> tuple[str | None, list[str], str, str]:
        first = service()
        await first.start(tmp_path)
        idle_id, _idle_token = await _service_device(first)
        used_id, used_token = await _service_device(first)
        await first.close()
        now[0] = paired_at + timedelta(days=89)
        second = service()
        await second.start(tmp_path)
        facts = second.inspect(_internet_scope("/api/runs"))
        signed_in = second.authenticate(
            [(b"authorization", f"Bearer {used_token}".encode())], facts
        )
        await second.close()
        now[0] = paired_at + timedelta(days=91)
        third = service()
        await third.start(tmp_path)
        remaining = [device["id"] for device in third.list_devices(host)["devices"]]
        await third.close()
        actor = signed_in.principal.actor if signed_in.principal else None
        return actor, remaining, idle_id, used_id

    signed_in, remaining, idle_id, used_id = asyncio.run(scenario())

    # Use moves the expiry on; the device left idle is gone after a restart.
    assert signed_in == used_id
    assert remaining == [used_id]
    assert f"Device expired (device={idle_id} idle_days=90)" in caplog.text


def test_rate_limits_and_the_refusal_log_stay_bounded_under_a_flood(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = [0.0]
    window = SlidingWindow(3, 60, clock=lambda: clock[0], max_keys=2)
    flushes: list[Any] = []
    refusals = RefusalLog(
        logging.getLogger("vbot.test.refusals"),
        interval=60,
        schedule=lambda _delay, flush: flushes.append(flush),
    )

    for _attempt in range(1000):
        window.record("flood")
    kept = len(window._events["flood"])
    blocked = window.blocked("flood")
    window.record("second")
    window.record("third")
    for n in range(50):
        refusals.refused("invalid_token", client=f"203.0.113.{n}", detail="path=/api/rpc")
    flushes.pop()()

    # One key keeps only the events that decide its limit; the oldest key goes first.
    assert kept == 3
    assert blocked == 60 and window.blocked("flood") is None
    # A summary names a bounded number of clients, however many there were.
    summary = caplog.records[-1].getMessage()
    assert summary.startswith("Access refused repeatedly (reason=invalid_token count=49 clients=")
    assert summary.count("203.0.113.") == 10
    assert summary.endswith(",... interval=60s)")
