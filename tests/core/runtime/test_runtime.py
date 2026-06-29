"""Tests for the Runtime bootstrap class."""

import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.agents.agents import AgentStore
from core.channels import ChannelService
from core.chat.chat import ChatLoop
from core.prompts import LayoutEntry, SystemPromptManager
from core.providers.credentials import ProviderCredentialResolver
from core.providers.providers import ProviderRegistry
from core.recall import JsonlSessionRecallBackend, SqliteFtsRecallBackend
from core.runs import ChatRunManager, RunCancelledError
from core.runtime.runtime import Runtime
from core.sessions import ChatSessionManager
from core.skills.skills import SkillRegistry
from core.storage.storage import StorageManager
from core.subagents import SubAgentCoordinator
from core.tools.process_manager import ProcessManager
from core.tools.tools import ToolRegistry
from core.utils.config import Config

CANONICAL_BUILTIN_TOOLS = [
    "bash",
    "cron",
    "edit",
    "glob",
    "grep",
    "image_generation",
    "memory",
    "process",
    "read",
    "session_search",
    "status",
    "subagent",
    "subagent_result",
    "text_to_speech",
    "web_fetch",
    "web_search",
    "write",
]
RELOADED_SKILL_NAME = "runtime-reloaded-skill"


def _clear_provider_credential_environment(monkeypatch: pytest.MonkeyPatch) -> None:
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

    if seeded_credential_key is not None:
        monkeypatch.setenv(seeded_credential_key, "test-startup-credential")


def _expected_startup_inventory_message(runtime: Runtime) -> str:
    provider_ids = runtime.providers.list_ids()
    usable_provider_count = 0
    total_connection_count = 0
    usable_connection_count = 0

    for provider_id in provider_ids:
        provider_config = runtime.providers.get(provider_id)
        provider_is_usable = False

        for connection in provider_config.connections:
            total_connection_count += 1
            connection_id = f"{provider_id}:{connection.id}"
            if runtime.provider_credentials.has_credentials(provider_id, connection_id):
                usable_connection_count += 1
                provider_is_usable = True

        if provider_is_usable:
            usable_provider_count += 1

    return (
        "Runtime inventory: "
        f"{len(runtime.tools.list_tools())} tools, "
        f"{len(runtime.skills.list_all())} skills, "
        f"{usable_provider_count}/{len(provider_ids)} usable providers, "
        f"{usable_connection_count}/{total_connection_count} usable connections"
    )


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


def test_runtime_wires_trigger_service_to_streaming_chat_loop(config: Config) -> None:
    runtime = Runtime(config)

    runtime.start()

    assert runtime.trigger_service._trigger_chat_loop is runtime.streaming_chat_loop


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
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}\.log", log_files[0].name)


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


def test_runtime_start_logs_inventory_counts(
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime startup logs loaded tool, skill, and provider inventory counts."""
    _clear_provider_credential_environment(monkeypatch)
    runtime = Runtime(config)

    runtime.start()
    expected_message = _expected_startup_inventory_message(runtime)
    runtime.stop()

    log_file = next((config.data_dir / "logs").iterdir())
    contents = log_file.read_text(encoding="utf-8")

    assert f"[INFO] vbot.core - {expected_message}" in contents


def test_runtime_warning_logs_use_shared_manager_format(config: Config) -> None:
    """Runtime warnings emitted during startup use the managed logger contract."""
    config.data_dir.mkdir(parents=True, exist_ok=True)
    extra_skills_dir = config.data_dir / "extra-skills"
    broken_skill_dir = extra_skills_dir / "broken"
    broken_skill_dir.mkdir(parents=True)
    broken_skill_dir.joinpath("SKILL.md").write_text(
        "---\ndescription: Missing a skill name.\n---\n\n# Broken\n",
        encoding="utf-8",
    )
    config.data_dir.joinpath("settings.json").write_text(
        json.dumps({"skill_directories": [str(extra_skills_dir)]}),
        encoding="utf-8",
    )
    runtime = Runtime(config)

    runtime.start()
    runtime.stop()

    log_file = next((config.data_dir / "logs").iterdir())
    contents = log_file.read_text(encoding="utf-8")

    assert "[WARN] vbot.core - Loaded skills with " in contents
    assert " invalid skill directories; see vbot.skills warnings for details" in contents


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


def test_runtime_selects_jsonl_recall_backend_by_default(config: Config) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)

    runtime.start()

    assert isinstance(runtime.recall_backend, JsonlSessionRecallBackend)


def test_runtime_selects_sqlite_recall_backend_from_settings(config: Config) -> None:
    logging.getLogger("vbot").handlers = []
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.data_dir.joinpath("settings.json").write_text(
        json.dumps({"recall": {"backend": "sqlite_fts"}}),
        encoding="utf-8",
    )
    runtime = Runtime(config)

    runtime.start()

    assert isinstance(runtime.recall_backend, SqliteFtsRecallBackend)


def test_runtime_unknown_recall_backend_falls_back_to_jsonl(config: Config) -> None:
    logging.getLogger("vbot").handlers = []
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.data_dir.joinpath("settings.json").write_text(
        json.dumps({"recall": {"backend": "team_backend"}}),
        encoding="utf-8",
    )
    runtime = Runtime(config)

    runtime.start()

    assert isinstance(runtime.recall_backend, JsonlSessionRecallBackend)


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
    assert (data_dir / "prompts" / "runtime.md").is_file()


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
    assert runtime.channel_service.is_failed("tg-assistant") is True
    assert (
        runtime.channel_service.failure_reason("tg-assistant")
        == "Missing Telegram token in environment variable: TELEGRAM_BOT_TOKEN_TG_ASSISTANT"
    )

    runtime.stop()


@pytest.mark.asyncio
async def test_runtime_start_does_not_crash_when_channel_agent_is_missing(
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_TG_ASSISTANT", "test-token")
    channel_dir = config.data_dir / "channels" / "tg-assistant"
    channel_dir.mkdir(parents=True, exist_ok=True)
    channel_dir.joinpath("channel.json").write_text(
        "\n".join(
            (
                "{",
                '  "id": "tg-assistant",',
                '  "platform": "telegram",',
                '  "agent_id": "missing-agent",',
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
    runtime = Runtime(config)

    runtime.start()

    assert runtime.agents.get("main").id == "main"
    assert runtime.channel_service.has_active_channels() is False
    assert runtime.channel_service.is_failed("tg-assistant") is True
    assert (
        runtime.channel_service.failure_reason("tg-assistant") == "Unknown agent_id: missing-agent"
    )

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


@pytest.mark.asyncio
async def test_runtime_aclose_reaps_process_sessions(config: Config) -> None:
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
    session = process_manager.get_session(session_id, "agent-one")

    await runtime.aclose()

    assert session.status == "killed"
    assert session.proc.returncode is not None
    assert session.wait_task is not None and session.wait_task.done()
    with pytest.raises(RuntimeError, match="not started"):
        _ = runtime.process_manager


def test_runtime_registers_bash_and_process_tools(config: Config) -> None:
    """Runtime.start() registers host process tools backed by ProcessManager."""
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)

    runtime.start()

    assert runtime.tools.get("bash").name == "bash"
    assert runtime.tools.get("process").name == "process"


def test_runtime_registers_subagent_tools(config: Config) -> None:
    """Runtime.start() registers sub-agent tools and owns their coordinator."""
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)

    runtime.start()

    assert isinstance(runtime._subagent_coordinator, SubAgentCoordinator)  # noqa: SLF001
    assert runtime.tools.get("subagent").name == "subagent"
    assert runtime.tools.get("subagent_result").name == "subagent_result"


@pytest.mark.asyncio
async def test_runtime_process_manager_cancels_run_scoped_sessions(config: Config) -> None:
    """ProcessManager cancellation kills all processes associated with one Run."""
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    try:
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
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_chat_run_cancellation_calls_runtime_process_manager(tmp_path: Path) -> None:
    """ChatLoop wires Run cancellation to Runtime.process_manager.cancel_scope()."""
    adapter = _BlockingAdapter()
    process_manager = _RecordingProcessManager()
    runtime: Any = _ChatRuntimeStub(tmp_path, adapter, process_manager)
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

    runtime.storage.update_settings_sections({"skills": {"directories": [str(skill_root)]}})
    runtime.reload_skills()
    prompt_after_reload = runtime.system_prompts.build_system_prompt(agent)

    assert f"<name>{RELOADED_SKILL_NAME}</name>" not in prompt_before_reload
    assert f"<name>{RELOADED_SKILL_NAME}</name>" in prompt_after_reload
    assert "Fresh skill loaded after settings update." in prompt_after_reload


def test_persisted_block_layout_and_override_flow_through_real_storage(config: Config):
    """The runtime wires the real StorageManager block store into the prompt manager.

    Guards the composition-root seam (_StorageManagerBlockStore): a persisted block
    override and a persisted layout written through StorageManager's block write API
    must actually shape the assembled prompt — i.e. the manager is NOT silently on
    the EmptyBlockStore fallback. The adapter bridges both the method names
    (read_layout → read_block_layout) and the scope convention (default scope key →
    None storage token).
    """
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    try:
        agent = runtime.agents.get("main")

        # Baseline: bundled tools block present, custom marker absent, skills present.
        baseline = runtime.system_prompts.build_system_prompt(agent)
        assert "## Tool Call Style" in baseline
        assert "PERSISTED-OVERRIDE-MARKER" not in baseline
        assert "## Available Skills" in baseline

        # Persist a default-scope override for the tools block and a layout that
        # disables the skills block — both through StorageManager's block write API
        # (scope None = default). The adapter reads these live on the next build.
        runtime.storage.write_block_override(
            None,
            "core:tools",
            "## PERSISTED-OVERRIDE-MARKER\n{generated:tool_list}",
        )
        runtime.storage.write_block_layout(
            None,
            [
                LayoutEntry(id="core:runtime", enabled=True, source="core"),
                LayoutEntry(id="core:tools", enabled=True, source="core"),
                LayoutEntry(id="core:skills", enabled=False, source="core"),
            ],
        )

        updated = runtime.system_prompts.build_system_prompt(agent)

        # The persisted override replaced the bundled tools text (override cascade).
        assert "## PERSISTED-OVERRIDE-MARKER" in updated
        assert "## Tool Call Style" not in updated
        # The persisted layout disabled the skills block (gate 1).
        assert "## Available Skills" not in updated
    finally:
        runtime.stop()


def test_block_edit_facade_writes_flow_through_real_storage(config: Config):
    """The block-edit facade's writes round-trip through the real StorageManager.

    Guards the write half of the composition-root seam (_StorageManagerBlockStore):
    update_block / set_layout / create_block / remove_block must persist through
    StorageManager's block write API (scope translation included) and shape the very
    next assembled prompt — proving the adapter is not on the EmptyBlockStore no-op
    sink. The facade lives on the runtime's SystemPromptManager.
    """
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    try:
        manager = runtime.system_prompts
        agent = runtime.agents.get("main")

        # update_block: a default-scope override on the tools block replaces the
        # bundled text on the next build (write_block_override → storage scope None).
        manager.update_block("core:tools", "## FACADE-TOOLS-MARKER\n{generated:tool_list}")
        assert (
            runtime.storage.read_block_override(None, "core:tools")
            == "## FACADE-TOOLS-MARKER\n{generated:tool_list}"
        )
        assert "## FACADE-TOOLS-MARKER" in manager.build_system_prompt(agent)

        # set_layout: disabling the skills block persists and gates it out (prune via
        # storage). An unknown id is tolerated — pruned, not an error.
        manager.set_layout(
            [
                {"id": "core:runtime", "enabled": True},
                {"id": "core:tools", "enabled": True},
                {"id": "core:skills", "enabled": False},
                {"id": "extension:gone", "enabled": True},
            ]
        )
        persisted_ids = {entry.id for entry in runtime.storage.read_block_layout(None)}
        assert "extension:gone" not in persisted_ids  # contributor-gone id pruned
        assert "## Available Skills" not in manager.build_system_prompt(agent)

        # create_block then remove_block: the custom block's override file and layout
        # entry are written, then both removed, all through the real storage seam.
        manager.create_block("greeting", "Hello from a custom block.")
        assert runtime.storage.read_block_override(None, "user:greeting") is not None
        assert "Hello from a custom block." in manager.build_system_prompt(agent)

        manager.remove_block("user:greeting")
        assert runtime.storage.read_block_override(None, "user:greeting") is None
        assert "Hello from a custom block." not in manager.build_system_prompt(agent)
    finally:
        runtime.stop()


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

    runtime.storage.update_settings_sections({"skills": {"directories": [str(skill_root)]}})
    runtime.reload_skills()
    definitions_after_reload = runtime.system_prompts.provider_tool_definitions(agent)

    assert [definition["name"] for definition in definitions_before_reload] == ["memory"]
    assert [definition["name"] for definition in definitions_after_reload] == [
        "memory",
        "skill",
    ]


def _write_test_skill(skill_root: Path, name: str, description: str) -> None:
    skill_dir = skill_root / name
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nUse this skill.\n",
        encoding="utf-8",
    )


def _write_project_skill(repo: Path, name: str, description: str) -> None:
    """Write a project-owned skill under ``<repo>/.opencode/skills/<name>/``."""
    _write_test_skill(repo / ".opencode" / "skills", name, description)


def _write_agent_skill(data_dir: Path, agent_id: str, name: str, description: str) -> None:
    """Write an agent-private skill under ``<data_dir>/agents/<id>/skills/<name>/``."""
    _write_test_skill(data_dir / "agents" / agent_id / "skills", name, description)


def test_skills_for_none_returns_global_registry(config: Config) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()

    # The identity path is byte-identical to the global registry — no scoping.
    assert runtime.skills_for(None) is runtime.skills


def test_skills_for_project_merges_project_and_bundled(config: Config, tmp_path: Path) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_project_skill(repo, "proj-only-skill", "A project-scoped playbook.")
    project = runtime.projects.create("p", "P", repo)

    registry = runtime.skills_for(project.project_id)

    names = {skill.name for skill in registry.list_all()}
    # The project's own skill plus the entire bundled pool are visible.
    assert "proj-only-skill" in names
    assert {skill.name for skill in runtime.skills.list_all()}.issubset(names)


def test_skills_for_project_skill_wins_name_collision(config: Config, tmp_path: Path) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    bundled_name = runtime.skills.list_all()[0].name
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_project_skill(repo, bundled_name, "Project override of a bundled skill.")
    project = runtime.projects.create("p", "P", repo)

    registry = runtime.skills_for(project.project_id)

    # The project skill shadows the bundled one of the same name (one slot, project wins).
    assert registry.get(bundled_name).description == "Project override of a bundled skill."


def test_project_skill_names_returns_project_owned_only(config: Config, tmp_path: Path) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_project_skill(repo, "alpha", "Alpha.")
    _write_project_skill(repo, "beta", "Beta.")
    project = runtime.projects.create("p", "P", repo)

    assert runtime.project_skill_names(project.project_id) == frozenset({"alpha", "beta"})
    assert runtime.project_skill_names(None) == frozenset()


def test_skills_for_project_is_cached_until_invalidated(config: Config, tmp_path: Path) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_project_skill(repo, "alpha", "Alpha.")
    project = runtime.projects.create("p", "P", repo)

    first = runtime.skills_for(project.project_id)
    assert runtime.skills_for(project.project_id) is first  # cached, not re-scanned
    runtime.invalidate_project_skills(project.project_id)
    assert runtime.skills_for(project.project_id) is not first  # rebuilt after invalidation


def test_invalidate_project_skills_reflects_cwd_change(config: Config, tmp_path: Path) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    repo_a = tmp_path / "a"
    repo_a.mkdir()
    _write_project_skill(repo_a, "skill-a", "From repo A.")
    repo_b = tmp_path / "b"
    repo_b.mkdir()
    _write_project_skill(repo_b, "skill-b", "From repo B.")
    project = runtime.projects.create("p", "P", repo_a)
    assert runtime.project_skill_names(project.project_id) == frozenset({"skill-a"})

    runtime.projects.update(project.project_id, cwd=str(repo_b))
    runtime.invalidate_project_skills(project.project_id)

    assert runtime.project_skill_names(project.project_id) == frozenset({"skill-b"})


def test_reload_skills_drops_project_skill_cache(config: Config, tmp_path: Path) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_project_skill(repo, "alpha", "Alpha.")
    project = runtime.projects.create("p", "P", repo)
    first = runtime.skills_for(project.project_id)

    runtime.reload_skills()

    # A global skill reload makes project registries stale, so they rebuild.
    assert runtime.skills_for(project.project_id) is not first


def test_agent_skills_dir_path(config: Config) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()

    assert (
        runtime.agent_skills_dir("main") == runtime.storage.data_dir / "agents" / "main" / "skills"
    )


def test_skills_for_agent_includes_own_private_skills(config: Config) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    _write_agent_skill(runtime.storage.data_dir, "main", "my-private", "An agent-only playbook.")

    registry = runtime.skills_for(None, "main")

    names = {skill.name for skill in registry.list_all()}
    assert "my-private" in names
    # The same skill is invisible to the agent-less global resolution.
    assert "my-private" not in {skill.name for skill in runtime.skills.list_all()}


def test_agent_own_skill_bypasses_owner_allowlist(config: Config) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    _write_agent_skill(runtime.storage.data_dir, "main", "my-private", "An agent-only playbook.")

    registry = runtime.skills_for(None, "main")

    # An empty allow-list normally exposes nothing, but the agent's own skill is
    # always allowed for its owner — and only it (no bundled skill leaks in).
    assert registry.is_allowed("my-private", [])
    assert [skill.name for skill in registry.filter_allowed([])] == ["my-private"]


def test_agent_own_skills_isolated_between_agents(config: Config) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    _write_agent_skill(runtime.storage.data_dir, "main", "main-only", "Main's private playbook.")

    # Another agent with no private home falls through to the global pool, which never
    # contains main's private skill.
    other_registry = runtime.skills_for(None, "other")
    assert "main-only" not in {skill.name for skill in other_registry.list_all()}


def test_skills_for_identity_without_own_skills_is_global(config: Config) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()

    # No private skills home → byte-identical to the global registry (same object).
    assert runtime.skills_for(None, "main") is runtime.skills


def test_skills_for_agent_layers_own_over_project(config: Config, tmp_path: Path) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_project_skill(repo, "proj-skill", "Project playbook.")
    _write_project_skill(repo, "shared", "Project version.")
    project = runtime.projects.create("p", "P", repo)
    _write_agent_skill(runtime.storage.data_dir, "main", "agent-skill", "Agent playbook.")
    _write_agent_skill(runtime.storage.data_dir, "main", "shared", "Agent version.")

    registry = runtime.skills_for(project.project_id, "main")

    names = {skill.name for skill in registry.list_all()}
    assert {"agent-skill", "proj-skill"}.issubset(names)
    # Agent skills are scanned first, so the agent wins a name collision with project.
    assert registry.get("shared").description == "Agent version."


def test_invalidate_agent_skills_drops_only_that_agent(config: Config) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    _write_agent_skill(runtime.storage.data_dir, "main", "main-skill", "Main.")
    _write_agent_skill(runtime.storage.data_dir, "two", "two-skill", "Two.")
    main_first = runtime.skills_for(None, "main")
    two_first = runtime.skills_for(None, "two")
    assert runtime.skills_for(None, "main") is main_first  # cached

    runtime.invalidate_agent_skills("main")

    assert runtime.skills_for(None, "main") is not main_first  # main rebuilt
    assert runtime.skills_for(None, "two") is two_first  # two untouched


def test_reload_skills_drops_agent_cache(config: Config) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    _write_agent_skill(runtime.storage.data_dir, "main", "main-skill", "Main.")
    first = runtime.skills_for(None, "main")

    runtime.reload_skills()

    assert runtime.skills_for(None, "main") is not first


def test_invalidate_project_skills_drops_matching_agent_cache(
    config: Config, tmp_path: Path
) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_project_skill(repo, "proj-skill", "Project.")
    project = runtime.projects.create("p", "P", repo)
    _write_agent_skill(runtime.storage.data_dir, "main", "agent-skill", "Agent.")
    first = runtime.skills_for(project.project_id, "main")

    # The agent registry embeds the project layer, so a project invalidation drops it.
    runtime.invalidate_project_skills(project.project_id)

    assert runtime.skills_for(project.project_id, "main") is not first


def test_project_own_skills_returns_scanned_project_metadata(
    config: Config, tmp_path: Path
) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_project_skill(repo, "deploy", "Ship it.")
    project = runtime.projects.create("p", "P", repo)

    skills = runtime.project_own_skills(project.project_id)

    # Only the project's own skills (no bundled), with their real SKILL.md paths.
    assert [skill.name for skill in skills] == ["deploy"]
    assert skills[0].path == (repo / ".opencode" / "skills" / "deploy" / "SKILL.md").resolve()


def test_skills_for_tags_origin_per_scope(config: Config, tmp_path: Path) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_project_skill(repo, "proj-skill", "Project.")
    project = runtime.projects.create("p", "P", repo)
    _write_agent_skill(runtime.storage.data_dir, "main", "mine", "Mine.")
    bundled_name = runtime.skills.list_all()[0].name

    registry = runtime.skills_for(project.project_id, "main")

    assert registry.get("mine").origin == "agent"
    assert registry.get("proj-skill").origin == "project:P"
    assert registry.get(bundled_name).origin == "bundled"


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

    def normalize_response(
        self, response: dict[str, object], *, model_id: str | None = None
    ) -> dict[str, object]:
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
        self.agent_resolver = _StubAgentResolver(self.agents)
        self.projects = _StubProjects()
        self.providers = _StubProviders()
        self.provider_credentials = _StubCredentials()
        self.chat_sessions = ChatSessionManager(tmp_path)
        self.chat_runs = ChatRunManager()
        self.chat_run_manager = self.chat_runs
        self.extensions = None
        self.system_prompts = _StubPrompts()
        self.tools = ToolRegistry()
        self.storage = SimpleNamespace(data_dir=tmp_path)
        self._process_manager = process_manager
        self._adapter = adapter

    def get_adapter(self, _provider_id: str, _connection_id: str) -> _BlockingAdapter:
        return self._adapter

    def skills_for(
        self, _project_id: str | None = None, _agent_id: str | None = None
    ) -> SkillRegistry:
        return SkillRegistry({})

    def project_skill_names(self, _project_id: str | None = None) -> frozenset[str]:
        return frozenset()

    @property
    def process_manager(self) -> _RecordingProcessManager:
        return self._process_manager


class _StubProjects:
    """Empty project store: this runtime stub registers no project, so the rooting
    lookup never matches and the visit list is always empty."""

    def get(self, project_id: str) -> object:
        raise KeyError(project_id)

    def list(self) -> list[object]:
        return []

    def find_by_cwd(self, _cwd: object) -> object | None:
        return None


class _StubAgents:
    def get(self, agent_id: str) -> object:
        return SimpleNamespace(
            id=agent_id,
            model="provider/model::default",
            temperature=0.0,
            thinking_effort="",
            allowed_tools=["*"],
            allowed_skills=["*"],
            workspace="",
        )


class _StubAgentResolver:
    """Identity-only resolver seam for the chat-cancellation runtime stub."""

    def __init__(self, agents: _StubAgents) -> None:
        self._agents = agents

    def resolve_agent(self, _project_id: str | None, agent_id: str) -> object:
        return self._agents.get(agent_id)


class _StubProviders:
    def get(self, provider_id: str) -> object:
        return SimpleNamespace(id=provider_id)


class _StubCredentials:
    def has_credentials(self, _provider_id: str, _connection_id: str | None = None) -> bool:
        return True


class _StubPrompts:
    def build_system_prompt(
        self,
        _agent: object,
        _scope: object = None,
        *,
        agent_body: str = "",
        project_context: object = None,
        skill_registry: object = None,
        skill_catalog: object = None,
    ) -> str:
        return "System prompt"

    def render_skill_catalog(self, _agent: object, skill_registry: object = None) -> object:
        from core.prompts import PinnedSkillCatalog

        return PinnedSkillCatalog(catalog_text="", has_loadable_skills=False)

    def provider_tool_definitions(
        self, _agent: object, *, skill_registry: object = None, skill_catalog: object = None
    ) -> list[dict[str, object]]:
        return []
