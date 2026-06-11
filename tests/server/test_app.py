"""Tests for the server FastAPI app factory."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient  # type: ignore[import-not-found]

from core.chat import ChatLoop
from core.runs import ChatRunManager, RunStatus
from core.runtime import Runtime
from core.utils.config import Config
from server.app import (
    ServerEventBus,
    _active_runs_snapshot,
    _bus_epoch,
    _bus_last_sequence,
    _parse_query_string,
    _register_run_event_bridge,
    create_app,
)


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


def test_create_app_wires_runtime_owned_chat_runs_for_stub_runtime(tmp_path: Path) -> None:
    runtime = _StubServerRuntime(tmp_path)
    app = create_app(runtime=cast(Any, runtime))

    with TestClient(app):
        assert app.state.chat_runs is runtime.chat_run_manager
        assert app.state.chat_loop is runtime.chat_loop
        assert app.state.streaming_chat_loop is runtime.streaming_chat_loop
        assert app.state.command_dispatcher is runtime.command_dispatcher


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


def test_create_app_lifecycle_closes_device_flow_engine(tmp_path: Path) -> None:
    runtime = _StubServerRuntime(tmp_path)
    app = create_app(runtime=cast(Any, runtime))
    engine = _AsyncCloseDeviceFlowEngine()

    with TestClient(app):
        app.state.device_flow_engine = engine

    assert engine.aclose_called is True


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


def test_rpc_endpoint_returns_error_envelope_for_malformed_json(tmp_path: Path) -> None:
    app = create_app(runtime=Runtime(Config(data_dir=tmp_path / "data")))

    with TestClient(app) as client:
        response = client.post(
            "/api/rpc",
            content="{",
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
    assert asset_response.status_code == 200
    assert asset_response.text == "console.log('webui');"
    assert fallback_response.status_code == 200
    assert '<script type="module" src="/assets/app.js"></script>' in fallback_response.text


@pytest.mark.asyncio
async def test_run_event_bridge_publishes_non_rpc_runs() -> None:
    chat_runs = ChatRunManager()
    state = type(
        "State",
        (),
        {
            "chat_runs": chat_runs,
            "event_bus": ServerEventBus(),
            "run_event_bridge_run_ids": set(),
        },
    )()
    unsubscribe = _register_run_event_bridge(state)

    async def execute(_run: Any) -> str:
        return "done"

    try:
        run = await chat_runs.start(
            agent_id="coder",
            session_id="session-one",
            executor=execute,
        )
        await run.wait()
        await _wait_for_events(state.event_bus, 2)
    finally:
        if callable(unsubscribe):
            unsubscribe()

    assert [event["type"] for event in state.event_bus.events] == [
        "run_started",
        "run_completed",
    ]
    assert all(event["payload"]["run_id"] == run.id for event in state.event_bus.events)


async def _wait_for_events(event_bus: ServerEventBus, count: int) -> None:
    for _ in range(20):
        if len(event_bus.events) >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"expected at least {count} events, got {len(event_bus.events)}")


# -- Unit tests for the /ws connection-ready handshake helpers --


def test_parse_query_string_returns_blank_for_missing_or_whitespace() -> None:
    assert _parse_query_string(None) == ""
    assert _parse_query_string("") == ""
    assert _parse_query_string("   ") == ""
    assert _parse_query_string("  abc  ") == "abc"


def test_bus_epoch_returns_property_value_from_bus() -> None:
    bus = ServerEventBus()
    bus._epoch = "epoch-xyz"  # type: ignore[attr-defined]
    assert _bus_epoch(bus) == "epoch-xyz"


def test_bus_last_sequence_uses_property_value_from_bus() -> None:
    bus = ServerEventBus()
    bus.publish("agent.created", {"id": "a"})
    bus.publish("agent.updated", {"id": "a"})
    assert _bus_last_sequence(bus) == 2


def test_bus_last_sequence_is_zero_for_empty_bus() -> None:
    bus = ServerEventBus()
    assert _bus_last_sequence(bus) == 0


def test_active_runs_snapshot_includes_only_running_runs_with_sse_url(
    tmp_path: Path,
) -> None:
    chat_runs = ChatRunManager()
    snapshot: list[Any] = []
    chat_runs.active_runs = lambda: list(snapshot)  # type: ignore[method-assign]

    running_run = cast(
        Any,
        type(
            "StubRun",
            (),
            {
                "id": "run-running",
                "agent_id": "coder",
                "session_id": "session-running",
                "status": RunStatus.RUNNING,
            },
        )(),
    )
    terminal_run = cast(
        Any,
        type(
            "StubRun",
            (),
            {
                "id": "run-terminal",
                "agent_id": "coder",
                "session_id": "session-terminal",
                "status": RunStatus.COMPLETED,
            },
        )(),
    )
    snapshot.extend([running_run, terminal_run])

    state = type("State", (), {"chat_runs": chat_runs})()
    result = _active_runs_snapshot(state)

    assert result == [
        {
            "run_id": "run-running",
            "agent_id": "coder",
            "session_id": "session-running",
            "status": "running",
            "sse_url": "/api/runs/run-running/events",
        }
    ]


def test_active_runs_snapshot_returns_empty_list_when_run_manager_missing() -> None:
    state = type("State", (), {})()
    result = _active_runs_snapshot(state)

    assert result == []


def test_active_runs_snapshot_returns_empty_list_when_manager_lacks_accessor() -> None:
    chat_runs = ChatRunManager()
    state = type("State", (), {"chat_runs": chat_runs})()
    result = _active_runs_snapshot(state)

    assert result == []


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


class _StubServerRuntime:
    """Minimal runtime stub providing the services `_initialize_app_state` reads."""

    def __init__(self, data_dir: Path) -> None:
        self.chat_run_manager = ChatRunManager()
        self.chat_runs = self.chat_run_manager
        self.chat_loop = object()
        self.streaming_chat_loop = object()
        self.command_dispatcher = object()
        self.storage = type("Storage", (), {"data_dir": data_dir})()

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None


class _AsyncCloseRuntime(_StubServerRuntime):
    def __init__(self, data_dir: Path) -> None:
        super().__init__(data_dir)
        self.aclose_called = False
        self.stop_called = False

    async def aclose(self) -> None:
        self.aclose_called = True

    def stop(self) -> None:
        self.stop_called = True


class _AsyncCloseDeviceFlowEngine:
    def __init__(self) -> None:
        self.aclose_called = False

    async def aclose(self) -> None:
        self.aclose_called = True
