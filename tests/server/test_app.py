"""Server app lifespan: runtime wiring, startup resilience, bind state and shutdown."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient  # type: ignore[import-not-found]

from core.automation import _cron_claims as cron_claims
from core.automation.cron import CronService
from core.chat import ChatLoop
from core.database import write_bootstrap_marker
from core.extensions import ExtensionRegistrationIdentity
from core.extensions.extensions import ExtensionUnavailableError
from core.runs import ChatRunManager, Run
from core.runtime import Runtime
from core.sessions import ChatSessionManager
from core.statistics import StatisticsIndex
from core.utils.config import Config
from core.utils.server_control import CONTROL_SHUTDOWN_PATH, CONTROL_TOKEN_HEADER
from server.app import create_app
from server.clients import ClientRegistry
from server.events import ServerEventBus
from tests.server.app_test_support import ServerStubRuntime

JsonObject = dict[str, Any]


def test_real_runtime_serves_a_fresh_data_directory_and_stops_with_the_app(
    tmp_path: Path,
) -> None:
    # The only fresh real-Runtime startup here (about a second): RPC suites use
    # stub Agents, so this covers the app <-> Runtime seam end to end.
    data_dir = tmp_path / "data"
    runtime = Runtime(Config(data_dir=data_dir))
    app = create_app(runtime=runtime)

    with TestClient(app) as client:

        def rpc(method: str, **params: Any) -> JsonObject:
            response = client.post("/api/rpc", json={"method": method, "params": params})
            return cast(JsonObject, response.json())

        assert client.get("/health").json() == {"status": "ok"}
        assert app.state.runtime is runtime
        assert runtime.chat_runs is runtime.chat_run_manager is app.state.chat_runs
        assert isinstance(app.state.chat_runs, ChatRunManager)
        assert isinstance(app.state.chat_loop, ChatLoop)
        assert isinstance(app.state.event_bus, ServerEventBus)
        assert isinstance(app.state.client_registry, ClientRegistry)
        assert isinstance(app.state.agent_delete_lock, asyncio.Lock)
        assert runtime.trigger_service is not None
        assert app.state.server_bind == {
            "listen_host": "127.0.0.1",
            "listen_port": 8420,
            "port_source": "default",
        }

        publisher = runtime._extension_change_publisher  # noqa: SLF001 - wiring contract
        assert publisher is not None
        publisher("swarm", "board", ("swarm-one",), 7)
        # This thread is off the server Event Loop, so the bus hands the event
        # to that loop; a portal round trip runs after the handed-off publish.
        assert client.portal is not None
        client.portal.call(asyncio.sleep, 0)
        assert app.state.event_bus.events[-1]["type"] == "resource_changed"
        assert app.state.event_bus.events[-1]["payload"] == {
            "kind": "extensions",
            "scope": {"owner": "swarm", "resource": "board", "ids": ["swarm-one"], "revision": 7},
        }

        (bootstrap_agent,) = rpc("agent.list")["result"]["agents"]
        history = rpc("chat.history", agent_id="main")["result"]
        last_delete = rpc("agent.delete", id="main")
        created = rpc("agent.create", id="coder")
        created_with_workspace = rpc("agent.create", id="other", workspace=str(tmp_path))
        workspace = tmp_path / "workspace"
        moved = rpc("agent.update", id="main", workspace=str(workspace))
        updated = rpc("agent.update", id="coder", name="Updated Coder")
        session = rpc("session.create", agent_id="coder", session_id="kept", make_current=True)
        private_skill = data_dir / "agents" / "coder" / "skills" / "private" / "SKILL.md"
        private_skill.parent.mkdir(parents=True)
        private_skill.write_text("# Private\n", encoding="utf-8")
        renamed = rpc("agent.rename", id="coder", new_id="researcher")
        agents = {agent["id"]: agent for agent in rpc("agent.list")["result"]["agents"]}
        renamed_history = rpc("chat.history", agent_id="researcher", session_id="kept")
        old_agent = rpc("agent.get", id="coder")
        deleted = rpc("agent.delete", id="researcher")

    assert (bootstrap_agent["id"], bootstrap_agent["name"]) == ("main", "Main")
    assert history["session_id"] == bootstrap_agent["current_session_id"]
    assert history["messages"] == []
    assert last_delete["error"]["code"] == "last_agent"
    assert created["result"]["name"] == "coder"
    assert created["result"]["current_session_id"]
    # A workspace is chosen by updating an existing Agent, never at creation.
    assert created_with_workspace["error"]["code"] == "invalid_request"
    assert moved["result"]["workspace"] == str(workspace.resolve())
    assert updated["result"]["name"] == "Updated Coder"
    assert (workspace / "SOUL.md").exists()
    assert session["result"] == {"agent_id": "coder", "session_id": "kept"}
    assert renamed["result"]["id"] == "researcher"
    assert agents["researcher"]["current_session_id"] == "kept"
    assert renamed_history["result"]["session_id"] == "kept"
    assert old_agent["error"]["code"] == "agent_not_found"
    assert not (data_dir / "agents" / "coder").exists()
    assert deleted["result"]["agent_id"] == "researcher"
    assert runtime.chat_runs is None
    assert runtime.logger is not None
    with pytest.raises(RuntimeError, match="not started"):
        _ = runtime.storage


def test_startup_tolerates_corrupt_agent_settings_and_automation_state(tmp_path: Path) -> None:
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
    (data_dir / "agents" / "main").mkdir(parents=True)
    (data_dir / "agents" / "main" / "agent.json").write_text(
        json.dumps({"name": "Missing id"}), encoding="utf-8"
    )
    (data_dir / "settings.json").write_text(
        json.dumps({"format_version": 1, "server_port": 8500, "compaction": {"enabled": "yes"}}),
        encoding="utf-8",
    )
    (data_dir / "bootstrap").mkdir()
    (data_dir / "bootstrap" / "jobs.json").write_text("{", encoding="utf-8")
    app = create_app(runtime=Runtime(Config(data_dir=data_dir)))

    with TestClient(app) as client:
        health = client.get("/health")
        agents = _rpc_result(client, "agent.list")["agents"]
        cron_jobs = _rpc_result(client, "cron.list")["jobs"]
        bootstrap_jobs = _rpc_result(client, "bootstrap.list")["jobs"]
        server_bind = app.state.server_bind

    assert health.json() == {"status": "ok"}
    assert [agent["id"] for agent in agents] == ["main-2"]
    # Only the once job with the unreadable claim is held; Cron stays available.
    assert [job["id"] for job in cron_jobs] == [once.id]
    assert bootstrap_jobs == []
    # The valid port survives next to an invalid Settings key.
    assert server_bind["listen_port"] == 8500


def test_startup_tolerates_a_corrupt_cron_store(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    jobs_path = data_dir / "cron" / "jobs.json"
    jobs_path.parent.mkdir(parents=True)
    write_bootstrap_marker(data_dir)
    jobs_path.write_text("{", encoding="utf-8")
    app = create_app(runtime=Runtime(Config(data_dir=data_dir)))

    with TestClient(app) as client:
        agents = _rpc_result(client, "agent.list")["agents"]
        cron_jobs = _rpc_result(client, "cron.list")["jobs"]

    assert [agent["id"] for agent in agents] == ["main"]
    assert cron_jobs == []
    assert jobs_path.read_text(encoding="utf-8") == "{"


@pytest.mark.parametrize("async_close", [False, True])
def test_stub_runtime_lifespan_wires_state_and_closes_services(
    tmp_path: Path, async_close: bool
) -> None:
    runtime = _AsyncCloseRuntime(tmp_path) if async_close else ServerStubRuntime(tmp_path)
    app = create_app(runtime=runtime)
    engine = _AsyncCloseDeviceFlowEngine()

    with TestClient(app):
        assert app.state.chat_runs is runtime.chat_run_manager
        assert app.state.chat_loop is runtime.chat_loop
        assert app.state.streaming_chat_loop is runtime.streaming_chat_loop
        assert app.state.command_dispatcher is runtime.command_dispatcher
        assert runtime.bootstrap_activated is True
        app.state.device_flow_engine = engine

    assert engine.aclose_called is True
    # A runtime with async close is closed that way instead of stopped.
    assert runtime.stopped is not async_close
    assert getattr(runtime, "aclose_called", False) is async_close


def test_server_bind_prefers_explicit_state_then_environment_then_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "settings.json").write_text(
        json.dumps({"format_version": 1, "server_port": 8500}), encoding="utf-8"
    )

    def served_bind(**options: Any) -> JsonObject:
        app = create_app(
            runtime=ServerStubRuntime(data_dir), config=Config(data_dir=data_dir), **options
        )
        with TestClient(app):
            return dict(app.state.server_bind)

    monkeypatch.delenv("VBOT_SERVER_PORT", raising=False)
    from_settings = served_bind()
    monkeypatch.setenv("VBOT_SERVER_PORT", "8600")
    from_environment = served_bind()
    explicit = served_bind(
        server_bind={"listen_host": "0.0.0.0", "listen_port": 9100, "port_source": "cli"}
    )

    assert from_settings == {
        "listen_host": "127.0.0.1",
        "listen_port": 8500,
        "port_source": "settings.server_port",
    }
    assert from_environment == {
        "listen_host": "127.0.0.1",
        "listen_port": 8600,
        "port_source": "VBOT_SERVER_PORT",
    }
    assert explicit == {"listen_host": "0.0.0.0", "listen_port": 9100, "port_source": "cli"}


def test_control_shutdown_requires_secret_and_requests_uvicorn_exit(tmp_path: Path) -> None:
    requested: list[str] = []
    app = create_app(
        runtime=ServerStubRuntime(tmp_path),
        shutdown_token="local-secret",
        request_shutdown=lambda: requested.append("shutdown"),
    )

    with TestClient(app) as client:
        rejected = client.post(CONTROL_SHUTDOWN_PATH)
        accepted = client.post(
            CONTROL_SHUTDOWN_PATH, headers={CONTROL_TOKEN_HEADER: "local-secret"}
        )

    assert rejected.status_code == 404
    assert accepted.status_code == 202
    assert accepted.json() == {"status": "stopping"}
    assert requested == ["shutdown"]


@pytest.mark.parametrize(
    "missing", [None, "statistics_index", "models", "list_owned_session_summaries"]
)
def test_statistics_warmup_runs_only_for_a_complete_statistics_surface(
    tmp_path: Path, missing: str | None
) -> None:
    write_bootstrap_marker(tmp_path)
    sessions = ChatSessionManager(tmp_path)
    runtime: Any = ServerStubRuntime(
        tmp_path,
        chat_sessions=sessions,
        statistics_index=StatisticsIndex(tmp_path),
        usage_recorder=None,
        agents=SimpleNamespace(list=lambda: []),
        models=SimpleNamespace(pricing_for=lambda _: None),
        projects=SimpleNamespace(list=lambda: [], session_owning_agents=lambda _project_id: []),
    )
    if missing == "list_owned_session_summaries":
        runtime.chat_sessions = SimpleNamespace(
            **{
                name: getattr(sessions, name)
                for name in ("list_summaries", "list_history_versions", "get")
            }
        )
    elif missing is not None:
        delattr(runtime, missing)
    app = create_app(runtime=runtime)

    with TestClient(app) as client:
        warmup = app.state.statistics_warmup_task
        if warmup is not None:
            assert client.portal is not None
            client.portal.call(asyncio.wait_for, warmup, 10)

    assert (warmup is not None) is (missing is None)
    index_file = tmp_path / "statistics" / "session-statistics.sqlite"
    assert index_file.is_file() is (missing is None)


def test_shutdown_ends_the_local_catalog_refresh_without_raising(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    tasks: list[asyncio.Task[Any]] = []
    with caplog.at_level(logging.WARNING, logger="vbot.server.app"):
        for fails in (False, True):
            # Each TestClient runs its own Event Loop, so each refresh gets its own Event.
            started = asyncio.Event()

            async def refresh(started: asyncio.Event = started, fails: bool = fails) -> None:
                started.set()
                if fails:
                    raise RuntimeError("refresh boom")
                await asyncio.Event().wait()

            app = create_app(
                runtime=ServerStubRuntime(tmp_path, maybe_refresh_local_catalogs=refresh)
            )
            with TestClient(app) as client:
                assert client.portal is not None
                client.portal.call(started.wait)
                tasks.append(app.state.local_catalog_refresh_task)

    pending, failed = tasks
    assert pending.cancelled()
    assert isinstance(failed.exception(), RuntimeError)
    warnings = [
        record
        for record in caplog.records
        if record.getMessage() == "Local model catalog refresh failed during shutdown"
    ]
    assert len(warnings) == 1
    assert warnings[0].exc_info is not None


def test_extension_run_events_streams_only_the_current_owned_page_run(tmp_path: Path) -> None:
    run = Run(run_id="run-a", agent_id="participant", session_id="session-a")
    run.emit("model.response", {"text": "visible"})
    identity = ExtensionRegistrationIdentity("alpha", "epoch-a")

    class Groups:
        calls = 0
        retire_during_lookup = False

        async def owned_run(self, group_id: str, run_id: str) -> Any:
            if (group_id, run_id) != ("group-a", "run-a"):
                raise ValueError("foreign run")
            self.calls += 1
            if self.retire_during_lookup:
                registry.current = False
            elif self.calls == 1:
                run.mark_completed(None)
            return SimpleNamespace(run=run)

    class Registry:
        current = True
        host_bound = True

        def is_registration_current(self, candidate: Any) -> bool:
            return self.current and candidate == identity

        def page_declarations(self) -> list[tuple[Any, Any, Path]]:
            return [(identity, SimpleNamespace(page_id="main"), tmp_path / "index.html")]

        def host_for(self, candidate: Any) -> Any:
            if not self.is_registration_current(candidate):
                raise ValueError("stale owner")
            if not self.host_bound:
                raise ExtensionUnavailableError("Extension host is not bound")
            return SimpleNamespace(temporary_agents=groups)

    groups = Groups()
    registry = Registry()
    app = create_app(runtime=ServerStubRuntime(tmp_path / "data", extensions=registry))

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
        registry.current = False
        stale_response = client.get(url)
        registry.current = True
        registry.host_bound = False
        reloading_response = client.get(url)
        registry.host_bound = True
        groups.retire_during_lookup = True
        retired_response = client.get(url)

    assert response.status_code == 200
    assert groups.calls == 2
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: model.response" in response.text
    assert '"text":"visible"' in response.text
    assert '"file_urls":[]' in response.text
    assert stale_response.status_code == 404
    assert reloading_response.status_code == 404
    assert retired_response.status_code == 404


def _rpc_result(client: TestClient, method: str) -> JsonObject:
    response = client.post("/api/rpc", json={"method": method, "params": {}}).json()
    assert response["ok"] is True, response
    return cast(JsonObject, response["result"])


class _AsyncCloseRuntime(ServerStubRuntime):
    def __init__(self, data_dir: Path) -> None:
        super().__init__(data_dir)
        self.aclose_called = False

    async def aclose(self) -> None:
        self.aclose_called = True


class _AsyncCloseDeviceFlowEngine:
    def __init__(self) -> None:
        self.aclose_called = False

    async def aclose(self) -> None:
        self.aclose_called = True
