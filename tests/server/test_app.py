"""Tests for app."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient  # type: ignore[import-not-found]

from core.automation import _cron_claims as cron_claims
from core.automation.cron import CronService
from core.chat import ChatLoop
from core.extensions import ExtensionRegistrationIdentity
from core.runs import ChatRunManager, Run
from core.runtime import Runtime
from core.sessions import ChatSessionManager
from core.sessions.format import write_bootstrap_marker
from core.utils.config import Config
from core.utils.server_control import CONTROL_SHUTDOWN_PATH, CONTROL_TOKEN_HEADER
from server._app_lifecycle import (
    _shutdown_local_catalog_refresh,
    _start_statistics_warmup,
)
from server.app import (
    create_app,
)
from server.clients import ClientRegistry
from server.events import ServerEventBus


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


def test_create_app_wires_owner_qualified_extension_invalidations(tmp_path: Path) -> None:
    runtime = Runtime(Config(data_dir=tmp_path / "data"))
    app = create_app(runtime=runtime)

    with TestClient(app):
        publisher = runtime._extension_change_publisher  # noqa: SLF001 - wiring contract
        assert publisher is not None
        publisher("swarm", "board", ("swarm-one",), 7)

        event = app.state.event_bus.events[-1]
        assert event["type"] == "resource_changed"
        assert event["payload"] == {
            "kind": "extensions",
            "scope": {
                "owner": "swarm",
                "resource": "board",
                "ids": ["swarm-one"],
                "revision": 7,
            },
        }


def test_extension_run_events_streams_only_the_current_owned_page_run(tmp_path: Path) -> None:
    run = Run(run_id="run-a", agent_id="participant", session_id="session-a")
    run.emit("model.response", {"text": "visible"})
    identity = ExtensionRegistrationIdentity("alpha", "epoch-a")

    class Groups:
        calls = 0

        async def owned_run(self, group_id: str, run_id: str) -> Any:
            if (group_id, run_id) != ("group-a", "run-a"):
                raise ValueError("foreign run")
            self.calls += 1
            if self.calls == 2:
                run.mark_completed(None)
            return SimpleNamespace(run=run)

    class Registry:
        current = True

        def is_registration_current(self, candidate: Any) -> bool:
            return self.current and candidate == identity

        def page_declarations(self) -> list[tuple[Any, Any, Path]]:
            return [(identity, SimpleNamespace(page_id="main"), tmp_path / "index.html")]

        def host_for(self, candidate: Any) -> Any:
            if not self.is_registration_current(candidate):
                raise ValueError("stale owner")
            return SimpleNamespace(temporary_agents=groups)

    groups = Groups()
    runtime = cast(Any, _StubServerRuntime(tmp_path / "data"))
    runtime.extensions = Registry()
    app = create_app(runtime=runtime, config=Config(data_dir=tmp_path / "data"))

    with TestClient(app) as client:
        url = app.state.file_delivery.open_extension_run(
            extension="alpha",
            page="main",
            epoch="epoch-a",
            group_id="group-a",
            run_id="run-a",
            after_sequence=0,
        )["url"]
        response = client.get(url)
        runtime.extensions.current = False
        stale_response = client.get(url)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: model.response" in response.text
    assert '"text":"visible"' in response.text
    assert stale_response.status_code == 404


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
    claim_path = cron_claims.path_for(seed_cron._once_fire_claims_dir, once.id)
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
