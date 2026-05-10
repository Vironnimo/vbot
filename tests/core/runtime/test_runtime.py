"""Tests for the Runtime bootstrap class."""

import logging
import re
from pathlib import Path

import pytest

from core.agents.agents import AgentStore, SystemPromptManager
from core.chat.chat import ChatSessionManager
from core.providers.credentials import ProviderCredentialResolver
from core.runtime.runtime import Runtime
from core.skills.skills import SkillRegistry
from core.storage.storage import StorageManager
from core.tools.tools import ToolRegistry
from core.utils.config import Config

CANONICAL_BUILTIN_TOOLS = ["edit", "glob", "grep", "read", "write"]
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
