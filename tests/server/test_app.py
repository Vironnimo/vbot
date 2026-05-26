"""Tests for the server FastAPI app factory."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient  # type: ignore[import-not-found]

from core.chat import ChatLoop
from core.runs import ChatRunManager
from core.runtime import Runtime
from core.utils.config import Config
from server.app import ServerEventBus, create_app


def test_create_app_does_not_mount_webui_when_build_is_absent(monkeypatch, tmp_path: Path) -> None:
    import server.app as server_app

    monkeypatch.setattr(server_app, "WEBUI_DIST_DIR", tmp_path / "missing-dist")
    app = create_app(runtime=Runtime(Config(data_dir=tmp_path / "data")))

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 404


def test_create_app_wires_runtime_services_into_state(tmp_path: Path) -> None:
    runtime = Runtime(Config(data_dir=tmp_path / "data"))
    app = create_app(runtime=runtime)

    with TestClient(app) as client:
        response = client.get("/health")

        assert response.json() == {"status": "ok"}
        assert app.state.runtime is runtime
        assert isinstance(app.state.chat_runs, ChatRunManager)
        assert isinstance(app.state.chat_loop, ChatLoop)
        assert isinstance(app.state.event_bus, ServerEventBus)
        assert isinstance(app.state.agent_delete_lock, asyncio.Lock)
        assert app.state.server_bind == {
            "listen_host": "127.0.0.1",
            "listen_port": 8420,
            "port_source": "default",
        }
        assert runtime.chat_runs is app.state.chat_runs
        assert runtime.chat_run_manager is app.state.chat_runs
        assert runtime.trigger_service is not None

    assert runtime.logger is not None


def test_create_app_wires_runtime_owned_chat_runs_for_lazy_stub_runtime(tmp_path: Path) -> None:
    runtime = _LazyChatRunRuntime(tmp_path)
    app = create_app(runtime=cast(Any, runtime))

    with TestClient(app):
        assert isinstance(app.state.chat_runs, ChatRunManager)
        assert runtime.chat_runs is app.state.chat_runs


def test_create_app_falls_back_to_chat_runs_for_stub_runtime_error(tmp_path: Path) -> None:
    runtime = _UnavailableChatRunRuntime(tmp_path)
    app = create_app(runtime=cast(Any, runtime))

    with TestClient(app):
        assert isinstance(app.state.chat_runs, ChatRunManager)
        assert runtime.chat_runs is app.state.chat_runs


def test_runtime_chat_runs_reraises_runtime_lifecycle_error(tmp_path: Path) -> None:
    import server.app as server_app

    runtime = Runtime(Config(data_dir=tmp_path / "data"))

    try:
        server_app._runtime_chat_runs(runtime)
    except RuntimeError as exc:
        assert "Runtime not started" in str(exc)
    else:
        raise AssertionError("real Runtime lifecycle error should not be swallowed")


def test_create_app_uses_explicit_server_bind_state(tmp_path: Path) -> None:
    runtime = Runtime(Config(data_dir=tmp_path / "data"))
    app = create_app(
        runtime=runtime,
        server_bind={"listen_host": "0.0.0.0", "listen_port": 9100, "port_source": "cli"},
    )

    with TestClient(app):
        assert app.state.server_bind == {
            "listen_host": "0.0.0.0",
            "listen_port": 9100,
            "port_source": "cli",
        }


def test_create_app_derives_server_bind_from_environment_port(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("VBOT_SERVER_PORT", "8600")
    app = create_app(runtime=Runtime(Config(data_dir=tmp_path / "data")))

    with TestClient(app):
        assert app.state.server_bind == {
            "listen_host": "127.0.0.1",
            "listen_port": 8600,
            "port_source": "VBOT_SERVER_PORT",
        }


def test_create_app_derives_server_bind_from_settings_file(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "settings.json").write_text('{"server_port": 8500}', encoding="utf-8")
    app = create_app(runtime=Runtime(Config(data_dir=data_dir)))

    with TestClient(app):
        assert app.state.server_bind == {
            "listen_host": "127.0.0.1",
            "listen_port": 8500,
            "port_source": "settings.server_port",
        }


def test_create_app_lifecycle_stops_runtime_on_shutdown(tmp_path: Path) -> None:
    runtime = Runtime(Config(data_dir=tmp_path / "data"))
    app = create_app(runtime=runtime)

    with TestClient(app):
        assert runtime.storage.data_dir == tmp_path / "data"

    try:
        _ = runtime.storage
    except RuntimeError as exc:
        assert "not started" in str(exc)
    else:
        raise AssertionError("runtime storage should be unavailable after shutdown")
    assert runtime.chat_runs is None


def test_create_app_lifecycle_prefers_async_runtime_shutdown(tmp_path: Path) -> None:
    runtime = _AsyncCloseRuntime(tmp_path)
    app = create_app(runtime=cast(Any, runtime))

    with TestClient(app):
        pass

    assert runtime.aclose_called is True
    assert runtime.stop_called is False


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
    assert asset_response.status_code == 200
    assert asset_response.text == "console.log('webui');"
    assert fallback_response.status_code == 200
    assert '<script type="module" src="/assets/app.js"></script>' in fallback_response.text


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


class _LazyChatRunRuntime:
    def __init__(self, data_dir: Path) -> None:
        self.chat_runs = None
        self._chat_run_manager = ChatRunManager()
        self.storage = type("Storage", (), {"data_dir": data_dir})()

    @property
    def chat_run_manager(self) -> ChatRunManager:
        return self._chat_run_manager

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None


class _AsyncCloseRuntime(_LazyChatRunRuntime):
    def __init__(self, data_dir: Path) -> None:
        super().__init__(data_dir)
        self.aclose_called = False
        self.stop_called = False

    async def aclose(self) -> None:
        self.aclose_called = True

    def stop(self) -> None:
        self.stop_called = True


class _UnavailableChatRunRuntime:
    def __init__(self, data_dir: Path) -> None:
        self.chat_runs = None
        self.storage = type("Storage", (), {"data_dir": data_dir})()

    @property
    def chat_run_manager(self) -> ChatRunManager:
        raise RuntimeError("stub chat run manager unavailable")

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None
