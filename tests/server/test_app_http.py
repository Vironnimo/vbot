"""HTTP edge of the server app: origin guard, RPC body guard, OAuth callback and WebUI serving."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient  # type: ignore[import-not-found]

import server.app as server_app
from server.app import JSON_REQUEST_BODY_MAX_BYTES, WEBUI_DOCUMENT_CACHE_HEADERS, create_app
from server.rpc.dispatcher import dispatch_rpc
from tests.server.app_test_support import ServerStubRuntime

_SAME_ORIGIN = "http://127.0.0.1:8420"


def test_browser_origin_guard_admits_only_the_served_origin(tmp_path: Path) -> None:
    app = create_app(runtime=ServerStubRuntime(tmp_path))
    foreign_origins = [
        "https://attacker.example",
        "http://testserver:8420",
        "https://testserver",
        "null",
    ]

    with TestClient(app) as client:
        foreign = [client.get("/health", headers={"origin": origin}) for origin in foreign_origins]
        # A rebinding page cannot pass by making Host match its own Origin.
        rebinding = client.get(
            "/health", headers={"origin": "http://attacker.example", "host": "attacker.example"}
        )
        same_origin = client.get("/health", headers={"origin": _SAME_ORIGIN})
        without_origin = client.get("/health")

    assert [response.status_code for response in [*foreign, rebinding]] == [403] * 5
    assert same_origin.json() == without_origin.json() == {"status": "ok"}


@pytest.mark.parametrize(
    ("listen_host", "requests"),
    [
        (
            "0.0.0.0",
            [
                ("192.168.10.25:8420", "http://192.168.10.25:8420", 200),
                ("attacker.example:8420", "http://attacker.example:8420", 403),
                ("192.168.10.25:8420", "http://192.168.10.99:8420", 403),
            ],
        ),
        ("::", [("[fd00::25]:8420", "http://[fd00::25]:8420", 200)]),
    ],
)
def test_wildcard_bind_admits_same_origin_ip_requests_only(
    tmp_path: Path, listen_host: str, requests: list[tuple[str, str, int]]
) -> None:
    app = create_app(
        runtime=ServerStubRuntime(tmp_path),
        server_bind={"listen_host": listen_host, "listen_port": 8420, "port_source": "cli"},
    )

    with TestClient(app) as client:
        observed = [
            (host, origin, client.get("/health", headers={"host": host, "origin": origin}))
            for host, origin, _status in requests
        ]

    assert [(host, origin, response.status_code) for host, origin, response in observed] == (
        requests
    )


class _Redirects:
    """Records what the server binds and delivers; only ``pending`` is awaited."""

    def __init__(self) -> None:
        self.callback_url: str | None = "unbound"
        self.delivered: list[dict[str, str]] = []

    def bind(self, callback_url: str | None) -> None:
        self.callback_url = callback_url

    def deliver(self, params: dict[str, str]) -> bool:
        self.delivered.append(params)
        return params.get("state") == "pending"


@pytest.mark.parametrize(
    ("listen_host", "callback_url"),
    [
        ("0.0.0.0", "http://127.0.0.1:8421/api/oauth/callback"),
        ("::", "http://[::1]:8421/api/oauth/callback"),
        ("192.168.1.5", None),
    ],
)
def test_oauth_callback_completes_only_the_awaited_sign_in(
    tmp_path: Path, listen_host: str, callback_url: str | None
) -> None:
    redirects = _Redirects()
    app = create_app(
        runtime=ServerStubRuntime(tmp_path, oauth_redirects=redirects),
        server_bind={"listen_host": listen_host, "listen_port": 8421, "port_source": "cli"},
    )

    with TestClient(app) as client:
        received = client.get("/api/oauth/callback?code=abc&state=pending")
        unknown = client.get("/api/oauth/callback?code=abc&state=other")
        # A repeated parameter is ambiguous and never reaches a sign-in.
        repeated = client.get("/api/oauth/callback?code=abc&state=pending&state=other")

    assert redirects.callback_url == callback_url
    assert received.status_code == 200
    assert "You can close this tab" in received.text
    assert [unknown.status_code, repeated.status_code] == [400, 400]
    assert "Start the sign-in again" in unknown.text
    assert redirects.delivered == [
        {"code": "abc", "state": "pending"},
        {"code": "abc", "state": "other"},
    ]
    # The page's address carries the code: it is never cached, framed or referred onward.
    assert received.headers["cache-control"] == "no-store"
    assert received.headers["referrer-policy"] == "no-referrer"
    assert received.headers["content-security-policy"].startswith("default-src 'none'")


def test_rpc_endpoint_rejects_unsafe_bodies_before_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dispatched: list[object] = []

    async def record_dispatch(_state: Any, payload: object) -> dict[str, object]:
        dispatched.append(payload)
        return {"ok": True, "result": {}}

    monkeypatch.setattr(server_app, "dispatch_rpc", record_dispatch)
    app = create_app(runtime=ServerStubRuntime(tmp_path))
    json_type = {"content-type": "application/json"}
    body = '{"method":"terminal.start"}'

    with TestClient(app) as client:
        oversized = client.post(
            "/api/rpc", content=b"x" * (JSON_REQUEST_BODY_MAX_BYTES + 1), headers=json_type
        )
        # A cross-origin simple request is refused before its media type matters.
        cross_origin = client.post(
            "/api/rpc",
            content=body,
            headers={"content-type": "text/plain", "origin": "https://attacker.example"},
        )
        wrong_media_type = client.post(
            "/api/rpc", content=body, headers={"content-type": "text/plain", "origin": _SAME_ORIGIN}
        )
        malformed = [
            client.post("/api/rpc", content=content, headers=json_type)
            for content in ("{", b"\xff")
        ]
        accepted = client.post("/api/rpc", json={"method": "agent.list"})

    assert [oversized.status_code, cross_origin.status_code, wrong_media_type.status_code] == [
        413,
        403,
        415,
    ]
    for response in malformed:
        assert response.status_code == 200
        assert response.json() == {
            "ok": False,
            "error": {"code": "invalid_request", "message": "RPC request body must be valid JSON"},
        }
    assert accepted.json() == {"ok": True, "result": {}}
    assert dispatched == [{"method": "agent.list"}]


def test_rpc_unexpected_failure_preserves_json_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def fail(_state: Any, _params: Any) -> Any:
        raise KeyError("test-owned-private-detail")

    async def dispatch(state: Any, request: Any) -> Any:
        return await dispatch_rpc(state, request, {"test.fail": fail})

    monkeypatch.setattr(server_app, "dispatch_rpc", dispatch)
    app = create_app(runtime=ServerStubRuntime(tmp_path))

    with TestClient(app) as client, caplog.at_level(logging.ERROR):
        response = client.post("/api/rpc", json={"method": "test.fail"})

    assert response.status_code == 500
    assert response.json() == {
        "ok": False,
        "error": {"code": "internal_error", "message": "Internal server error"},
    }
    assert "test-owned-private-detail" not in response.text
    assert any(
        record.exc_info and record.name.endswith("rpc.dispatcher") for record in caplog.records
    )


def test_webui_build_serves_the_app_shell_without_shadowing_server_routes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(server_app, "WEBUI_DIST_DIR", _write_webui_build(tmp_path))
    app = create_app(runtime=ServerStubRuntime(tmp_path / "data"))

    with TestClient(app) as client:
        index, asset, fallback = [
            client.get(path) for path in ("/", "/assets/app.js", "/agents/main")
        ]
        health = client.get("/health")
        missing_run = client.get("/api/runs/missing/events")
        unknown_method = client.post("/api/rpc", json={"method": "unknown.method"})
        # Unmatched server paths are never answered with the WebUI document.
        reserved = [
            client.get(path) for path in ("/api/unknown", "/ws", "/ws/logs", "/ws/terminals/term-1")
        ]

    document_cache = WEBUI_DOCUMENT_CACHE_HEADERS["Cache-Control"]
    assert '<div id="app"></div>' in index.text
    assert index.headers["cache-control"] == fallback.headers["cache-control"] == document_cache
    assert asset.text == "console.log('webui');"
    assert '<script type="module" src="/assets/app.js"></script>' in fallback.text
    assert health.json() == {"status": "ok"}
    assert missing_run.status_code == 404
    assert unknown_method.json()["error"]["code"] == "method_not_found"
    assert [(response.status_code, response.json()) for response in reserved] == [
        (404, {"detail": "Not Found"})
    ] * 4

    monkeypatch.setattr(server_app, "WEBUI_DIST_DIR", tmp_path / "missing-dist")
    with TestClient(create_app(runtime=ServerStubRuntime(tmp_path / "data"))) as client:
        assert client.get("/").status_code == 404


def _write_webui_build(tmp_path: Path) -> Path:
    dist_dir = tmp_path / "webui" / "dist"
    assets_dir = dist_dir / "assets"
    assets_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text(
        '<div id="app"></div><script type="module" src="/assets/app.js"></script>',
        encoding="utf-8",
    )
    (assets_dir / "app.js").write_text("console.log('webui');", encoding="utf-8")
    return dist_dir
