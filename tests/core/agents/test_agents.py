"""Tests for agent persistence and workspace lifecycle."""

import json
import shutil
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from core.agents import (
    Agent,
    AgentAlreadyExistsError,
    AgentError,
    AgentNotFoundError,
    AgentStore,
    InvalidAgentIdError,
)
from core.agents import agents as agents_module
from core.chat import ChatMessage
from core.sessions import ChatSessionManager

TEMPLATE_FILES = ("SOUL.md", "USER.md", "MEMORY.md")
EARLY_TIMESTAMP = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
LATE_TIMESTAMP = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def template_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "templates"
    directory.mkdir()
    for filename in TEMPLATE_FILES:
        (directory / filename).write_text(f"# {filename}\n", encoding="utf-8")
    return directory


@pytest.fixture
def store(tmp_path: Path, template_dir: Path) -> AgentStore:
    return AgentStore(tmp_path / "data", template_dir=template_dir)


def test_agent_dataclass_is_frozen() -> None:
    agent = Agent(
        id="coder",
        name="Coder Agent",
        model="openai/gpt-5.2",
        fallback_model="",
        workspace="C:/workspace",
        temperature=0.1,
        thinking_effort="",
        allowed_tools=["*"],
        allowed_skills=["*"],
        memory_prompt_mode="agent_user",
        custom_system_prompt_enabled=False,
        current_session_id="session-one",
        created_at="2026-05-03T12:00:00Z",
        updated_at="2026-05-03T12:00:00Z",
    )

    with pytest.raises(FrozenInstanceError):
        agent.name = "Changed"  # type: ignore[misc]


def test_create_writes_agent_json_sessions_and_workspace(store: AgentStore) -> None:
    agent = store.create("coder", "Coder Agent")

    agent_path = store.data_dir / "agents" / "coder" / "agent.json"
    data = json.loads(agent_path.read_text(encoding="utf-8"))

    assert data["id"] == "coder"
    assert data["name"] == "Coder Agent"
    assert data["model"] == ""
    assert data["fallback_model"] == ""
    assert data["workspace"] == str((store.data_dir / "workspace-coder").resolve())
    assert data["temperature"] is None
    assert data["thinking_effort"] is None
    assert data["memory_prompt_mode"] == "agent_user"
    assert data["allowed_tools"] == ["*"]
    assert data["allowed_skills"] == ["*"]
    assert data["custom_system_prompt_enabled"] is False
    assert isinstance(data["current_session_id"], str)
    assert data["current_session_id"]
    assert data["created_at"].endswith("Z")
    assert data["updated_at"] == data["created_at"]
    assert (store.data_dir / "agents" / "coder" / "sessions").is_dir()
    assert (
        store.data_dir / "agents" / "coder" / "sessions" / f"{data['current_session_id']}.jsonl"
    ).is_file()
    assert agent.current_session_id == data["current_session_id"]
    assert agent == store.get("coder")

    workspace_path = Path(agent.workspace)
    for filename in TEMPLATE_FILES:
        assert (workspace_path / filename).read_text(encoding="utf-8") == f"# {filename}\n"


def test_create_with_custom_values_persists_schema(store: AgentStore, tmp_path: Path) -> None:
    custom_workspace = tmp_path / "custom-workspace"
    agent = store.create(
        "researcher_1",
        "Research Agent",
        model="openrouter/deepseek/deepseek-v4-pro",
        fallback_model="openai/gpt-5.2",
        workspace=custom_workspace,
        temperature=0.7,
        thinking_effort="high",
        memory_prompt_mode="agent",
        allowed_tools=[],
        allowed_skills=["memory"],
        custom_system_prompt_enabled=True,
    )

    assert agent.workspace == str(custom_workspace.resolve())
    assert agent.allowed_tools == []
    assert agent.allowed_skills == ["memory"]
    assert agent.memory_prompt_mode == "agent"
    assert agent.custom_system_prompt_enabled is True
    assert (custom_workspace / "SOUL.md").exists()


def test_create_removes_runtime_derived_memory_tool_from_allowed_tools(
    store: AgentStore,
) -> None:
    agent = store.create(
        "coder",
        "Coder Agent",
        allowed_tools=["read_file", "memory"],
        memory_prompt_mode="agent_user",
    )

    agent_path = store.data_dir / "agents" / "coder" / "agent.json"
    data = json.loads(agent_path.read_text(encoding="utf-8"))

    assert agent.allowed_tools == ["read_file"]
    assert data["allowed_tools"] == ["read_file"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("name", "", "name must be a non-empty string"),
        ("model", 12, "model must be a string"),
        ("fallback_model", 12, "fallback_model must be a string"),
        ("temperature", "0.4", "temperature must be a number"),
        ("temperature", -0.1, "temperature must be between"),
        ("temperature", 2.1, "temperature must be between"),
        ("thinking_effort", "extreme", "thinking_effort must be one of"),
        ("memory_prompt_mode", "sometimes", "memory_prompt_mode must be one of"),
        ("memory_prompt_mode", True, "memory_prompt_mode must be a string"),
        ("workspace", "", "workspace must be a non-empty path string"),
        ("allowed_tools", "read_file", "allowed_tools must be a list of strings"),
        ("allowed_tools", ["read_file", 1], "allowed_tools must be a list of strings"),
        ("allowed_skills", "debugging", "allowed_skills must be a list of strings"),
        ("allowed_skills", ["debugging", None], "allowed_skills must be a list of strings"),
        (
            "custom_system_prompt_enabled",
            "yes",
            "custom_system_prompt_enabled must be a boolean",
        ),
    ],
)
def test_create_rejects_invalid_mutable_fields(
    store: AgentStore,
    field: str,
    value: object,
    message: str,
) -> None:
    name = value if field == "name" else "Coder Agent"
    fields: dict[str, Any] = {} if field == "name" else {field: value}

    with pytest.raises(AgentError, match=message):
        store.create("coder", name, **fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "thinking_effort", ["", "none", "minimal", "low", "medium", "high", "xhigh", "max"]
)
def test_create_accepts_supported_thinking_efforts(
    store: AgentStore,
    thinking_effort: str,
) -> None:
    agent = store.create(
        f"coder_{thinking_effort or 'default'}", "Coder", thinking_effort=thinking_effort
    )

    assert agent.thinking_effort == thinking_effort


def test_create_accepts_none_temperature_and_thinking_effort(store: AgentStore) -> None:
    agent = store.create(
        "coder_none",
        "Coder",
        temperature=None,
        thinking_effort=None,
    )

    assert agent.temperature is None
    assert agent.thinking_effort is None


def test_create_rejects_duplicate_agent(store: AgentStore) -> None:
    store.create("coder", "Coder Agent")

    with pytest.raises(AgentAlreadyExistsError, match="coder"):
        store.create("coder", "Coder Agent")


@pytest.mark.parametrize("agent_id", ["", ".hidden", "../escape", "with space", "slash/name"])
def test_create_rejects_unsafe_agent_id(store: AgentStore, agent_id: str) -> None:
    with pytest.raises(InvalidAgentIdError):
        store.create(agent_id, "Unsafe Agent")


def test_get_missing_agent_raises_not_found(store: AgentStore) -> None:
    with pytest.raises(AgentNotFoundError, match="missing"):
        store.get("missing")


def test_get_rejects_invalid_agent_json_schema(store: AgentStore) -> None:
    store.create("broken", "Broken Agent")
    agent_path = store.data_dir / "agents" / "broken" / "agent.json"
    data = json.loads(agent_path.read_text(encoding="utf-8"))
    data["allowed_tools"] = "read_file"
    agent_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(AgentError, match=r"\$\.allowed_tools: must be a list of strings"):
        store.get("broken")


def test_list_returns_agents_sorted_by_id(store: AgentStore) -> None:
    store.create("beta", "Beta Agent")
    store.create("alpha", "Alpha Agent")

    agents = store.list()

    assert [agent.id for agent in agents] == ["alpha", "beta"]


def test_update_changes_mutable_fields_and_preserves_id(store: AgentStore) -> None:
    original = store.create("coder", "Coder Agent")
    current_session_id = original.current_session_id
    updated = store.update(
        "coder",
        name="Updated Coder",
        model="openai/gpt-5.2",
        allowed_tools=["read_file"],
        memory_prompt_mode="off",
        custom_system_prompt_enabled=True,
    )

    assert updated.id == "coder"
    assert updated.created_at == original.created_at
    assert updated.updated_at >= original.updated_at
    assert updated.name == "Updated Coder"
    assert updated.model == "openai/gpt-5.2"
    assert updated.allowed_tools == ["read_file"]
    assert updated.memory_prompt_mode == "off"
    assert updated.custom_system_prompt_enabled is True
    assert updated.current_session_id == current_session_id
    assert store.get("coder") == updated


def test_update_removes_runtime_derived_memory_tool_from_allowed_tools(
    store: AgentStore,
) -> None:
    store.create("coder", "Coder Agent")

    updated = store.update("coder", allowed_tools=["read_file", "memory"])

    assert updated.allowed_tools == ["read_file"]


def test_update_changes_workspace_and_seeds_templates(
    store: AgentStore,
    tmp_path: Path,
) -> None:
    store.create("coder", "Coder Agent")
    workspace = tmp_path / "updated-workspace"

    updated = store.update("coder", workspace=workspace)

    assert updated.workspace == str(workspace.resolve())
    assert workspace.is_dir()
    assert (workspace / "SOUL.md").exists()

    agent_path = store.data_dir / "agents" / "coder" / "agent.json"
    data = json.loads(agent_path.read_text(encoding="utf-8"))
    assert data["workspace"] == str(workspace.resolve())


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("name", 123, "name must be a string"),
        ("name", "", "name must be a non-empty string"),
        ("model", 123, "model must be a string"),
        ("fallback_model", 123, "fallback_model must be a string"),
        ("temperature", True, "temperature must be a number"),
        ("temperature", 3.0, "temperature must be between"),
        ("thinking_effort", "turbo", "thinking_effort must be one of"),
        ("memory_prompt_mode", "sometimes", "memory_prompt_mode must be one of"),
        ("memory_prompt_mode", 1, "memory_prompt_mode must be a string"),
        ("workspace", "", "workspace must be a non-empty path string"),
        ("allowed_tools", "read_file", "allowed_tools must be a list of strings"),
        ("allowed_tools", ["read_file", False], "allowed_tools must be a list of strings"),
        ("allowed_skills", "debugging", "allowed_skills must be a list of strings"),
        ("allowed_skills", ["debugging", {}], "allowed_skills must be a list of strings"),
        (
            "custom_system_prompt_enabled",
            1,
            "custom_system_prompt_enabled must be a boolean",
        ),
    ],
)
def test_update_rejects_invalid_mutable_fields(
    store: AgentStore,
    field: str,
    value: object,
    message: str,
) -> None:
    store.create("coder", "Coder Agent")

    with pytest.raises(AgentError, match=message):
        store.update("coder", **{field: value})


def test_update_rejects_id_change(store: AgentStore) -> None:
    store.create("coder", "Coder Agent")

    with pytest.raises(AgentError, match="immutable"):
        store.update("coder", id="other")


def test_update_rejects_unknown_fields(store: AgentStore) -> None:
    store.create("coder", "Coder Agent")

    with pytest.raises(AgentError, match="Unknown agent fields"):
        store.update("coder", unknown=True)


def test_update_can_set_current_session_id_to_existing_session(store: AgentStore) -> None:
    original = store.create("coder", "Coder Agent")
    new_session = store.data_dir / "agents" / "coder" / "sessions" / "session-two.jsonl"
    new_session.touch()

    updated = store.update("coder", current_session_id="session-two")

    assert updated.current_session_id == "session-two"
    assert updated.current_session_id != original.current_session_id


def test_update_rejects_missing_current_session_id(store: AgentStore) -> None:
    store.create("coder", "Coder Agent")

    with pytest.raises(AgentError, match="current session does not exist"):
        store.update("coder", current_session_id="missing")


def test_reset_current_after_session_removed_lands_on_newest_remaining(store: AgentStore) -> None:
    agent = store.create("alpha", "Alpha")
    manager = ChatSessionManager(store.data_dir)
    manager.get("alpha", agent.current_session_id).append(
        ChatMessage.user("old", timestamp=EARLY_TIMESTAMP)
    )
    manager.create("alpha", session_id="newer").append(
        ChatMessage.user("recent", timestamp=LATE_TIMESTAMP)
    )
    manager.create("alpha", session_id="moved")
    store.update("alpha", current_session_id="moved")
    # Simulate the move: the current session's files leave the source home.
    manager.delete("alpha", "moved")

    result = store.reset_current_after_session_removed("alpha", "moved")

    assert result.current_session_id == "newer"


def test_reset_current_after_session_removed_creates_fresh_when_none_remain(
    store: AgentStore,
) -> None:
    agent = store.create("solo", "Solo")
    manager = ChatSessionManager(store.data_dir)
    moved_id = agent.current_session_id
    manager.delete("solo", moved_id)  # the only session is moved away

    result = store.reset_current_after_session_removed("solo", moved_id)

    assert result.current_session_id != moved_id
    assert manager.exists("solo", result.current_session_id) is True
    assert [session.id for session in manager.list("solo")] == [result.current_session_id]


def test_reset_current_after_session_removed_leaves_pointer_when_not_current(
    store: AgentStore,
) -> None:
    agent = store.create("beta", "Beta")
    manager = ChatSessionManager(store.data_dir)
    current_id = agent.current_session_id
    manager.create("beta", session_id="other")
    manager.delete("beta", "other")  # a non-current session was moved away

    result = store.reset_current_after_session_removed("beta", "other")

    assert result.current_session_id == current_id


def test_temperature_and_thinking_effort_none_round_trip_as_json_null(
    store: AgentStore,
) -> None:
    store.create(
        "coder_nulls",
        "Coder Agent",
        temperature=None,
        thinking_effort=None,
    )
    agent_path = store.data_dir / "agents" / "coder_nulls" / "agent.json"

    data = json.loads(agent_path.read_text(encoding="utf-8"))
    restored = agents_module._agent_from_dict(data)

    assert data["temperature"] is None
    assert data["thinking_effort"] is None
    assert restored.temperature is None
    assert restored.thinking_effort is None


def test_apply_defaults_fills_empty_model(store: AgentStore) -> None:
    agent = store.create("coder_model", "Coder Agent", model="")

    resolved = store._apply_defaults(agent, {"model": "openai/gpt-5.2"})

    assert resolved.model == "openai/gpt-5.2"


def test_apply_defaults_fills_none_temperature(store: AgentStore) -> None:
    agent = store.create("coder_temperature", "Coder Agent", temperature=None)

    resolved = store._apply_defaults(agent, {"temperature": 0.7})

    assert resolved.temperature == 0.7


def test_apply_defaults_leaves_explicit_values_unchanged(store: AgentStore) -> None:
    agent = store.create(
        "coder_explicit",
        "Coder Agent",
        model="openai/gpt-5.2",
        fallback_model="openrouter/anthropic/claude-sonnet-4",
        temperature=0.3,
        thinking_effort="high",
    )

    resolved = store._apply_defaults(
        agent,
        {
            "model": "openrouter/openai/gpt-4.1",
            "fallback_model": "openai/gpt-5.2-mini",
            "temperature": 0.9,
            "thinking_effort": "low",
        },
    )

    assert resolved.model == "openai/gpt-5.2"
    assert resolved.fallback_model == "openrouter/anthropic/claude-sonnet-4"
    assert resolved.temperature == 0.3
    assert resolved.thinking_effort == "high"


def test_get_reflects_updated_defaults_without_reloading_agent_file(
    tmp_path: Path,
    template_dir: Path,
) -> None:
    defaults: dict[str, Any] = {
        "model": "openai/gpt-5.2",
        "temperature": 0.2,
        "thinking_effort": "low",
    }
    store = AgentStore(
        tmp_path / "data",
        template_dir=template_dir,
        defaults_provider=lambda: defaults,
    )
    store.create(
        "coder_dynamic_defaults",
        "Coder Agent",
        model="",
        temperature=None,
        thinking_effort=None,
    )

    first = store.get("coder_dynamic_defaults")
    assert first.model == "openai/gpt-5.2"
    assert first.temperature == 0.2
    assert first.thinking_effort == "low"

    defaults["model"] = "openrouter/anthropic/claude-sonnet-4"
    defaults["temperature"] = 0.6
    defaults["thinking_effort"] = "high"

    second = store.get("coder_dynamic_defaults")
    assert second.model == "openrouter/anthropic/claude-sonnet-4"
    assert second.temperature == 0.6
    assert second.thinking_effort == "high"

    persisted = json.loads(
        (store.data_dir / "agents" / "coder_dynamic_defaults" / "agent.json").read_text(
            encoding="utf-8"
        )
    )
    assert persisted["model"] == ""
    assert persisted["temperature"] is None
    assert persisted["thinking_effort"] is None


def test_create_returns_resolved_defaults_but_persists_raw_values(
    tmp_path: Path,
    template_dir: Path,
) -> None:
    store = AgentStore(
        tmp_path / "data",
        template_dir=template_dir,
        defaults_provider=lambda: {
            "model": "openai/gpt-5.2",
            "fallback_model": "openai/gpt-5.2-mini",
            "temperature": 0.6,
            "thinking_effort": "high",
        },
    )

    created = store.create(
        "coder_create_defaults",
        "Coder Agent",
        model="",
        fallback_model="",
        temperature=None,
        thinking_effort=None,
    )

    assert created.model == "openai/gpt-5.2"
    assert created.fallback_model == "openai/gpt-5.2-mini"
    assert created.temperature == 0.6
    assert created.thinking_effort == "high"

    persisted = json.loads(
        (store.data_dir / "agents" / "coder_create_defaults" / "agent.json").read_text(
            encoding="utf-8"
        )
    )
    assert persisted["model"] == ""
    assert persisted["fallback_model"] == ""
    assert persisted["temperature"] is None
    assert persisted["thinking_effort"] is None


def test_update_with_explicit_temperature_does_not_write_default_to_disk(
    tmp_path: Path,
    template_dir: Path,
) -> None:
    store = AgentStore(
        tmp_path / "data",
        template_dir=template_dir,
        defaults_provider=lambda: {"temperature": 0.9},
    )
    store.create("coder_update_default", "Coder Agent", temperature=None)

    updated = store.update("coder_update_default", temperature=0.4)

    assert updated.temperature == 0.4
    persisted = json.loads(
        (store.data_dir / "agents" / "coder_update_default" / "agent.json").read_text(
            encoding="utf-8"
        )
    )
    assert persisted["temperature"] == 0.4
    assert store.get("coder_update_default").temperature == 0.4


def test_legacy_agent_without_current_session_id_is_normalized(store: AgentStore) -> None:
    agent = store.create("legacy", "Legacy Agent")
    agent_path = store.data_dir / "agents" / "legacy" / "agent.json"
    data = json.loads(agent_path.read_text(encoding="utf-8"))
    data.pop("current_session_id")
    for session_file in (store.data_dir / "agents" / "legacy" / "sessions").glob("*.jsonl"):
        session_file.unlink()
    agent_path.write_text(json.dumps(data), encoding="utf-8")

    loaded = store.get("legacy")

    assert loaded.current_session_id
    assert loaded.current_session_id != agent.current_session_id
    assert (
        store.data_dir / "agents" / "legacy" / "sessions" / f"{loaded.current_session_id}.jsonl"
    ).is_file()
    normalized_data = json.loads(agent_path.read_text(encoding="utf-8"))
    assert normalized_data["current_session_id"] == loaded.current_session_id


def test_agent_without_workspace_is_normalized_to_default_workspace(store: AgentStore) -> None:
    store.create("missing_workspace", "Missing Workspace Agent")
    agent_path = store.data_dir / "agents" / "missing_workspace" / "agent.json"
    workspace_path = store.data_dir / "workspace-missing_workspace"
    shutil.rmtree(workspace_path)
    data = json.loads(agent_path.read_text(encoding="utf-8"))
    data.pop("workspace")
    agent_path.write_text(json.dumps(data), encoding="utf-8")

    loaded = store.get("missing_workspace")

    assert loaded.workspace == str(workspace_path.resolve())
    assert workspace_path.is_dir()
    assert (workspace_path / "SOUL.md").exists()
    normalized_data = json.loads(agent_path.read_text(encoding="utf-8"))
    assert normalized_data["workspace"] == str(workspace_path.resolve())


def test_agent_without_custom_prompt_toggle_uses_default_false(store: AgentStore) -> None:
    store.create("missing_prompt_toggle", "Missing Prompt Toggle Agent")
    agent_path = store.data_dir / "agents" / "missing_prompt_toggle" / "agent.json"
    data = json.loads(agent_path.read_text(encoding="utf-8"))
    data.pop("custom_system_prompt_enabled")
    agent_path.write_text(json.dumps(data), encoding="utf-8")

    loaded = store.get("missing_prompt_toggle")

    assert loaded.custom_system_prompt_enabled is False
    persisted_data = json.loads(agent_path.read_text(encoding="utf-8"))
    assert "custom_system_prompt_enabled" not in persisted_data


def test_agent_with_missing_workspace_directory_recreates_workspace(store: AgentStore) -> None:
    agent = store.create("recreate_workspace", "Recreate Workspace Agent")
    workspace_path = Path(agent.workspace)
    shutil.rmtree(workspace_path)

    loaded = store.get("recreate_workspace")

    assert loaded.workspace == agent.workspace
    assert workspace_path.is_dir()
    assert (workspace_path / "SOUL.md").exists()


def test_delete_archives_agent_data_and_workspace(store: AgentStore) -> None:
    agent = store.create("coder", "Coder Agent")
    session_file = store.data_dir / "agents" / "coder" / "sessions" / "session.jsonl"
    session_file.write_text('{"role":"user"}\n', encoding="utf-8")

    archive_dir = store.delete("coder")

    assert archive_dir == store.data_dir / "archive" / "coder"
    assert not (store.data_dir / "agents" / "coder").exists()
    assert not Path(agent.workspace).exists()
    assert (archive_dir / "agent" / "agent.json").exists()
    assert (archive_dir / "agent" / "sessions" / "session.jsonl").exists()
    assert (archive_dir / "workspace" / "SOUL.md").exists()


def test_delete_missing_agent_raises_not_found(store: AgentStore) -> None:
    with pytest.raises(AgentNotFoundError, match="missing"):
        store.delete("missing")


def test_workspace_seeding_does_not_overwrite_existing_custom_workspace_file(
    store: AgentStore,
    tmp_path: Path,
) -> None:
    custom_workspace = tmp_path / "custom-workspace"
    custom_workspace.mkdir()
    (custom_workspace / "SOUL.md").write_text("custom soul", encoding="utf-8")

    store.create("coder", "Coder Agent", workspace=custom_workspace)

    assert (custom_workspace / "SOUL.md").read_text(encoding="utf-8") == "custom soul"
    assert (custom_workspace / "USER.md").exists()
