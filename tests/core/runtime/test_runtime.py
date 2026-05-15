"""Tests for the Runtime bootstrap class."""

import asyncio
import logging
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.agents.agents import AgentStore, SystemPromptManager
from core.channels import ChannelService
from core.chat.chat import ChatLoop, ChatSessionManager
from core.chat.runs import ChatRunManager, RunCancelledError
from core.providers.credentials import ProviderCredentialResolver
from core.runtime.runtime import Runtime
from core.skills.skills import SkillRegistry
from core.storage.storage import StorageManager
from core.tools.process_manager import ProcessManager
from core.tools.subagent import SubAgentBatchTracker
from core.tools.tools import ToolRegistry
from core.utils.config import Config

CANONICAL_BUILTIN_TOOLS = [
    "bash",
    "cron",
    "edit",
    "glob",
    "grep",
    "process",
    "read",
    "subagent",
    "subagent_result",
    "write",
]
RELOADED_SKILL_NAME = "runtime-reloaded-skill"


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(data_dir=tmp_path / "data")


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


def test_runtime_logger_exists_after_start(tmp_path: Path):
    """After start(), runtime.logger is a valid logger object."""
    # Arrange
    logging.getLogger("vbot").handlers = []
    config = Config(data_dir=tmp_path / "data")
    runtime = Runtime(config)

    # Act
    runtime.start()

    # Assert
    logger = runtime.logger
    assert logger is not None
    assert hasattr(logger, "info")
    assert hasattr(logger, "error")
    assert hasattr(logger, "debug")
    # Verify it is a logging.Logger (the concrete implementation)
    assert isinstance(logger, logging.Logger)


def test_runtime_start_creates_date_named_log_file(config: Config) -> None:
    """Runtime logging writes to the active daily log file under the data dir."""
    runtime = Runtime(config)

    runtime.start()
    runtime.stop()

    log_files = list((config.data_dir / "logs").iterdir())
    assert len(log_files) == 1
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", log_files[0].name)


def test_runtime_start_logs_startup_and_shutdown_with_required_format(config: Config) -> None:
    """Runtime lifecycle logs use the required shared log format."""
    runtime = Runtime(config)

    runtime.start()
    runtime.stop()

    log_file = next((config.data_dir / "logs").iterdir())
    lines = log_file.read_text(encoding="utf-8").strip().splitlines()

    assert any(line.endswith("[INFO] vbot.core - Runtime startup initiated") for line in lines)
    assert any(line.endswith("[INFO] vbot.core - Runtime started") for line in lines)
    assert any(line.endswith("[INFO] vbot.core - Runtime stopped") for line in lines)


def test_runtime_warning_logs_use_shared_manager_format(config: Config) -> None:
    """Runtime warnings emitted during startup use the managed logger contract."""
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.data_dir.joinpath("settings.json").write_text(
        '{"skill_directories": [null]}\n',
        encoding="utf-8",
    )
    runtime = Runtime(config)

    runtime.start()
    runtime.stop()

    log_file = next((config.data_dir / "logs").iterdir())
    contents = log_file.read_text(encoding="utf-8")

    assert "[WARN] vbot.core - Ignoring invalid skill directory setting: None" in contents


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
    assert isinstance(runtime.skills, SkillRegistry)
    assert isinstance(runtime.chat_sessions, ChatSessionManager)
    assert isinstance(runtime.system_prompts, SystemPromptManager)


def test_start_registers_builtin_tools_once(config: Config):
    """Runtime.start() registers each built-in tool exactly once for agent use."""
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)

    runtime.start()

    tool_names = sorted(tool.name for tool in runtime.tools.list_tools())
    assert tool_names == CANONICAL_BUILTIN_TOOLS


def test_builtin_provider_definitions_expose_model_visible_metadata_only(config: Config):
    """Runtime tool definitions expose schemas without handlers or context."""
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)

    runtime.start()

    definitions = runtime.tools.provider_definitions()
    definitions_by_name = {definition["name"]: definition for definition in definitions}

    assert sorted(definitions_by_name) == CANONICAL_BUILTIN_TOOLS
    for tool_name, definition in definitions_by_name.items():
        tool = runtime.tools.get(tool_name)
        assert set(definition) == {"name", "description", "parameters"}
        assert definition["description"] == tool.description
        assert definition["parameters"] == tool.parameters
        assert "handler" not in definition
        assert "context" not in definition


def test_runtime_start_exposes_canonical_builtin_tools(config: Config):
    """Runtime startup exposes the canonical built-in tool set."""
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)

    runtime.start()

    tool_names = sorted(tool.name for tool in runtime.tools.list_tools())
    assert tool_names == CANONICAL_BUILTIN_TOOLS


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
        "skills",
        "chat_sessions",
        "system_prompts",
    ):
        with pytest.raises(RuntimeError, match="not started"):
            getattr(runtime, attribute_name)


def test_start_ensures_data_directories_and_prompt_fragments(config: Config):
    """Runtime.start() prepares the Phase 2 data directory structure."""
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)

    runtime.start()

    data_dir = runtime.storage.data_dir
    for directory_name in (
        ".tmp",
        "agents",
        "archive",
        "channels",
        "cron",
        "oauth",
        "prompts",
        "skills",
        "logs",
    ):
        assert (data_dir / directory_name).is_dir()
    assert (data_dir / "prompts" / "system.md").is_file()


def test_runtime_resolve_environment_credential_prefers_process_env(
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.data_dir.joinpath(".env").write_text(
        "TELEGRAM_BOT_TOKEN_TG_ASSISTANT=fallback-token\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_TG_ASSISTANT", "process-token")

    runtime = Runtime(config)
    runtime.start()

    assert (
        runtime.resolve_environment_credential("TELEGRAM_BOT_TOKEN_TG_ASSISTANT") == "process-token"
    )

    runtime.stop()


def test_runtime_resolve_environment_credential_uses_data_dir_fallback(
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.data_dir.joinpath(".env").write_text(
        "TELEGRAM_BOT_TOKEN_TG_ASSISTANT=fallback-token\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN_TG_ASSISTANT", raising=False)

    runtime = Runtime(config)
    runtime.start()

    assert (
        runtime.resolve_environment_credential("TELEGRAM_BOT_TOKEN_TG_ASSISTANT")
        == "fallback-token"
    )

    runtime.stop()


@pytest.mark.asyncio
async def test_runtime_start_does_not_crash_when_channel_adapter_cannot_start(
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN_TG_ASSISTANT", raising=False)
    runtime = Runtime(config)

    seed_agent_store = AgentStore(
        config.data_dir,
        template_dir=runtime._resolve_resources_path() / "workspace-templates",  # noqa: SLF001
    )
    seed_agent_store.create("assistant", "Assistant")

    channel_dir = config.data_dir / "channels" / "tg-assistant"
    channel_dir.mkdir(parents=True, exist_ok=True)
    channel_dir.joinpath("channel.json").write_text(
        "\n".join(
            (
                "{",
                '  "id": "tg-assistant",',
                '  "platform": "telegram",',
                '  "agent_id": "assistant",',
                '  "dm_scope": "per_conversation",',
                '  "allowed_chat_ids": [12345],',
                '  "token_env_var": "TELEGRAM_BOT_TOKEN_TG_ASSISTANT",',
                '  "enabled": true',
                "}",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    runtime.start()

    assert runtime.channel_service.has_active_channels() is False

    runtime.stop()


@pytest.mark.asyncio
async def test_runtime_start_registers_channel_send_when_enabled_channel_starts(
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = Runtime(config)

    seed_agent_store = AgentStore(
        config.data_dir,
        template_dir=runtime._resolve_resources_path() / "workspace-templates",  # noqa: SLF001
    )
    seed_agent_store.create("assistant", "Assistant")

    channel_dir = config.data_dir / "channels" / "tg-assistant"
    channel_dir.mkdir(parents=True, exist_ok=True)
    channel_dir.joinpath("channel.json").write_text(
        "\n".join(
            (
                "{",
                '  "id": "tg-assistant",',
                '  "platform": "telegram",',
                '  "agent_id": "assistant",',
                '  "dm_scope": "per_conversation",',
                '  "allowed_chat_ids": [12345],',
                '  "token_env_var": "TELEGRAM_BOT_TOKEN_TG_ASSISTANT",',
                '  "enabled": true',
                "}",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    adapter = _BlockingChannelAdapter()
    monkeypatch.setattr(ChannelService, "_create_adapter", lambda _service, _config: adapter)

    runtime.start()
    await asyncio.wait_for(adapter.started.wait(), timeout=1)

    tool_names = sorted(tool.name for tool in runtime.tools.list_tools())
    assert "channel_send" in tool_names
    assert runtime.channel_service.has_active_channels() is True

    runtime.stop()
    await asyncio.wait_for(adapter.stopped.wait(), timeout=1)
    await asyncio.sleep(0)


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
    assert runtime.chat_sessions.get("main", main_agent.current_session_id).load() == []


def test_runtime_stop_clears_phase_two_services(config: Config):
    """After stop(), Phase 2 service properties are inaccessible again."""
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()

    runtime.stop()

    with pytest.raises(RuntimeError, match="not started"):
        _ = runtime.storage
    with pytest.raises(RuntimeError, match="not started"):
        _ = runtime.provider_credentials
    with pytest.raises(RuntimeError, match="not started"):
        _ = runtime.process_manager


@pytest.mark.asyncio
async def test_runtime_starts_and_stops_process_manager_sweeper(config: Config) -> None:
    """Runtime owns the ProcessManager lifecycle when an event loop is running."""
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)

    runtime.start()
    process_manager = runtime.process_manager

    assert process_manager._sweeper_task is not None
    assert not process_manager._sweeper_task.done()

    runtime.stop()

    assert process_manager._sweeper_task is None


def test_runtime_registers_bash_and_process_tools(config: Config) -> None:
    """Runtime.start() registers host process tools backed by ProcessManager."""
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)

    runtime.start()

    assert runtime.tools.get("bash").name == "bash"
    assert runtime.tools.get("process").name == "process"


def test_runtime_registers_subagent_tools(config: Config) -> None:
    """Runtime.start() registers sub-agent tools and owns their batch tracker."""
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)

    runtime.start()

    assert isinstance(runtime._subagent_batch_tracker, SubAgentBatchTracker)  # noqa: SLF001
    assert runtime.tools.get("subagent").name == "subagent"
    assert runtime.tools.get("subagent_result").name == "subagent_result"


@pytest.mark.asyncio
async def test_runtime_process_manager_cancels_run_scoped_sessions(config: Config) -> None:
    """ProcessManager cancellation kills all processes associated with one Run."""
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    process_manager = runtime.process_manager
    session_id = await process_manager.spawn(
        "run-one",
        "agent-one",
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env={},
        cwd=config.data_dir,
    )

    process_manager.cancel_scope("run-one")
    poll_result = await process_manager.poll(session_id, "agent-one", timeout_ms=1000)

    assert poll_result["status"] == "killed"


@pytest.mark.asyncio
async def test_chat_run_cancellation_calls_runtime_process_manager(tmp_path: Path) -> None:
    """ChatLoop wires Run cancellation to Runtime.process_manager.cancel_scope()."""
    adapter = _BlockingAdapter()
    process_manager = _RecordingProcessManager()
    runtime = _ChatRuntimeStub(tmp_path, adapter, process_manager)
    runtime.chat_sessions.create("agent-one", session_id="session-one")
    chat_loop = ChatLoop(runtime)

    run = await chat_loop.start_run("agent-one", "hello", session_id="session-one")
    await adapter.request_started.wait()
    run.request_cancel()

    with pytest.raises(RunCancelledError):
        await run.wait()

    assert process_manager.cancelled_scopes == [run.id]


def test_reload_skills_updates_system_prompt_skill_registry(config: Config, tmp_path: Path):
    """Runtime.reload_skills() makes prompt catalogs use the fresh skill registry."""
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    agent = runtime.agents.update("main", allowed_skills=[RELOADED_SKILL_NAME])
    skill_root = tmp_path / "team-skills"
    _write_test_skill(
        skill_root,
        RELOADED_SKILL_NAME,
        "Fresh skill loaded after settings update.",
    )

    prompt_before_reload = runtime.system_prompts.build_system_prompt(agent)

    runtime.storage.update_skill_directory_settings([str(skill_root)])
    runtime.reload_skills()
    prompt_after_reload = runtime.system_prompts.build_system_prompt(agent)

    assert f"<name>{RELOADED_SKILL_NAME}</name>" not in prompt_before_reload
    assert f"<name>{RELOADED_SKILL_NAME}</name>" in prompt_after_reload
    assert "Fresh skill loaded after settings update." in prompt_after_reload


def test_reload_skills_updates_provider_skill_tool_visibility(config: Config, tmp_path: Path):
    """Runtime.reload_skills() makes provider tools use the fresh skill registry."""
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    agent = runtime.agents.update(
        "main",
        allowed_tools=[],
        allowed_skills=[RELOADED_SKILL_NAME],
    )
    skill_root = tmp_path / "team-skills"
    _write_test_skill(
        skill_root,
        RELOADED_SKILL_NAME,
        "Fresh skill loaded after settings update.",
    )

    definitions_before_reload = runtime.system_prompts.provider_tool_definitions(agent)

    runtime.storage.update_skill_directory_settings([str(skill_root)])
    runtime.reload_skills()
    definitions_after_reload = runtime.system_prompts.provider_tool_definitions(agent)

    assert [definition["name"] for definition in definitions_before_reload] == []
    assert [definition["name"] for definition in definitions_after_reload] == ["skill"]


def _write_test_skill(skill_root: Path, name: str, description: str) -> None:
    skill_dir = skill_root / name
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nUse this skill.\n",
        encoding="utf-8",
    )


class _BlockingChannelAdapter:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def start(self) -> None:
        self.started.set()
        await asyncio.Future()

    async def stop(self) -> None:
        self.stopped.set()

    async def send(self, _message: str, _platform_target: str) -> None:
        return


class _BlockingAdapter:
    def __init__(self) -> None:
        self.request_started = asyncio.Event()

    async def send(self, _messages: object, **_kwargs: object) -> dict[str, object]:
        self.request_started.set()
        await asyncio.Event().wait()
        return {"content": "unreachable", "tool_calls": None}

    def normalize_response(self, response: dict[str, object]) -> dict[str, object]:
        return response

    async def aclose(self) -> None:
        return None


class _RecordingProcessManager:
    def __init__(self) -> None:
        self.cancelled_scopes: list[str] = []

    def cancel_scope(self, scope_key: str) -> None:
        self.cancelled_scopes.append(scope_key)


class _ChatRuntimeStub:
    def __init__(
        self,
        tmp_path: Path,
        adapter: _BlockingAdapter,
        process_manager: _RecordingProcessManager,
    ) -> None:
        self.agents = _StubAgents()
        self.providers = _StubProviders()
        self.provider_credentials = _StubCredentials()
        self.chat_sessions = ChatSessionManager(tmp_path)
        self.chat_runs = ChatRunManager()
        self.system_prompts = _StubPrompts()
        self.tools = ToolRegistry()
        self.storage = SimpleNamespace(data_dir=tmp_path)
        self._process_manager = process_manager
        self._adapter = adapter

    def get_adapter(self, _provider_id: str, _connection_id: str) -> _BlockingAdapter:
        return self._adapter

    @property
    def process_manager(self) -> _RecordingProcessManager:
        return self._process_manager


class _StubAgents:
    def get(self, agent_id: str) -> object:
        return SimpleNamespace(
            id=agent_id,
            model="provider/model",
            connection="provider:default",
            temperature=0.0,
            thinking_effort="",
            allowed_tools=["*"],
            allowed_skills=["*"],
            workspace="",
        )


class _StubProviders:
    def get(self, provider_id: str) -> object:
        return SimpleNamespace(id=provider_id)


class _StubCredentials:
    def has_credentials(self, _provider_id: str, _connection_id: str | None = None) -> bool:
        return True


class _StubPrompts:
    def build_system_prompt(self, _agent: object) -> str:
        return "System prompt"

    def provider_tool_definitions(self, _agent: object) -> list[dict[str, object]]:
        return []
