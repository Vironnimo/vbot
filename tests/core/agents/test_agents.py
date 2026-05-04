"""Tests for agent persistence and workspace lifecycle."""

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from core.agents import (
    Agent,
    AgentAlreadyExistsError,
    AgentError,
    AgentNotFoundError,
    AgentStore,
    InvalidAgentIdError,
)

TEMPLATE_FILES = ("SOUL.md", "IDENTITY.md", "AGENTS.md", "USER.md")


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
    assert data["temperature"] == 0.1
    assert data["thinking_effort"] == ""
    assert data["allowed_tools"] == ["*"]
    assert data["allowed_skills"] == ["*"]
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
        allowed_tools=[],
        allowed_skills=["memory"],
    )

    assert agent.workspace == str(custom_workspace.resolve())
    assert agent.allowed_tools == []
    assert agent.allowed_skills == ["memory"]
    assert (custom_workspace / "SOUL.md").exists()


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
    )

    assert updated.id == "coder"
    assert updated.created_at == original.created_at
    assert updated.updated_at >= original.updated_at
    assert updated.name == "Updated Coder"
    assert updated.model == "openai/gpt-5.2"
    assert updated.allowed_tools == ["read_file"]
    assert updated.current_session_id == current_session_id
    assert store.get("coder") == updated


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
    assert (custom_workspace / "IDENTITY.md").exists()
