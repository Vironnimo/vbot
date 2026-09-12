"""Tests for app http."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient  # type: ignore[import-not-found]

from core.runtime import Runtime
from core.utils.config import Config
from server.app import (
    JSON_REQUEST_BODY_MAX_BYTES,
    WEBUI_DOCUMENT_CACHE_HEADERS,
    create_app,
)


def test_webui_serving_keeps_api_routes_precedence(monkeypatch, tmp_path: Path) -> None:
    import server.app as server_app

    dist_dir = _write_webui_build(tmp_path)
    monkeypatch.setattr(server_app, "WEBUI_DIST_DIR", dist_dir)
    app = create_app(runtime=Runtime(Config(data_dir=tmp_path / "data")))

    with TestClient(app) as client:
        health_response = client.get("/health")
        missing_sse_response = client.get("/api/runs/missing/events")
        rpc_response = client.post("/api/rpc", json={"method": "unknown.method"})

    assert health_response.status_code == 200
    assert health_response.json() == {"status": "ok"}
    assert missing_sse_response.status_code == 404
    assert rpc_response.status_code == 200
    assert rpc_response.json()["ok"] is False


@pytest.mark.parametrize(
    "origin",
    [
        "https://attacker.example",
        "http://testserver:8420",
        "https://testserver",
        "null",
    ],
)
def test_http_transport_rejects_non_same_origin_browser_requests(
    tmp_path: Path,
    origin: str,
) -> None:
    app = create_app(runtime=Runtime(Config(data_dir=tmp_path / "data")))

    with TestClient(app) as client:
        response = client.get("/health", headers={"origin": origin})

    assert response.status_code == 403


def test_http_transport_allows_same_origin_and_non_browser_requests(tmp_path: Path) -> None:
    app = create_app(runtime=Runtime(Config(data_dir=tmp_path / "data")))

    with TestClient(app) as client:
        same_origin = client.get("/health", headers={"origin": "http://127.0.0.1:8420"})
        without_origin = client.get("/health")

    assert same_origin.status_code == 200
    assert without_origin.status_code == 200


def test_http_transport_rejects_host_header_origin_rebinding(tmp_path: Path) -> None:
    app = create_app(runtime=Runtime(Config(data_dir=tmp_path / "data")))

    with TestClient(app) as client:
        response = client.get(
            "/health",
            headers={"origin": "http://attacker.example", "host": "attacker.example"},
        )

    assert response.status_code == 403


@pytest.mark.parametrize(
    ("listen_host", "request_host", "origin"),
    [
        ("0.0.0.0", "192.168.10.25:8420", "http://192.168.10.25:8420"),
        ("::", "[fd00::25]:8420", "http://[fd00::25]:8420"),
    ],
)
def test_wildcard_bind_allows_same_origin_ip_browser_requests(
    tmp_path: Path,
    listen_host: str,
    request_host: str,
    origin: str,
) -> None:
    app = create_app(
        runtime=Runtime(Config(data_dir=tmp_path / "data")),
        server_bind={
            "listen_host": listen_host,
            "listen_port": 8420,
            "port_source": "cli",
        },
    )

    with TestClient(app) as client:
        response = client.get(
            "/health",
            headers={"host": request_host, "origin": origin},
        )

    assert response.status_code == 200


@pytest.mark.parametrize(
    ("request_host", "origin"),
    [
        ("attacker.example:8420", "http://attacker.example:8420"),
        ("192.168.10.25:8420", "http://192.168.10.99:8420"),
    ],
)
def test_wildcard_bind_rejects_rebinding_and_cross_ip_origins(
    tmp_path: Path,
    request_host: str,
    origin: str,
) -> None:
    app = create_app(
        runtime=Runtime(Config(data_dir=tmp_path / "data")),
        server_bind={
            "listen_host": "0.0.0.0",
            "listen_port": 8420,
            "port_source": "cli",
        },
    )

    with TestClient(app) as client:
        response = client.get(
            "/health",
            headers={"host": request_host, "origin": origin},
        )

    assert response.status_code == 403


def test_rpc_rejects_oversized_body_before_dispatch(monkeypatch, tmp_path: Path) -> None:
    import server.app as server_app

    dispatch_calls: list[object] = []

    async def record_dispatch(_state: Any, payload: object) -> dict[str, object]:
        dispatch_calls.append(payload)
        return {"ok": True, "result": {}}

    monkeypatch.setattr(server_app, "dispatch_rpc", record_dispatch)
    app = create_app(runtime=Runtime(Config(data_dir=tmp_path / "data")))

    with TestClient(app) as client:
        response = client.post(
            "/api/rpc",
            content=b"x" * (JSON_REQUEST_BODY_MAX_BYTES + 1),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 413
    assert dispatch_calls == []


def test_speech_synthesis_rejects_oversized_body(tmp_path: Path) -> None:
    app = create_app(runtime=Runtime(Config(data_dir=tmp_path / "data")))

    with TestClient(app) as client:
        response = client.post(
            "/api/speech/synthesize",
            content=b"x" * (JSON_REQUEST_BODY_MAX_BYTES + 1),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 413


def test_rpc_rejects_non_json_media_type_before_dispatch(monkeypatch, tmp_path: Path) -> None:
    import server.app as server_app

    dispatch_calls: list[object] = []

    async def record_dispatch(_state: Any, payload: object) -> dict[str, object]:
        dispatch_calls.append(payload)
        return {"ok": True, "result": {}}

    monkeypatch.setattr(server_app, "dispatch_rpc", record_dispatch)
    app = create_app(runtime=Runtime(Config(data_dir=tmp_path / "data")))

    with TestClient(app) as client:
        cross_origin_response = client.post(
            "/api/rpc",
            content='{"method":"terminal.start"}',
            headers={
                "content-type": "text/plain",
                "origin": "https://attacker.example",
            },
        )
        wrong_media_type_response = client.post(
            "/api/rpc",
            content='{"method":"terminal.start"}',
            headers={
                "content-type": "text/plain",
                "origin": "http://127.0.0.1:8420",
            },
        )

    assert cross_origin_response.status_code == 403
    assert wrong_media_type_response.status_code == 415
    assert dispatch_calls == []


@pytest.mark.parametrize("content", ["{", b"\xff"])
def test_rpc_endpoint_returns_error_envelope_for_malformed_json(
    tmp_path: Path, content: str | bytes
) -> None:
    app = create_app(runtime=Runtime(Config(data_dir=tmp_path / "data")))

    with TestClient(app) as client:
        response = client.post(
            "/api/rpc",
            content=content,
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "ok": False,
        "error": {
            "code": "invalid_request",
            "message": "RPC request body must be valid JSON",
        },
    }


def test_webui_serves_index_static_assets_and_spa_fallback(monkeypatch, tmp_path: Path) -> None:
    import server.app as server_app

    dist_dir = _write_webui_build(tmp_path)
    monkeypatch.setattr(server_app, "WEBUI_DIST_DIR", dist_dir)
    app = create_app(runtime=Runtime(Config(data_dir=tmp_path / "data")))

    with TestClient(app) as client:
        index_response = client.get("/")
        asset_response = client.get("/assets/app.js")
        fallback_response = client.get("/agents/main")

    assert index_response.status_code == 200
    assert '<div id="app"></div>' in index_response.text
    assert index_response.headers["cache-control"] == WEBUI_DOCUMENT_CACHE_HEADERS["Cache-Control"]
    assert asset_response.status_code == 200
    assert asset_response.text == "console.log('webui');"
    assert fallback_response.status_code == 200
    assert '<script type="module" src="/assets/app.js"></script>' in fallback_response.text
    assert (
        fallback_response.headers["cache-control"] == WEBUI_DOCUMENT_CACHE_HEADERS["Cache-Control"]
    )


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


@pytest.mark.parametrize("error_type", [RuntimeError, KeyError])
def test_rpc_unexpected_failure_preserves_json_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error_type: type[Exception],
) -> None:
    import server.app as server_app
    from server.rpc.dispatcher import dispatch_rpc
    from tests.server.test_rpc import StubAdapter, StubRuntime

    def fail(_state: Any, _params: Any) -> Any:
        raise error_type("test-owned-private-detail")

    async def dispatch(state: Any, request: Any) -> Any:
        return await dispatch_rpc(state, request, {"test.fail": fail})

    monkeypatch.setattr(server_app, "dispatch_rpc", dispatch)
    app = create_app(runtime=cast(Any, StubRuntime(tmp_path, StubAdapter())))
    with TestClient(app) as client, caplog.at_level(logging.ERROR):
        response = client.post("/api/rpc", json={"method": "test.fail"})
    assert response.status_code == 500
    payload = response.json()
    assert payload["ok"] is False
    assert payload["error"]["code"] == "internal_error"
    assert payload["error"]["message"]
    assert "test-owned-private-detail" not in response.text
    assert any(
        record.exc_info and record.name.endswith("rpc.dispatcher") for record in caplog.records
    )
