"""Tests for runtime."""

import asyncio
import json
import logging
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from core.agents.agents import AgentStore
from core.chat import ChatMessage
from core.prompts import SystemPromptManager
from core.providers.credentials import ProviderCredentialResolver
from core.runs import Run, RunStatus
from core.runtime._configuration import _VBOT_ROOT, _detect_vbot_version
from core.runtime.runtime import Runtime
from core.sessions import ChatSessionManager, SessionAddress
from core.skills.skills import SkillRegistry
from core.storage.layout import DATA_DIRECTORY_RELATIVE_PATHS
from core.storage.storage import StorageManager
from core.storage.temp_files import TemporaryFileManager
from core.tools.process_manager import ProcessManager
from core.tools.terminal_manager import TerminalManager, TerminalManagerError
from core.tools.tools import ToolRegistry
from core.utils.config import Config
from tests.core.runtime.runtime_test_support import (
    _authorize_session_store,
)
from tests.core.runtime.runtime_test_support import (
    config as config,
)

CANONICAL_BUILTIN_TOOLS = [
    "analyze_image",
    "apply_patch",
    "bash",
    "calendar",
    "cron",
    "edit",
    "generate_music",
    "generate_video",
    "glob",
    "grep",
    "history",
    "image_generation",
    "memory",
    "process",
    "project",
    "read",
    "session_read",
    "session_search",
    "skill",
    "skill_manage",
    "status",
    "subagent",
    "terminal",
    "text_to_speech",
    "web_fetch",
    "web_search",
    "write",
]


# The four Home Assistant tools ship as a bundled extension and are always
# registered (readiness only hides them from model-facing surfaces until the
# token is set), so they are part of the registered inventory even without a
# token — but absent from provider definitions, which filter on readiness.
HOME_ASSISTANT_TOOLS = [
    "ha_call_service",
    "ha_get_state",
    "ha_list_entities",
    "ha_list_services",
]


CANONICAL_REGISTERED_TOOLS = sorted(CANONICAL_BUILTIN_TOOLS + HOME_ASSISTANT_TOOLS + ["computer"])


def _declared_hidden_session_tools(runtime: Runtime) -> set[str]:
    registry = runtime.extensions
    assert registry is not None
    return {
        declaration.name
        for record in registry.records()
        if record.status == "loaded"
        for declaration in record.declarations.tools
        if declaration.session_scoped
    }


@pytest.mark.asyncio
async def test_runtime_registers_local_speech_and_closes_its_executor(config: Config) -> None:
    runtime = Runtime(config)
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
    assert speech._local_executor._closed


def test_detect_vbot_version_matches_pyproject_single_source() -> None:
    """The reported vBot version tracks the one source of truth in pyproject.toml.

    Regression guard for the split where the System Prompt showed a hardcoded
    default instead of the real release version.
    """
    with (_VBOT_ROOT / "pyproject.toml").open("rb") as handle:
        expected = tomllib.load(handle)["project"]["version"]

    assert _detect_vbot_version() == expected


def test_runtime_feeds_detected_version_into_system_prompt(config: Config) -> None:
    """Runtime wires the detected vBot version into the System Prompt manager."""
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)

    runtime.start()

    assert runtime.system_prompts._vbot_version == _detect_vbot_version()


def test_runtime_start_no_error(tmp_path: Path):
    """Instantiating Runtime and calling start() raises no exception."""
    # Arrange
    logging.getLogger("vbot").handlers = []
    config = Config(data_dir=tmp_path / "data")
    runtime = Runtime(config)

    # Act
    runtime.start()

    # Assert
    assert runtime.logger is not None


def test_runtime_start_survives_corrupt_optional_configuration(tmp_path: Path) -> None:
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
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    _authorize_session_store(data_dir)
    data_dir.joinpath(".env").write_bytes(b"\xff")
    config = Config(data_dir=data_dir)
    config._data["RESOURCES_PATH"] = str(resources_dir)
    runtime = Runtime(config)

    runtime.start()

    assert runtime.models.get("healthy", "model-a").name == "Healthy Model"
    assert runtime.providers.list_ids() == []
    assert runtime.agents.list()
    runtime.stop()


def test_runtime_loads_and_reloads_custom_provider_settings_in_place(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    storage = StorageManager(data_dir)
    storage.save_custom_provider_settings(
        "local-ai",
        {
            "name": "Local AI",
            "adapter": "openai_compatible",
            "base_url": "http://127.0.0.1:8080/v1",
            "auth": "none",
            "models": {"chat-model": {"capabilities": {}}},
        },
    )
    runtime = Runtime(Config(data_dir=data_dir))

    runtime.start()
    providers = runtime.providers
    models = runtime.models
    assert providers.get("local-ai").custom is True
    assert models.get("local-ai", "chat-model").name == "chat-model"

    storage.save_custom_provider_settings(
        "local-ai",
        {
            "name": "Renamed",
            "adapter": "openai_compatible",
            "base_url": "http://127.0.0.1:8080/v1",
            "auth": "none",
            "models": {"chat-model": {"name": "Renamed Model", "capabilities": {}}},
        },
    )
    runtime.reload_custom_providers()

    assert runtime.providers is providers
    assert runtime.models is models
    assert providers.get("local-ai").name == "Renamed"
    assert models.get("local-ai", "chat-model").name == "Renamed Model"


def test_runtime_wires_trigger_service_to_streaming_chat_loop(config: Config) -> None:
    runtime = Runtime(config)

    runtime.start()

    assert runtime.trigger_service._trigger_chat_loop is runtime.streaming_chat_loop
    availability = runtime.chat_loop._dependencies.image_understanding_available  # noqa: SLF001
    assert getattr(availability, "__self__", None) is runtime._image  # noqa: SLF001


def test_runtime_never_creates_automatic_session_snapshots(config: Config, monkeypatch) -> None:
    from core.sessions import snapshots

    def reject_snapshot(*_args, **_kwargs):
        raise AssertionError("normal Runtime operation must not create a Session snapshot")

    monkeypatch.setattr(snapshots, "create_snapshot", reject_snapshot)
    runtime = Runtime(config)
    runtime.start()
    try:
        runtime.chat_sessions.create("main", session_id="no-automatic-snapshot").append(
            ChatMessage.user("normal Runtime write")
        )
    finally:
        runtime.stop()


def test_runtime_stop_runs_cleanly(tmp_path: Path):
    """After start(), calling stop() completes without exception."""
    # Arrange
    logging.getLogger("vbot").handlers = []
    config = Config(data_dir=tmp_path / "data")
    runtime = Runtime(config)
    runtime.start()

    # Act
    runtime.stop()
    # Assert — reaching here without exception is success


def test_runtime_stop_without_start_does_not_crash(tmp_path: Path):
    """Calling stop() before start() is a no-op and does not crash."""
    # Arrange
    logging.getLogger("vbot").handlers = []
    config = Config(data_dir=tmp_path / "data")
    runtime = Runtime(config)

    # Act
    runtime.stop()
    # Assert — reaching here without exception proves it is a safe no-op


def test_phase_two_services_available_after_start(config: Config):
    """Runtime.start() wires all Phase 2 domain services."""
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)

    runtime.start()

    assert isinstance(runtime.storage, StorageManager)
    assert isinstance(runtime.agents, AgentStore)
    assert isinstance(runtime.provider_credentials, ProviderCredentialResolver)
    assert isinstance(runtime.tools, ToolRegistry)
    assert isinstance(runtime.process_manager, ProcessManager)
    assert isinstance(runtime.terminal_manager, TerminalManager)
    assert isinstance(runtime.skills, SkillRegistry)
    assert isinstance(runtime.chat_sessions, ChatSessionManager)
    assert isinstance(runtime.system_prompts, SystemPromptManager)


def test_start_registers_builtin_tools_once(config: Config):
    """Runtime.start() registers each built-in tool exactly once for agent use."""
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)

    runtime.start()

    hidden_session_tools = _declared_hidden_session_tools(runtime)
    tool_names = {tool.name for tool in runtime.tools.list_tools()}
    assert sorted(tool_names - hidden_session_tools) == CANONICAL_REGISTERED_TOOLS
    assert hidden_session_tools <= tool_names
    assert not hidden_session_tools & {
        tool.name for tool in runtime.tools.list_tools(include_catalog_hidden=False)
    }
    assert runtime.tools.get("history").session_scoped is True
    for tool in runtime.tools.list_tools():
        if tool.name in hidden_session_tools:
            continue
        assert tool.result_schema is not None
        assert len(tool.contract.schema_fingerprint) == 64


def test_builtin_provider_definitions_expose_model_visible_metadata_only(config: Config):
    """Runtime tool definitions expose schemas without handlers or context."""
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)

    runtime.start()

    definitions = runtime.tools.provider_definitions()
    definitions_by_name = {definition["name"]: definition for definition in definitions}

    assert sorted(definitions_by_name) == [
        name for name in CANONICAL_BUILTIN_TOOLS if name != "history"
    ]
    for tool_name, definition in definitions_by_name.items():
        tool = runtime.tools.get(tool_name)
        assert set(definition) == {"name", "description", "parameters"}
        assert definition["description"] == tool.description
        assert definition["parameters"] == tool.parameters
        assert "handler" not in definition
        assert "context" not in definition


def test_runtime_start_exposes_canonical_builtin_tools(config: Config):
    """Runtime startup exposes the canonical built-in tool set plus bundled HA."""
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)

    runtime.start()

    hidden_session_tools = _declared_hidden_session_tools(runtime)
    tool_names = {tool.name for tool in runtime.tools.list_tools()}
    assert sorted(tool_names - hidden_session_tools) == CANONICAL_REGISTERED_TOOLS
    assert hidden_session_tools <= tool_names


def test_phase_two_services_inaccessible_before_start(config: Config):
    """Runtime service properties raise a startup error before start()."""
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)

    for attribute_name in (
        "storage",
        "agents",
        "provider_credentials",
        "tools",
        "process_manager",
        "terminal_manager",
        "skills",
        "chat_sessions",
        "system_prompts",
    ):
        with pytest.raises(RuntimeError):
            getattr(runtime, attribute_name)


def test_start_ensures_canonical_data_directories_and_prompt_fragments(config: Config):
    """Runtime.start() prepares the canonical data-directory structure."""
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)

    runtime.start()

    data_dir = runtime.storage.data_dir
    for directory_name in DATA_DIRECTORY_RELATIVE_PATHS:
        assert (data_dir / directory_name).is_dir()
    assert (data_dir / ".env").is_file()
    assert (data_dir / "settings.json").is_file()
    for legacy_name in (
        ".tmp",
        "attachments",
        "images",
        "speech",
        "models",
        "debug",
        "temp",
        "provider-usage",
    ):
        assert not (data_dir / legacy_name).exists()
    # Startup must NOT seed fragment copies into the data dir: a seeded copy would
    # shadow the bundled resource forever and freeze prompt defaults at first-run
    # state. Bundled fragments are read live; only a hand-created copy overrides.
    assert not (data_dir / "prompts" / "runtime.md").exists()


def test_start_bootstraps_main_agent_when_data_dir_is_empty(config: Config):
    """Runtime.start() leaves a new data dir with a usable default agent."""
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)

    runtime.start()

    agents = runtime.agents.list()
    assert [agent.id for agent in agents] == ["main"]
    main_agent = agents[0]
    assert main_agent.name == "Main"
    assert main_agent.current_session_id
    assert (
        runtime.chat_sessions.get(
            SessionAddress(
                project_id=None, agent_id="main", session_id=main_agent.current_session_id
            )
        ).load()
        == []
    )


def test_runtime_stop_clears_phase_two_services(config: Config):
    """After stop(), Phase 2 service properties are inaccessible again."""
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()

    runtime.stop()

    with pytest.raises(RuntimeError):
        _ = runtime.storage
    with pytest.raises(RuntimeError):
        _ = runtime.provider_credentials
    with pytest.raises(RuntimeError):
        _ = runtime.process_manager
    with pytest.raises(RuntimeError):
        _ = runtime.terminal_manager


@pytest.mark.asyncio
async def test_runtime_starts_and_stops_process_manager_sweeper(config: Config) -> None:
    """Runtime owns process and temporary-file cleanup on the running loop."""
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)

    runtime.start()
    process_manager = runtime.process_manager
    terminal_manager = runtime.terminal_manager
    temporary_files = runtime.storage.temporary_files

    assert process_manager._sweeper_task is not None
    assert not process_manager._sweeper_task.done()
    assert terminal_manager._sweeper_task is not None
    assert not terminal_manager._sweeper_task.done()
    assert temporary_files._sweeper_task is not None
    assert not temporary_files._sweeper_task.done()

    runtime.stop()

    assert process_manager._sweeper_task is None
    assert terminal_manager._sweeper_task is None
    assert temporary_files._sweeper_task is None


def test_runtime_failed_start_stops_started_resources(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    logging.getLogger("vbot").handlers = []
    stopped: list[str] = []
    original_temporary_stop = TemporaryFileManager.stop
    original_process_stop = ProcessManager.stop

    def stop_temporary_files(manager: TemporaryFileManager) -> None:
        stopped.append("temporary_files")
        original_temporary_stop(manager)

    def stop_processes(manager: ProcessManager) -> None:
        stopped.append("process_manager")
        original_process_stop(manager)

    def fail_terminal_start(_runtime: Runtime) -> None:
        raise RuntimeError("terminal startup failed")

    monkeypatch.setattr(TemporaryFileManager, "stop", stop_temporary_files)
    monkeypatch.setattr(ProcessManager, "stop", stop_processes)
    monkeypatch.setattr(Runtime, "_start_terminal_manager", fail_terminal_start)

    with pytest.raises(RuntimeError, match="terminal startup failed"):
        Runtime(config).start()

    assert "process_manager" in stopped
    assert "temporary_files" in stopped


@pytest.mark.asyncio
async def test_runtime_aclose_reaps_tracked_processes(config: Config) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    process_manager = runtime.process_manager
    temporary_files = runtime.storage.temporary_files
    process_id = await process_manager.spawn(
        "run-one",
        "agent-one",
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env={},
        cwd=config.data_dir,
    )
    tracked = process_manager.get_process(process_id, "agent-one")

    await runtime.aclose()

    assert tracked.status == "killed"
    assert tracked.proc.returncode is not None
    assert tracked.wait_task is not None and tracked.wait_task.done()
    assert temporary_files._sweeper_task is None
    with pytest.raises(RuntimeError):
        _ = runtime.process_manager


@pytest.mark.asyncio
async def test_runtime_aclose_cancels_runs_titles_and_reflections(config: Config) -> None:
    logging.getLogger("vbot").handlers = []
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
        SessionAddress(project_id=None, agent_id="main", session_id="shutdown-tracked"),
        execute,
    )
    title_task = asyncio.create_task(background_work())
    reflection_task = asyncio.create_task(background_work())
    title_service = runtime._session_title_service  # noqa: SLF001
    reflection_service = runtime._reflection_service  # noqa: SLF001
    assert title_service is not None
    assert reflection_service is not None
    title_service._background_tasks.add(title_task)  # noqa: SLF001
    reflection_service._background_tasks.add(reflection_task)  # noqa: SLF001
    await run_started.wait()
    await background_started.wait()

    await runtime.aclose()

    assert run.status == RunStatus.CANCELLED
    assert title_task.cancelled()
    assert reflection_task.cancelled()
    assert runtime.chat_runs is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner", "method"),
    [
        ("StorageManager", "ensure_directories"),
        ("StorageManager", "load_settings"),
        ("StorageManager", "load_environment"),
        ("StorageManager", "load_custom_providers_settings"),
        ("TokenStore", None),
        ("ModelRegistry", "load"),
        ("TaskModelService", None),
        ("ImageService", None),
        ("Runtime", "_start_provider_usage_service"),
    ],
)
async def test_failed_bootstrap_cleans_resources_and_can_retry(
    config: Config, monkeypatch: pytest.MonkeyPatch, owner: str, method: str | None
) -> None:
    import core.runtime._bootstrap as bootstrap_module

    runtime = Runtime(config)
    resources: dict[str, Any] = {}
    failure = RuntimeError("bootstrap test failure")

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        resources["storage"] = runtime._storage
        resources["keep_awake"] = runtime._keep_awake
        resources["speech"] = runtime._speech
        if runtime._keep_awake is not None:
            resources["closed_power"] = False
            original_close = runtime._keep_awake.close

            def close_power() -> None:
                resources["closed_power"] = True
                original_close()

            monkeypatch.setattr(runtime._keep_awake, "close", close_power)
        raise failure

    with monkeypatch.context() as patch:
        if method is None:
            patch.setattr(bootstrap_module, owner, fail)
        else:
            patch.setattr(
                Runtime if owner == "Runtime" else getattr(bootstrap_module, owner), method, fail
            )
        with pytest.raises(RuntimeError) as caught:
            runtime.start()
    assert caught.value is failure
    assert not runtime._started
    assert runtime._started_at is None
    assert runtime._startup_id is None
    assert runtime._storage is None
    assert runtime._speech is None
    assert runtime._video is None
    assert runtime._music is None
    assert not runtime._log_manager._handlers
    assert not runtime._log_manager._configured
    if resources["storage"] is not None:
        assert resources["storage"].temporary_files._sweeper_task is None
    if resources["keep_awake"] is not None:
        assert resources["closed_power"]

    try:
        runtime.start()
        assert runtime._started
        assert runtime.storage is not resources["storage"]
        assert runtime._log_manager._configured
    finally:
        await runtime.aclose()


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
    monkeypatch.setattr(runtime._log_manager, "close", logging_close)

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


def test_service_access_preserves_readiness_availability_and_readonly_contract(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(config)
    with pytest.raises(RuntimeError, match="Runtime not started"):
        _ = runtime.providers
    monkeypatch.setattr(runtime, "_started", True)
    with pytest.raises(RuntimeError, match="Provider registry not available"):
        _ = runtime.providers
    with pytest.raises(AttributeError, match="has no setter"):
        runtime.providers = object()
    runtime.stop()
    with pytest.raises(RuntimeError, match="Runtime not started"):
        _ = runtime.providers
