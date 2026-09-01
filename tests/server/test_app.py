"""Tests for the server FastAPI app factory."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient  # type: ignore[import-not-found]

from core.automation.cron import CronService
from core.chat import ChatLoop
from core.runs import ChatRunManager, RunKind, RunStatus
from core.runtime import Runtime
from core.sessions import ChatSessionManager, SessionAddress
from core.sessions.format import write_bootstrap_marker
from core.utils.config import Config
from core.utils.server_control import CONTROL_SHUTDOWN_PATH, CONTROL_TOKEN_HEADER
from server.app import (
    WEBUI_DOCUMENT_CACHE_HEADERS,
    ServerEventBus,
    _active_runs_snapshot,
    _bus_epoch,
    _bus_last_sequence,
    _connection_replay_status,
    _is_reserved_server_path,
    _parse_query_string,
    _queues_snapshot,
    _register_cron_change_bridge,
    _register_run_event_bridge,
    _register_session_completion_read_bridge,
    _register_session_title_bridge,
    _shutdown_local_catalog_refresh,
    _start_statistics_warmup,
    _stream_websocket_events,
    create_app,
)
from server.clients import ClientRegistry


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
        assert isinstance(app.state.client_registry, ClientRegistry)
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


def test_control_shutdown_requires_secret_and_requests_uvicorn_exit(tmp_path: Path) -> None:
    requested: list[str] = []
    app = create_app(
        runtime=Runtime(Config(data_dir=tmp_path / "data")),
        shutdown_token="local-secret",
        request_shutdown=lambda: requested.append("shutdown"),
    )

    with TestClient(app) as client:
        rejected = client.post(CONTROL_SHUTDOWN_PATH)
        accepted = client.post(
            CONTROL_SHUTDOWN_PATH,
            headers={CONTROL_TOKEN_HEADER: "local-secret"},
        )

    assert rejected.status_code == 404
    assert accepted.status_code == 202
    assert accepted.json() == {"status": "stopping"}
    assert requested == ["shutdown"]


@pytest.mark.asyncio
async def test_statistics_warmup_builds_disposable_index_for_complete_runtime_surface(
    tmp_path: Path,
) -> None:
    write_bootstrap_marker(tmp_path)
    manager = ChatSessionManager(tmp_path)
    runtime = SimpleNamespace(
        chat_sessions=manager,
        agents=SimpleNamespace(list=lambda: []),
        projects=SimpleNamespace(list=lambda: [], session_owning_agents=lambda _project_id: []),
    )
    state = SimpleNamespace(runtime=runtime)

    task = _start_statistics_warmup(state)

    assert task is not None
    await task
    assert (tmp_path / "statistics" / "session-statistics.sqlite").is_file()
    assert state.statistics_service is not None


def test_bootstrap_rpc_persists_job_without_firing_in_current_process(tmp_path: Path) -> None:
    runtime = Runtime(Config(data_dir=tmp_path / "data"))
    app = create_app(runtime=runtime)

    with TestClient(app) as client:
        response = client.post(
            "/api/rpc",
            json={
                "method": "bootstrap.create",
                "params": {
                    "agent_id": "main",
                    "name": "Verify restart",
                    "prompt": "Check status and logs",
                    "mode": "once",
                },
            },
        )
        listed = client.post("/api/rpc", json={"method": "bootstrap.list", "params": {}}).json()

        assert response.json()["ok"] is True
        assert listed["result"]["jobs"][0]["status"] == "active"
        assert listed["result"]["jobs"][0]["last_run_id"] is None


def test_create_app_starts_when_a_once_fire_claim_is_invalid(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    seed_cron = CronService(cast(Any, SimpleNamespace()), data_dir)
    once = seed_cron.create_job(
        agent_id="main",
        prompt="Once prompt",
        schedule_type="once",
        run_at="2099-01-01T00:00:00+00:00",
    )
    claim_path = seed_cron._once_fire_claim_path(once.id)
    claim_path.parent.mkdir(parents=True, exist_ok=True)
    claim_path.write_text("{", encoding="utf-8")
    write_bootstrap_marker(data_dir)
    runtime = Runtime(Config(data_dir=data_dir))
    app = create_app(runtime=runtime)

    with TestClient(app) as client:
        response = client.get("/health")
        assert runtime.cron_service.list_jobs() == []

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_app_starts_when_bootstrap_store_is_malformed(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    jobs_path = data_dir / "bootstrap" / "jobs.json"
    jobs_path.parent.mkdir(parents=True)
    jobs_path.write_text("{", encoding="utf-8")
    write_bootstrap_marker(data_dir)
    runtime = Runtime(Config(data_dir=data_dir))
    app = create_app(runtime=runtime)

    with TestClient(app) as client:
        response = client.get("/health")
        assert runtime.bootstrap_service.list_jobs() == []

    assert response.status_code == 200


def test_create_app_wires_runtime_owned_chat_runs_for_stub_runtime(tmp_path: Path) -> None:
    runtime = _StubServerRuntime(tmp_path)
    app = create_app(runtime=cast(Any, runtime))

    with TestClient(app):
        assert app.state.chat_runs is runtime.chat_run_manager
        assert app.state.chat_loop is runtime.chat_loop
        assert app.state.streaming_chat_loop is runtime.streaming_chat_loop
        assert app.state.command_dispatcher is runtime.command_dispatcher
        assert runtime.bootstrap_activated is True


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
    write_bootstrap_marker(data_dir)
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
        same_origin = client.get("/health", headers={"origin": "http://testserver"})
        without_origin = client.get("/health")

    assert same_origin.status_code == 200
    assert without_origin.status_code == 200


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
                "origin": "http://testserver",
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
            SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
            execute,
        )
        await run.wait()
        await _wait_for_events(state.event_bus, 4)
    finally:
        if callable(unsubscribe):
            unsubscribe()

    assert [event["type"] for event in state.event_bus.events] == [
        "run_started",
        "run_completed",
        "resource_changed",
        "resource_changed",
    ]
    assert all(event["payload"]["run_id"] == run.id for event in state.event_bus.events[:2])
    assert [event["payload"] for event in state.event_bus.events[-2:]] == [
        {"kind": "debug_traces"},
        {"kind": "sessions", "scope": {"agent_id": "coder"}},
    ]


def test_session_title_bridge_publishes_sessions_invalidation(tmp_path: Path) -> None:
    write_bootstrap_marker(tmp_path)
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="session-one")
    state = type(
        "State",
        (),
        {
            "runtime": type("Runtime", (), {"chat_sessions": sessions})(),
            "event_bus": ServerEventBus(),
        },
    )()
    unsubscribe = _register_session_title_bridge(state)

    try:
        sessions.set_auto_title(
            SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
            "Local title",
        )
    finally:
        if callable(unsubscribe):
            unsubscribe()

    assert state.event_bus.events[-1]["type"] == "resource_changed"
    assert state.event_bus.events[-1]["payload"] == {
        "kind": "sessions",
        "scope": {"agent_id": "coder"},
    }


def test_session_completion_read_bridge_publishes_sessions_invalidation(tmp_path: Path) -> None:
    write_bootstrap_marker(tmp_path)
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="session-one")
    sessions.record_terminal_run(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        "run-one",
        "completed",
        "2026-07-20T10:00:00+00:00",
    )
    state = SimpleNamespace(
        runtime=SimpleNamespace(chat_sessions=sessions),
        event_bus=ServerEventBus(),
    )
    unsubscribe = _register_session_completion_read_bridge(state)

    try:
        sessions.mark_terminal_run_read(
            SessionAddress(project_id=None, agent_id="coder", session_id="session-one"), "run-one"
        )
    finally:
        if callable(unsubscribe):
            unsubscribe()

    assert state.event_bus.events[-1]["type"] == "resource_changed"
    assert state.event_bus.events[-1]["payload"] == {
        "kind": "sessions",
        "scope": {"agent_id": "coder"},
    }


def test_cron_change_bridge_publishes_scheduler_invalidation(tmp_path: Path) -> None:
    cron_service = CronService(cast(Any, SimpleNamespace()), tmp_path)
    state = SimpleNamespace(
        runtime=SimpleNamespace(cron_service=cron_service),
        event_bus=ServerEventBus(),
    )
    unsubscribe = _register_cron_change_bridge(state)

    try:
        cron_service.create_job(
            agent_id="coder",
            prompt="Scheduled work",
            schedule_type="cron",
            cron_expression="0 9 * * *",
        )
    finally:
        if callable(unsubscribe):
            unsubscribe()

    assert state.event_bus.events[-1]["type"] == "resource_changed"
    assert state.event_bus.events[-1]["payload"] == {"kind": "cron"}


async def _wait_for_events(event_bus: ServerEventBus, count: int) -> None:
    for _ in range(20):
        if len(event_bus.events) >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"expected at least {count} events, got {len(event_bus.events)}")


# -- Unit tests for the /ws connection-ready handshake helpers --


def test_reserved_server_paths_include_websocket_and_control_prefixes() -> None:
    assert _is_reserved_server_path("health")
    assert _is_reserved_server_path("ws")
    assert _is_reserved_server_path("ws/logs")
    assert _is_reserved_server_path("ws/terminals/term-1")
    assert _is_reserved_server_path("api/rpc")
    assert not _is_reserved_server_path("chat")
    assert not _is_reserved_server_path("settings")


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
    bus.publish("run_started", {"id": "a"})
    bus.publish("run_output", {"id": "a"})
    assert _bus_last_sequence(bus) == 2


def test_bus_last_sequence_is_zero_for_empty_bus() -> None:
    bus = ServerEventBus()
    assert _bus_last_sequence(bus) == 0


def test_connection_replay_status_distinguishes_complete_and_incomplete_cursors() -> None:
    bus = ServerEventBus(event_retention_limit=2)
    bus._epoch = "epoch-current"  # type: ignore[attr-defined]
    for index in range(4):
        bus.publish("run_started", {"id": f"run-{index}"})

    assert (
        _connection_replay_status(
            bus,
            client_epoch="",
            client_after_sequence=0,
            last_sequence=bus.last_sequence,
        )
        == "fresh"
    )
    assert (
        _connection_replay_status(
            bus,
            client_epoch="epoch-current",
            client_after_sequence=2,
            last_sequence=bus.last_sequence,
        )
        == "resumed"
    )
    assert (
        _connection_replay_status(
            bus,
            client_epoch="epoch-current",
            client_after_sequence=1,
            last_sequence=bus.last_sequence,
        )
        == "gap"
    )
    assert (
        _connection_replay_status(
            bus,
            client_epoch="epoch-current",
            client_after_sequence=5,
            last_sequence=bus.last_sequence,
        )
        == "gap"
    )
    assert (
        _connection_replay_status(
            bus,
            client_epoch="epoch-old",
            client_after_sequence=2,
            last_sequence=bus.last_sequence,
        )
        == "epoch_changed"
    )


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
                "project_id": "acme",
                "session_id": "session-running",
                "run_kind": RunKind.USER,
                "status": RunStatus.RUNNING,
                "created_at": "2026-08-05T18:00:00+00:00",
                "iteration_count": 4,
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
                "project_id": "acme",
                "session_id": "session-terminal",
                "run_kind": RunKind.USER,
                "status": RunStatus.COMPLETED,
                "created_at": "2026-08-05T17:00:00+00:00",
                "iteration_count": 2,
            },
        )(),
    )
    snapshot.extend([running_run, terminal_run])

    state = type("State", (), {"chat_runs": chat_runs})()
    result = _active_runs_snapshot(state)

    # The project rides alongside the bare agent id so a reconnecting client can
    # rebuild the address-keyed session and re-attach the run.
    assert result == [
        {
            "run_id": "run-running",
            "agent_id": "coder",
            "project_id": "acme",
            "session_id": "session-running",
            "run_kind": "user",
            "status": "running",
            "started_at": "2026-08-05T18:00:00+00:00",
            "iteration_count": 4,
            "sse_url": "/api/runs/run-running/events",
        }
    ]


class _ScriptedLogWebSocket:
    """Minimal /ws/logs websocket double: scripted inbound frames, recorded sends."""

    def __init__(self) -> None:
        self.incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []

    async def receive(self) -> dict[str, Any]:
        return await self.incoming.get()

    async def send_json(self, event: dict[str, Any]) -> None:
        self.sent.append(event)


def _queue_log_stream(queue: asyncio.Queue[Any]) -> Any:
    """A real async generator, so a cancelled __anext__ finalizes it like the live tail."""

    async def generate() -> Any:
        while True:
            item = await queue.get()
            if item is None:
                return
            yield item

    return generate()


@pytest.mark.asyncio
async def test_stream_websocket_events_survives_stray_client_frames() -> None:
    # Arrange
    websocket = _ScriptedLogWebSocket()
    log_events: asyncio.Queue[Any] = asyncio.Queue()
    streamer = asyncio.create_task(
        _stream_websocket_events(cast(Any, websocket), _queue_log_stream(log_events))
    )

    # Act: a stray client frame arrives while no log event is pending; a log
    # event afterwards must still be delivered instead of the tail dying.
    await websocket.incoming.put({"type": "websocket.receive", "text": "keepalive"})
    await log_events.put({"line": "hello"})

    async def wait_for_delivery() -> None:
        while not websocket.sent:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_delivery(), timeout=1)
    await websocket.incoming.put({"type": "websocket.disconnect"})
    await asyncio.wait_for(streamer, timeout=1)

    # Assert
    assert websocket.sent == [{"line": "hello"}]


@pytest.mark.asyncio
async def test_shutdown_local_catalog_refresh_cancels_pending_task() -> None:
    # Arrange
    refresh_started = asyncio.Event()

    async def never_finishes() -> None:
        refresh_started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(never_finishes())
    await refresh_started.wait()

    # Act
    await _shutdown_local_catalog_refresh(task, logging.getLogger("test-shutdown"))

    # Assert
    assert task.cancelled()


@pytest.mark.asyncio
async def test_shutdown_local_catalog_refresh_logs_failed_task_without_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Arrange: the refresh already failed before shutdown reaches it.
    async def explode() -> None:
        raise RuntimeError("refresh boom")

    task = asyncio.create_task(explode())
    await asyncio.sleep(0)

    # Act
    with caplog.at_level(logging.WARNING, logger="test-shutdown"):
        await _shutdown_local_catalog_refresh(task, logging.getLogger("test-shutdown"))

    # Assert
    warnings = [record for record in caplog.records if record.name == "test-shutdown"]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert warnings[0].exc_info is not None


def test_active_runs_snapshot_keeps_project_id_none_for_identity_run(
    tmp_path: Path,
) -> None:
    chat_runs = ChatRunManager()
    snapshot: list[Any] = []
    chat_runs.active_runs = lambda: list(snapshot)  # type: ignore[method-assign]

    identity_run = cast(
        Any,
        type(
            "StubRun",
            (),
            {
                "id": "run-identity",
                "agent_id": "coder",
                "project_id": None,
                "session_id": "session-identity",
                "run_kind": RunKind.USER,
                "status": RunStatus.RUNNING,
                "created_at": "2026-08-05T18:00:00+00:00",
                "iteration_count": 1,
            },
        )(),
    )
    snapshot.append(identity_run)

    state = type("State", (), {"chat_runs": chat_runs})()
    result = _active_runs_snapshot(state)

    assert result[0]["project_id"] is None


def test_active_runs_snapshot_marks_runs_excluded_from_agent_activity() -> None:
    chat_runs = ChatRunManager()
    hidden_run = cast(
        Any,
        type(
            "StubRun",
            (),
            {
                "id": "run-system",
                "agent_id": "coder",
                "project_id": None,
                "session_id": "session-system",
                "run_kind": RunKind.SYSTEM,
                "status": RunStatus.RUNNING,
                "created_at": "2026-08-05T18:00:00+00:00",
                "iteration_count": 0,
                "contributes_to_agent_activity": False,
            },
        )(),
    )
    chat_runs.active_runs = lambda: [hidden_run]  # type: ignore[method-assign]

    result = _active_runs_snapshot(type("State", (), {"chat_runs": chat_runs})())

    assert result[0]["contributes_to_agent_activity"] is False


def test_active_runs_snapshot_returns_empty_list_when_run_manager_missing() -> None:
    state = type("State", (), {})()
    result = _active_runs_snapshot(state)

    assert result == []


def test_active_runs_snapshot_returns_empty_list_when_manager_lacks_accessor() -> None:
    chat_runs = ChatRunManager()
    state = type("State", (), {"chat_runs": chat_runs})()
    result = _active_runs_snapshot(state)

    assert result == []


def test_queues_snapshot_groups_public_items_by_sorted_session_scope() -> None:
    class StubItem:
        def __init__(self, item_id: str, *, internal: bool = False) -> None:
            self.item_id = item_id
            self.internal = internal

        def to_dict(self) -> dict[str, Any]:
            return {"id": self.item_id, "internal": self.internal}

    queued = [
        (("project-b", "writer", "session-b"), StubItem("second")),
        ((None, "coder", "session-a"), StubItem("first")),
        ((None, "coder", "session-a"), StubItem("hidden", internal=True)),
        (("project-b", "writer", "session-b"), StubItem("third")),
    ]
    chat_runs = ChatRunManager()
    chat_runs.all_queued = lambda: cast(Any, list(queued))  # type: ignore[method-assign]
    state = type("State", (), {"chat_runs": chat_runs})()

    assert _queues_snapshot(state) == [
        {
            "project_id": None,
            "agent_id": "coder",
            "session_id": "session-a",
            "items": [{"id": "first", "internal": False}],
        },
        {
            "project_id": "project-b",
            "agent_id": "writer",
            "session_id": "session-b",
            "items": [
                {"id": "second", "internal": False},
                {"id": "third", "internal": False},
            ],
        },
    ]


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
        self.bootstrap_activated = False

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def activate_bootstrap(self) -> None:
        self.bootstrap_activated = True


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
