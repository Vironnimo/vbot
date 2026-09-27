"""Runtime start, stop, close, failed-start cleanup, logging, and lifetime-owned services."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import threading
import time
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

import core.runtime._bootstrap as bootstrap_module
import core.runtime.runtime as runtime_module
from core.automation import CronService
from core.automation import _cron_claims as cron_claims
from core.chat import ChatMessage
from core.database import canonical_database_path
from core.debug import DebugTraceStore
from core.debug import store as debug_store
from core.model_tasks import SpeechService
from core.performance import PerformanceService
from core.performance.performance import reset_for_tests
from core.providers.accounts import ConnectionRef
from core.providers.providers import ProviderRegistry
from core.providers.usage import ProviderUsageService
from core.runs import Run, RunStatus
from core.runtime.databases import canonical_database_specs
from core.runtime.keep_awake import KeepAwakeController
from core.runtime.runtime import Runtime
from core.sessions import SessionAddress
from core.storage.layout import DataDirectoryLayout
from core.storage.storage import StorageManager
from core.storage.temp_files import TemporaryFileManager
from core.tools._bash_update_handoff import HANDOFF_DIRECTORY, UPDATE_HANDOFF_FILE_RETENTION
from core.tools.process_manager import ProcessManager
from core.tools.terminal_manager import TerminalManager, TerminalManagerError
from core.utils.config import Config
from tests.core.sessions.history_fixtures import seed_history

# Service accessors that exist only while the Runtime is started.
_STARTED_SERVICES = (
    "storage",
    "agents",
    "provider_credentials",
    "providers",
    "models",
    "tools",
    "process_manager",
    "update_handoffs",
    "terminal_manager",
    "skills",
    "chat_sessions",
    "system_prompts",
    "embeddings",
    "live_voice",
    "speech",
    "cron_service",
    "provider_usage",
    "performance",
)


def _assert_not_started(runtime: Runtime) -> None:
    for name in _STARTED_SERVICES:
        with pytest.raises(RuntimeError, match="Runtime not started"):
            getattr(runtime, name)


def test_runtime_rejects_unknown_safe_startup_modes(config: Config) -> None:
    with pytest.raises(ValueError, match="safe_startup_mode must be verification, test, or None"):
        Runtime(config, safe_startup_mode="production")  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize("safe_startup_mode", ["verification", "test"])
async def test_safe_startup_does_not_load_extensions_or_start_producers(
    config: Config, monkeypatch: pytest.MonkeyPatch, safe_startup_mode: str
) -> None:
    from core.extensions import ExtensionRegistry

    monkeypatch.setattr(
        ExtensionRegistry,
        "load",
        Mock(side_effect=AssertionError("safe startup must not load executable extensions")),
    )
    runtime = Runtime(config, safe_startup_mode=safe_startup_mode)  # type: ignore[arg-type]
    for name in (
        "_start_channel_service",
        "_start_cron_service",
        "_start_calendar_service",
        "_start_provider_usage_service",
    ):
        monkeypatch.setattr(runtime, name, Mock(side_effect=AssertionError(name)))

    reset_for_tests()
    runtime.start()
    try:
        extensions = runtime.extensions
        assert extensions is not None
        assert extensions.diagnostics() == []
        # The passive performance monitor still observes the Event Loop.
        performance = runtime.performance
        assert performance.monitoring
    finally:
        await runtime.aclose()
        reset_for_tests()
    assert not performance.monitoring


@pytest.mark.asyncio
async def test_runtime_runs_persist_through_the_shared_manager_without_data_snapshots(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.database import snapshots

    def reject_snapshot(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("normal Runtime operation must not create a data snapshot")

    monkeypatch.setattr(snapshots, "create_data_snapshot", reject_snapshot)
    # Run persistence must not depend on how the chat loops are constructed.
    monkeypatch.setattr(bootstrap_module, "ChatLoop", Mock())
    runtime = Runtime(config)
    runtime.start()
    try:
        session = runtime.chat_sessions.create("main")
        session.append(ChatMessage.user("normal Runtime write"))

        async def execute(run: Run) -> ChatMessage:
            answer = ChatMessage.assistant(model="test", content="persisted")
            await session.for_run(run.id).append_async(answer)
            return answer

        run = await runtime.chat_run_manager.start(session.address, execute)
        await run.wait()

        assert run.events[-1].payload["history_persisted"] is True
        result = session.load_run_result(run_id=run.id)
        assert result is not None
        assert result.assistant is not None
        assert result.assistant.content == "persisted"
        summary = session.find_run_summary(run_id=run.id)
        assert summary is not None
        assert summary.status == "completed"
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_runtime_registers_local_speech_and_closes_its_executor(config: Config) -> None:
    runtime = Runtime(config, safe_startup_mode="test")
    runtime.start()
    speech = runtime.speech
    try:
        targets = runtime.model_tasks.list_targets("speech_to_text")
        assert {target.id for target in targets if target.kind == "local"} == {
            "local/qwen3-asr",
            "local/parakeet",
            "local/nemotron3.5-asr",
        }
        tts = runtime.model_tasks.list_targets("text_to_speech")
        assert {target.id for target in tts if target.kind == "local"} == {
            "local/qwen3-tts",
            "local/chatterbox",
        }
        assert (
            speech.local_setup_for("local/chatterbox").directory
            == runtime.storage.layout.speech_engines / "chatterbox"
        )
        runtime.model_tasks.update(
            {"speech_to_text": {"target": "local/parakeet", "options": {"device": "cpu"}}}
        )
        assert runtime.model_tasks.binding_for("speech_to_text").target == "local/parakeet"
        assert not any(model["loaded"] for model in speech.local_memory_status()["models"])
    finally:
        await runtime.aclose()
    assert speech._local_executor._closed  # noqa: SLF001 - no public closed state.


def test_runtime_start_is_idempotent_and_restart_rebuilds_services(config: Config) -> None:
    # Stopping a Runtime that never started is a no-op.
    Runtime(config).stop()

    runtime = Runtime(config)
    runtime.start()
    started = {
        name: getattr(runtime, name) for name in ("providers", "models", "storage", "agents")
    }
    runtime.start()
    assert {name: getattr(runtime, name) for name in started} == started
    # Started outside an Event Loop, the performance monitor stays off.
    assert not runtime.performance.monitoring
    runtime.agents.create("coder", "Coder Agent")
    runtime.agents.delete("main")

    runtime.stop()
    _assert_not_started(runtime)
    runtime.stop()
    # Closing an already stopped Runtime is safe too; it can still start again.
    asyncio.run(runtime.aclose())
    _assert_not_started(runtime)

    runtime.start()
    try:
        assert runtime.storage is not started["storage"]
        assert "openai" in runtime.providers.list_ids()
        # Persisted Agents survive a restart; no default main Agent is added.
        agents = runtime.agents.list()
        assert [agent.id for agent in agents] == ["coder"]
        address = SessionAddress(
            project_id=None, agent_id="coder", session_id=agents[0].current_session_id
        )
        assert runtime.chat_sessions.get(address).load() == []
    finally:
        runtime.stop()


def test_service_access_requires_a_started_runtime_and_is_read_only(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(config)

    _assert_not_started(runtime)
    assert runtime.extensions is None
    for call in (
        lambda: runtime.get_adapter(ConnectionRef("openai", "openai:api-key")),
        lambda: runtime.get_model("openai", "gpt-5.2"),
        lambda: runtime.has_provider_credentials("openai"),
        lambda: runtime.get_provider_credentials("openai"),
    ):
        with pytest.raises(RuntimeError):
            call()
    monkeypatch.setattr(runtime, "_started", True)
    with pytest.raises(RuntimeError, match="Provider registry not available"):
        _ = runtime.providers
    with pytest.raises(AttributeError, match="has no setter"):
        runtime.providers = object()  # type: ignore[misc]
    runtime.stop()
    with pytest.raises(RuntimeError, match="Runtime not started"):
        _ = runtime.providers


def test_runtime_start_removes_only_expired_update_handoff_tickets(config: Config) -> None:
    tickets = config.data_dir / HANDOFF_DIRECTORY
    tickets.mkdir(parents=True)
    expired = tickets / "expired.json"
    recent = tickets / "recent.json"
    for path in (expired, recent):
        path.write_text("{}", encoding="utf-8")
    old = time.time() - UPDATE_HANDOFF_FILE_RETENTION.total_seconds() - 60
    os.utime(expired, (old, old))
    runtime = Runtime(config, safe_startup_mode="test")

    runtime.start()
    runtime.stop()

    assert not expired.exists()
    assert recent.exists()


def _clear_provider_credential_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Leave exactly one API-key Connection usable so the inventory counts are exact."""
    resources_path = Path(__file__).resolve().parents[3] / "resources"
    provider_registry = ProviderRegistry.load(resources_path)
    seeded_credential_key: str | None = None
    for provider_id in provider_registry.list_ids():
        for connection in provider_registry.get(provider_id).connections:
            credential_key = connection.auth.credential_key
            if not credential_key:
                continue
            monkeypatch.delenv(credential_key, raising=False)
            if seeded_credential_key is None and connection.type == "api_key":
                seeded_credential_key = credential_key
    assert seeded_credential_key is not None
    monkeypatch.setenv(seeded_credential_key, "test-startup-credential")


def _expected_startup_inventory_message(runtime: Runtime) -> str:
    provider_ids = runtime.providers.list_ids()
    usable_provider_count = 0
    total_connection_count = 0
    usable_connection_count = 0
    for provider_id in provider_ids:
        provider_is_usable = False
        for connection in runtime.providers.get(provider_id).connections:
            total_connection_count += 1
            if runtime.provider_credentials.is_usable(
                provider_id, f"{provider_id}:{connection.id}"
            ):
                usable_connection_count += 1
                provider_is_usable = True
        usable_provider_count += provider_is_usable
    assert 0 < usable_connection_count < total_connection_count
    return (
        "Runtime inventory: "
        f"{len(runtime.tools.list_tools())} tools, "
        f"{len(runtime.skills.list_all())} skills, "
        f"{usable_provider_count}/{len(provider_ids)} usable providers, "
        f"{usable_connection_count}/{total_connection_count} usable connections"
    )


def test_runtime_lifecycle_logs_use_the_managed_daily_log_file(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_provider_credential_environment(monkeypatch)
    broken_skill_dir = config.data_dir / "extra-skills" / "broken"
    broken_skill_dir.mkdir(parents=True)
    broken_skill_dir.joinpath("SKILL.md").write_text(
        "---\nname: broken\ndescription: Has unsupported vBot requirements.\n"
        "metadata:\n  vbot:\n    requirements:\n      provider: missing\n---\n\n# Broken\n",
        encoding="utf-8",
    )
    config.data_dir.joinpath("settings.json").write_text(
        json.dumps(
            {"format_version": 1, "skill_directories": [str(config.data_dir / "extra-skills")]}
        ),
        encoding="utf-8",
    )
    runtime = Runtime(config)

    runtime.start()
    assert isinstance(runtime.logger, logging.Logger)
    expected_inventory = _expected_startup_inventory_message(runtime)
    runtime.stop()

    log_files = list((config.data_dir / "logs").iterdir())
    assert len(log_files) == 1
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}\.log", log_files[0].name)
    contents = log_files[0].read_text(encoding="utf-8")
    lines = contents.strip().splitlines()
    for message in ("Runtime startup initiated", "Runtime started", "Runtime stopped"):
        assert any(line.endswith(f"[INFO] vbot.core - {message}") for line in lines)
    assert f"[INFO] vbot.core - {expected_inventory}" in contents
    assert "[WARN] vbot.core - Loaded skills with " in contents
    assert " invalid skill directories; see vbot.skills warnings for details" in contents


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner", "method"),
    [
        # Before the Runtime logger exists; nothing else has started yet.
        (StorageManager, "ensure_directories"),
        # With logging, keep-awake, speech, and host process owners started.
        (Runtime, "_start_terminal_manager"),
        # The last startup step, after the performance monitor began.
        (Runtime, "_start_provider_usage_service"),
    ],
)
async def test_failed_start_logs_once_releases_started_resources_and_can_retry(
    config: Config, monkeypatch: pytest.MonkeyPatch, owner: type, method: str
) -> None:
    released: list[str] = []
    performance_threads: list[threading.Thread | None] = []

    def record(target: type, name: str, label: str) -> None:
        original = getattr(target, name)

        def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            released.append(label)
            if isinstance(self, PerformanceService):
                performance_threads.append(self._monitor._thread)  # noqa: SLF001
            return original(self, *args, **kwargs)

        monkeypatch.setattr(target, name, wrapper)

    record(TemporaryFileManager, "stop", "temporary_files")
    record(KeepAwakeController, "close", "keep_awake")
    record(SpeechService, "close", "speech")
    record(ProcessManager, "stop", "process_manager")
    record(TerminalManager, "stop", "terminal_manager")
    record(PerformanceService, "stop", "performance")
    failure = RuntimeError("startup sentinel")
    runtime = Runtime(config)

    with monkeypatch.context() as patch:
        patch.setattr(owner, method, Mock(side_effect=failure))
        with pytest.raises(RuntimeError) as caught:
            runtime.start()

    assert caught.value is failure
    _assert_not_started(runtime)
    expected = {
        "ensure_directories": set(),
        "_start_terminal_manager": {"temporary_files", "keep_awake", "speech", "process_manager"},
        "_start_provider_usage_service": {
            "temporary_files",
            "keep_awake",
            "speech",
            "process_manager",
            "terminal_manager",
            "performance",
        },
    }[method]
    assert expected <= set(released)
    if "performance" in expected:
        assert performance_threads and all(
            thread is not None and not thread.is_alive() for thread in performance_threads
        )
    log_file = next((config.data_dir / "logs").iterdir())
    logging.getLogger("vbot.core").warning("written after cleanup")
    contents = log_file.read_text(encoding="utf-8")
    assert contents.count("[ERROR] vbot.core - Runtime startup failed") == 1
    assert contents.count("Traceback (most recent call last):") == 1
    assert contents.count("RuntimeError: startup sentinel") == 1
    # Cleanup closed the managed handlers after the failure was recorded.
    assert "written after cleanup" not in contents

    runtime.start()
    try:
        assert runtime.agents.list()
    finally:
        await runtime.aclose()


def test_runtime_failed_start_never_creates_a_missing_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = Config(data_dir=tmp_path / "fresh")
    monkeypatch.setattr(
        StorageManager, "ensure_directories", Mock(side_effect=RuntimeError("startup sentinel"))
    )

    with pytest.raises(RuntimeError, match="startup sentinel"):
        Runtime(config).start()

    # A root created only for the log would lack the Session bootstrap marker.
    assert not config.data_dir.exists()


def test_runtime_start_survives_corrupt_optional_configuration(
    tmp_path: Path, config: Config
) -> None:
    resources_dir = tmp_path / "resources"
    providers_dir = resources_dir / "providers"
    models_dir = resources_dir / "models"
    providers_dir.mkdir(parents=True)
    models_dir.mkdir(parents=True)
    providers_dir.joinpath("broken.json").write_text('{"id":', encoding="utf-8")
    models_dir.joinpath("broken.json").write_text('{"provider_id":', encoding="utf-8")
    models_dir.joinpath("healthy.json").write_text(
        json.dumps(
            {
                "provider_id": "healthy",
                "models": {
                    "model-a": {
                        "name": "Healthy Model",
                        "capabilities": {
                            "vision": False,
                            "tools": True,
                            "json_mode": False,
                            "reasoning": {"supported": False},
                        },
                        "context_window": 32000,
                        "max_output_tokens": 4096,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    config.data_dir.joinpath(".env").write_bytes(b"\xff")
    config._data["RESOURCES_PATH"] = str(resources_dir)  # noqa: SLF001
    runtime = Runtime(config)

    runtime.start()
    try:
        assert runtime.models.get("healthy", "model-a").name == "Healthy Model"
        assert runtime.providers.list_ids() == []
        assert runtime.agents.list()
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_loop_started_runtime_survives_corrupt_agent_and_automation_state(
    config: Config,
) -> None:
    data_dir = config.data_dir
    seed_cron = CronService(cast(Any, SimpleNamespace()), data_dir)
    once = seed_cron.create_job(
        agent_id="main",
        prompt="Once prompt",
        schedule_type="once",
        run_at="2099-01-01T00:00:00+00:00",
    )
    claim_path = cron_claims.path_for(seed_cron._once_fire_claims_dir, once.id)  # noqa: SLF001
    claim_path.parent.mkdir(parents=True, exist_ok=True)
    claim_path.write_text("{", encoding="utf-8")
    (data_dir / "agents" / "main").mkdir(parents=True)
    (data_dir / "agents" / "main" / "agent.json").write_text(
        json.dumps({"name": "Missing id"}), encoding="utf-8"
    )
    (data_dir / "settings.json").write_text(
        json.dumps({"format_version": 1, "compaction": {"enabled": "yes"}}), encoding="utf-8"
    )
    (data_dir / "bootstrap").mkdir()
    (data_dir / "bootstrap" / "jobs.json").write_text("{", encoding="utf-8")
    runtime = Runtime(config)

    runtime.start()
    try:
        # The serving lifespan activates Bootstrap; a degraded store skips it.
        runtime.activate_bootstrap()
        assert [agent.id for agent in runtime.agents.list()] == ["main-2"]
        # Only the once job with the unreadable claim is held; Cron stays available.
        assert [job.id for job in runtime.cron_service.list_jobs()] == [once.id]
        assert runtime.bootstrap_service.list_jobs() == []
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_loop_started_runtime_leaves_a_corrupt_cron_store_untouched(
    config: Config,
) -> None:
    jobs_path = config.data_dir / "cron" / "jobs.json"
    jobs_path.parent.mkdir(parents=True)
    jobs_path.write_text("{", encoding="utf-8")
    runtime = Runtime(config)

    runtime.start()
    try:
        assert [agent.id for agent in runtime.agents.list()] == ["main"]
        assert runtime.cron_service.list_jobs() == []
    finally:
        await runtime.aclose()
    assert jobs_path.read_text(encoding="utf-8") == "{"


def test_runtime_loads_and_reloads_custom_provider_settings_in_place(config: Config) -> None:
    storage = StorageManager(config.data_dir)
    settings = {
        "name": "Local AI",
        "adapter": "openai_compatible",
        "base_url": "http://127.0.0.1:8080/v1",
        "auth": "none",
        "models": {"chat-model": {"capabilities": {}}},
    }
    storage.save_custom_provider_settings("local-ai", settings)
    runtime = Runtime(config, safe_startup_mode="test")
    runtime.start()
    try:
        providers = runtime.providers
        models = runtime.models
        assert providers.get("local-ai").custom is True
        assert models.get("local-ai", "chat-model").name == "chat-model"

        storage.save_custom_provider_settings(
            "local-ai",
            {
                **settings,
                "name": "Renamed",
                "models": {"chat-model": {"name": "Renamed Model", "capabilities": {}}},
            },
        )
        runtime.reload_custom_providers()

        assert runtime.providers is providers
        assert runtime.models is models
        assert providers.get("local-ai").name == "Renamed"
        assert models.get("local-ai", "chat-model").name == "Renamed Model"
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_loop_started_runtime_owns_background_services_until_aclose(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Sample the Run gauges at once instead of on the one-second production cadence.
    monkeypatch.setattr(
        bootstrap_module,
        "PerformanceService",
        partial(PerformanceService, sample_interval_s=0.0, monitor_interval_s=0.01),
    )
    reset_for_tests()
    runtime = Runtime(config)
    runtime.start()
    try:
        process_manager = runtime.process_manager
        terminal_manager = runtime.terminal_manager
        temporary_files = runtime.storage.temporary_files
        sweepers = [
            manager._sweeper_task  # noqa: SLF001 - lifecycle resource under test.
            for manager in (process_manager, terminal_manager, temporary_files)
        ]
        assert all(task is not None and not task.done() for task in sweepers)
        assert isinstance(runtime.cron_service, CronService)
        provider_usage = runtime.provider_usage
        assert isinstance(provider_usage, ProviderUsageService)
        database = provider_usage.history_database
        assert database is not None
        assert database.path == canonical_database_path(config.data_dir, "provider_usage")
        assert database in runtime.canonical_databases()

        performance = runtime.performance
        watchdog = performance._monitor._thread  # noqa: SLF001 - lifecycle resource under test.
        assert performance.monitoring
        performance.start_recording()
        while "runs.queued" not in (await performance.snapshot())["gauges"]:
            await asyncio.sleep(0.01)
        recording = await performance.stop_recording()
        gauges = (await performance.snapshot())["gauges"]
    finally:
        await runtime.aclose()
        reset_for_tests()

    assert Path(recording["trace_path"]).parent == DataDirectoryLayout(config.data_dir).performance
    assert gauges["runs.active"] == 0 and gauges["runs.queued"] == 0
    assert not performance.monitoring
    assert watchdog is not None and not watchdog.is_alive()
    assert process_manager._sweeper_task is None  # noqa: SLF001
    assert terminal_manager._sweeper_task is None  # noqa: SLF001
    assert temporary_files._sweeper_task is None  # noqa: SLF001
    assert database.is_closed()
    assert runtime.canonical_databases() == ()
    _assert_not_started(runtime)


@pytest.mark.asyncio
async def test_aclose_cancels_work_reaps_processes_and_persists_handed_off_traces(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(config)
    runtime.start()
    run_started = asyncio.Event()
    background_started = asyncio.Event()
    background_count = 0

    async def execute(_run: Run) -> str:
        run_started.set()
        await asyncio.Event().wait()
        return "unreachable"

    async def background_work() -> None:
        nonlocal background_count
        background_count += 1
        if background_count == 2:
            background_started.set()
        await asyncio.Event().wait()

    run = await runtime.chat_run_manager.start(
        SessionAddress(project_id=None, agent_id="main", session_id="shutdown-tracked"), execute
    )
    title_task = asyncio.create_task(background_work())
    reflection_task = asyncio.create_task(background_work())
    title_service = runtime._session_title_service  # noqa: SLF001
    reflection_service = runtime._reflection_service  # noqa: SLF001
    assert title_service is not None and reflection_service is not None
    title_service._background_tasks.add(title_task)  # noqa: SLF001
    reflection_service._background_tasks.add(reflection_task)  # noqa: SLF001
    process_manager = runtime.process_manager
    process_id = await process_manager.spawn(
        "run-one",
        "agent-one",
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env={},
        cwd=config.data_dir,
    )
    tracked = process_manager.get_process(process_id, "agent-one")
    await run_started.wait()
    await background_started.wait()

    # Hand off a Debug trace whose file write stays blocked until aclose drains traces.
    store = DebugTraceStore(runtime.storage.data_dir, trace_limit=10)
    trace_id = "00000000000040008000000000000001"
    trace_path = store.get_data_dir() / "traces" / f"{trace_id}.json"
    write_entered = threading.Event()
    release_write = threading.Event()
    write = debug_store.atomic_write_text

    def blocked_write(path: Path, text: str) -> None:
        write_entered.set()
        release_write.wait(timeout=10)
        write(path, text)

    drain_entered = asyncio.Event()
    trace_written_when_drained: list[bool] = []
    drain = runtime_module.drain_debug_traces

    async def observed_drain() -> None:
        drain_entered.set()
        await drain()
        trace_written_when_drained.append(trace_path.is_file())

    monkeypatch.setattr(debug_store, "atomic_write_text", blocked_write)
    monkeypatch.setattr(runtime_module, "drain_debug_traces", observed_drain)
    assert store.save_trace_in_background(
        trace_id, lambda: {"trace_id": trace_id, "type": "provider_request", "timestamp": "t"}
    )
    try:
        assert await asyncio.to_thread(write_entered.wait, 5)
        closing = asyncio.create_task(runtime.aclose())
        await drain_entered.wait()
    finally:
        release_write.set()
    await closing

    assert trace_written_when_drained == [True]
    assert run.status == RunStatus.CANCELLED
    assert title_task.cancelled()
    assert reflection_task.cancelled()
    assert runtime.chat_runs is None
    assert tracked.status == "killed"
    assert tracked.proc.returncode is not None
    assert tracked.wait_task is not None and tracked.wait_task.done()


@pytest.mark.asyncio
@pytest.mark.parametrize("async_close", [False, True])
async def test_runtime_finishes_cleanup_before_reporting_terminal_shutdown_failure(
    config: Config, monkeypatch: pytest.MonkeyPatch, async_close: bool
) -> None:
    runtime = Runtime(config)
    failure = TerminalManagerError("terminal tree is still alive")
    terminal = SimpleNamespace(
        stop=Mock(side_effect=failure), aclose=AsyncMock(side_effect=failure)
    )
    keep_awake = SimpleNamespace(close=Mock())
    temporary_files = SimpleNamespace(stop=Mock(), aclose=AsyncMock())
    sessions = SimpleNamespace(close=Mock())
    speech = SimpleNamespace(close=Mock(), aclose=AsyncMock())
    logging_close = Mock()
    monkeypatch.setattr(runtime, "_terminal_manager", terminal)
    monkeypatch.setattr(runtime, "_keep_awake", keep_awake)
    monkeypatch.setattr(runtime, "_storage", SimpleNamespace(temporary_files=temporary_files))
    monkeypatch.setattr(runtime, "_chat_sessions", sessions)
    monkeypatch.setattr(runtime, "_speech", speech)
    monkeypatch.setattr(runtime._log_manager, "close", logging_close)  # noqa: SLF001

    with pytest.raises(TerminalManagerError) as raised:
        if async_close:
            await runtime.aclose()
        else:
            runtime.stop()

    assert raised.value is failure
    keep_awake.close.assert_called_once_with()
    sessions.close.assert_called_once_with()
    logging_close.assert_called_once_with()
    if async_close:
        terminal.aclose.assert_awaited_once_with()
        temporary_files.aclose.assert_awaited_once_with()
        speech.aclose.assert_awaited_once_with()
    else:
        terminal.stop.assert_called_once_with()
        temporary_files.stop.assert_called_once_with()
        speech.close.assert_called_once_with()
    with pytest.raises(RuntimeError):
        _ = runtime.terminal_manager
    with pytest.raises(RuntimeError):
        _ = runtime.chat_sessions


@pytest.mark.asyncio
async def test_runtime_imports_history_and_registers_canonical_accounting(config: Config) -> None:
    specs = {spec.name: spec for spec in canonical_database_specs(config.data_dir)}
    assert specs["provider_usage"].path == config.data_dir / "provider-usage.db"
    assert specs["provider_usage"].profile == "canonical"
    runtime = Runtime(config, safe_startup_mode="test")
    runtime.start()
    try:
        session = runtime.chat_sessions.create("legacy")
        seed_history(
            session,
            [ChatMessage.assistant(model="p/m", content="legacy", usage={"input_tokens": 6})],
        )
        await runtime.chat_sessions.archive(session.address)
    finally:
        await runtime.aclose()
    runtime = Runtime(config, safe_startup_mode="test")
    runtime.start()
    recorder = runtime.usage_recorder
    try:
        records = recorder.read_since()[1]
        assert len(records) == 1
        assert records[0].usage["input_tokens"] == 6
        assert recorder.database in runtime.canonical_databases()
        assert specs["model_usage"].path == recorder.database.path
        assert specs["model_usage"].profile == "canonical"
    finally:
        await runtime.aclose()
    assert recorder.database.is_closed()
    assert runtime.canonical_databases() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("withdraw_readiness", [False, True])
async def test_extension_sampling_records_usage_before_returning(
    config: Config, monkeypatch: pytest.MonkeyPatch, withdraw_readiness: bool
) -> None:
    runtime = Runtime(config, safe_startup_mode="test")
    runtime.start()
    recorder = runtime.usage_recorder
    try:

        async def send(*_args: object, **_kwargs: object) -> dict[str, object]:
            if withdraw_readiness:
                runtime._started = False  # noqa: SLF001 - shutdown begins mid-call.
            return {}

        adapter = SimpleNamespace(
            send=AsyncMock(side_effect=send),
            normalize_response=lambda *args, **kwargs: {
                "content": "sample",
                "usage": {"input_tokens": 10, "output_tokens": 2},
            },
            request_context_kwargs=lambda **kwargs: {},
            aclose=AsyncMock(),
        )
        monkeypatch.setattr(runtime, "get_adapter", lambda _: adapter)
        monkeypatch.setattr(
            runtime, "_extension_tool_agent", lambda _: SimpleNamespace(temperature=0)
        )
        monkeypatch.setattr(
            "core.chat.model_resolution.resolve_agent_model_target",
            lambda *args: ("p", "m", "p:api"),
        )
        context = SimpleNamespace(
            agent_id="agent",
            session_id="session",
            project_id=None,
            run_id="run",
            execution_owner=SimpleNamespace(extension="fixture", group_id="group"),
        )
        result = await runtime._sample_extension(  # noqa: SLF001 - Extension sampling seam.
            context, {"messages": [], "max_tokens": 100}
        )
        assert result["model"] == "p/m"
        record = recorder.read_since()[1][0]
        assert record.kind == "extension_sampling"
        assert record.usage["input_tokens"] == 10
        assert record.run_id == "run"
        assert record.owner_name == "fixture"
        adapter.aclose.assert_awaited_once()
    finally:
        await runtime.aclose()
