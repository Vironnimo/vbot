"""Tests for the Runtime bootstrap class.

Verifies that ``Runtime`` initialises, starts, and stops without errors,
and that the logger is properly created after ``start()``.
"""

import logging
from pathlib import Path

import pytest

from core.agents.agents import AgentStore, SystemPromptManager
from core.chat.chat import ChatSessionManager
from core.runtime.runtime import Runtime
from core.skills.skills import SkillRegistry
from core.storage.storage import StorageManager
from core.tools.tools import ToolRegistry
from core.utils.config import Config


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(data_dir=tmp_path / "data")


def test_runtime_start_no_error(tmp_path: Path):
    """Instantiating Runtime and calling start() raises no exception."""
    # Arrange
    config = Config(data_dir=tmp_path / "data")
    runtime = Runtime(config)

    # Act
    runtime.start()

    # Assert
    assert runtime.logger is not None


def test_runtime_logger_exists_after_start(tmp_path: Path):
    """After start(), runtime.logger is a valid logger object."""
    # Arrange
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


def test_runtime_stop_runs_cleanly(tmp_path: Path):
    """After start(), calling stop() completes without exception."""
    # Arrange
    config = Config(data_dir=tmp_path / "data")
    runtime = Runtime(config)
    runtime.start()

    # Act
    runtime.stop()

    # Assert — reaching here without exception is success


def test_runtime_stop_without_start_does_not_crash(tmp_path: Path):
    """Calling stop() before start() is a no-op and does not crash."""
    # Arrange
    config = Config(data_dir=tmp_path / "data")
    runtime = Runtime(config)

    # Act
    runtime.stop()

    # Assert — reaching here without exception proves it is a safe no-op


def test_phase_two_services_available_after_start(config: Config):
    """Runtime.start() wires all Phase 2 domain services."""
    runtime = Runtime(config)

    runtime.start()

    assert isinstance(runtime.storage, StorageManager)
    assert isinstance(runtime.agents, AgentStore)
    assert isinstance(runtime.tools, ToolRegistry)
    assert isinstance(runtime.skills, SkillRegistry)
    assert isinstance(runtime.chat_sessions, ChatSessionManager)
    assert isinstance(runtime.system_prompts, SystemPromptManager)


def test_start_registers_read_builtin_tool(config: Config):
    """Runtime.start() registers built-in tools for agent use."""
    runtime = Runtime(config)

    runtime.start()

    read_tool = runtime.tools.get("read")
    assert read_tool.name == "read"


def test_read_provider_definition_exposes_model_visible_metadata_only(config: Config):
    """Runtime tool definitions expose schema without handler or context."""
    runtime = Runtime(config)

    runtime.start()

    definitions = runtime.tools.provider_definitions(["read"])

    assert definitions == [
        {
            "name": "read",
            "description": "Read a text file from disk. Relative paths resolve from the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer"},
                    "limit": {"type": "integer"},
                    "description": {
                        "type": "string",
                        "description": "Brief description of what this tool call is doing",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        }
    ]
    assert set(definitions[0]) == {"name", "description", "parameters"}


def test_phase_two_services_inaccessible_before_start(config: Config):
    """Runtime service properties raise a startup error before start()."""
    runtime = Runtime(config)

    for attribute_name in (
        "storage",
        "agents",
        "tools",
        "skills",
        "chat_sessions",
        "system_prompts",
    ):
        with pytest.raises(RuntimeError, match="not started"):
            getattr(runtime, attribute_name)


def test_start_ensures_data_directories_and_prompt_fragments(config: Config):
    """Runtime.start() prepares the Phase 2 data directory structure."""
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
    runtime = Runtime(config)
    runtime.start()

    runtime.stop()

    with pytest.raises(RuntimeError, match="not started"):
        _ = runtime.storage
