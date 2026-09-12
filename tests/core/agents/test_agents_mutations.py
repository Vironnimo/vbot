"""Tests for agents mutations."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.agents import (
    AgentError,
    AgentNotFoundError,
    AgentStore,
)
from core.agents import agents as agents_module
from core.chat import ChatMessage
from core.sessions import ChatSessionManager, SessionAddress
from core.tools.availability import ToolAccess
from tests.core.agents.agents_test_support import (
    store as store,
)
from tests.core.agents.agents_test_support import (
    template_dir as template_dir,
)

EARLY_TIMESTAMP = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


LATE_TIMESTAMP = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def test_update_changes_mutable_fields_and_preserves_id(store: AgentStore) -> None:
    original = store.create("coder", "Coder Agent")
    current_session_id = original.current_session_id
    updated = store.update(
        "coder",
        name="Updated Coder",
        model="openai/gpt-5.2",
        tool_access={"mode": "selected", "allowed": ["read_file"]},
        tools={"subagent": {"allowed_agents": []}},
        memory_prompt_mode="off",
        custom_system_prompt_enabled=True,
    )

    assert updated.id == "coder"
    assert updated.created_at == original.created_at
    assert updated.updated_at >= original.updated_at
    assert updated.name == "Updated Coder"
    assert updated.model == "openai/gpt-5.2"
    assert updated.tool_access == ToolAccess(mode="selected", allowed=("read_file",))
    assert updated.tools == {"subagent": {"allowed_agents": []}}
    assert updated.memory_prompt_mode == "off"
    assert updated.custom_system_prompt_enabled is True
    assert updated.current_session_id == current_session_id
    assert store.get("coder") == updated


@pytest.mark.parametrize("name", [None, "", "   "])
def test_update_empty_optional_name_restores_id_default(
    store: AgentStore,
    name: str | None,
) -> None:
    store.create("coder", "Coder Agent")

    updated = store.update("coder", name=name)

    assert updated.name == "coder"


def test_update_rejects_allowed_and_denied_overlap(
    store: AgentStore,
) -> None:
    store.create("coder", "Coder Agent")

    with pytest.raises(AgentError, match="overlap"):
        store.update(
            "coder",
            tool_access={
                "mode": "selected",
                "allowed": ["read_file"],
                "denied": ["read_file"],
            },
        )


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


@pytest.mark.parametrize("workspace", [None, ""])
def test_update_empty_optional_workspace_restores_default(
    store: AgentStore,
    tmp_path: Path,
    workspace: str | None,
) -> None:
    store.create("coder", "Coder Agent", workspace=tmp_path / "custom-workspace")

    updated = store.update("coder", workspace=workspace)

    assert updated.workspace == store.default_workspace("coder")


def test_explicit_root_project_round_trips_independently_of_workspace(
    store: AgentStore,
) -> None:
    created = store.create("coder", "Coder Agent")

    rooted = store.update("coder", root_project_id="vbot")
    assert rooted.root_project_id == "vbot"
    assert rooted.workspace == created.workspace

    unrooted = store.update("coder", root_project_id=None)
    assert unrooted.root_project_id is None
    assert unrooted.workspace == created.workspace


def test_workspace_copy_preserves_sources_and_backs_up_destinations(
    store: AgentStore,
    tmp_path: Path,
) -> None:
    agent = store.create("coder", "Coder Agent")
    source = Path(agent.workspace)
    (source / "SOUL.md").write_text("source soul", encoding="utf-8")
    (source / "USER.md").write_text("source user", encoding="utf-8")
    (source / "MEMORY.md").write_text("source memory", encoding="utf-8")
    destination = tmp_path / "new-workspace"
    destination.mkdir()
    (destination / "SOUL.md").write_text("old soul", encoding="utf-8")

    result = store.update_with_metadata(
        "coder",
        workspace=destination,
        copy_workspace_identity_files=True,
    )

    assert result.copied_files == ("SOUL.md", "USER.md", "MEMORY.md")
    assert result.backed_up_files == ("SOUL.md",)
    assert (destination / "SOUL.md").read_text(encoding="utf-8") == "source soul"
    assert (destination / "USER.md").read_text(encoding="utf-8") == "source user"
    assert (destination / "MEMORY.md").read_text(encoding="utf-8") == "source memory"
    assert (source / "SOUL.md").read_text(encoding="utf-8") == "source soul"
    assert result.backup_dir is not None
    assert Path(result.backup_dir, "SOUL.md").read_text(encoding="utf-8") == "old soul"


def test_workspace_copy_rolls_back_destination_when_agent_write_fails(
    store: AgentStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = store.create("coder", "Coder Agent")
    source = Path(agent.workspace)
    (source / "SOUL.md").write_text("source soul", encoding="utf-8")
    destination = tmp_path / "new-workspace"
    destination.mkdir()
    (destination / "SOUL.md").write_text("destination soul", encoding="utf-8")

    monkeypatch.setattr(
        store, "_write_agent", lambda _agent: (_ for _ in ()).throw(OSError("disk"))
    )

    with pytest.raises(OSError, match="disk"):
        store.update_with_metadata(
            "coder",
            workspace=destination,
            copy_workspace_identity_files=True,
        )

    assert (destination / "SOUL.md").read_text(encoding="utf-8") == "destination soul"
    agent_json = json.loads(
        (store.data_dir / "agents" / "coder" / "agent.json").read_text(encoding="utf-8")
    )
    assert agent_json["workspace"] == "agents/coder/workspace"


@pytest.mark.parametrize(
    ("field", "value", "_message"),
    [
        ("name", 123, "name must be a string or null"),
        ("model", 123, "model must be a string"),
        ("fallback_models", 123, "fallback_models must be a list of strings"),
        ("temperature", True, "temperature must be a number"),
        ("temperature", 3.0, "temperature must be between"),
        ("thinking_effort", "turbo", "thinking_effort must be one of"),
        ("memory_prompt_mode", "sometimes", "memory_prompt_mode must be one of"),
        ("memory_prompt_mode", 1, "memory_prompt_mode must be a string"),
        ("tool_access", "read_file", "tool_access must be an object"),
        (
            "tool_access",
            {"mode": "selected", "allowed": ["read_file", False]},
            "tool_access.allowed must be a list of strings",
        ),
        ("allowed_skills", "debugging", "allowed_skills must be a list of strings"),
        ("allowed_skills", ["debugging", {}], "allowed_skills must be a list of strings"),
        ("tools", [], "tools must be an object"),
        (
            "tools",
            {"subagent": {"allowed_agents": ["worker", False]}},
            "tools.subagent.allowed_agents must be a list of strings",
        ),
        (
            "tools",
            {"bash": {"allowed_env": ["OPENAI_API_KEY", "bad-key"]}},
            "invalid environment key name",
        ),
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
    _message: str,
) -> None:
    store.create("coder", "Coder Agent")

    with pytest.raises(AgentError):
        store.update("coder", **{field: value})


def test_update_rejects_id_change(store: AgentStore) -> None:
    store.create("coder", "Coder Agent")

    with pytest.raises(AgentError):
        store.update("coder", id="other")


def test_update_rejects_unknown_fields(store: AgentStore) -> None:
    store.create("coder", "Coder Agent")

    with pytest.raises(AgentError):
        store.update("coder", unknown=True)


def test_update_can_set_current_session_id_to_existing_session(store: AgentStore) -> None:
    original = store.create("coder", "Coder Agent")
    store._session_manager().create("coder", session_id="session-two")

    updated = store.update("coder", current_session_id="session-two")

    assert updated.current_session_id == "session-two"
    assert updated.current_session_id != original.current_session_id


def test_update_rejects_missing_current_session_id(store: AgentStore) -> None:
    store.create("coder", "Coder Agent")

    with pytest.raises(AgentError):
        store.update("coder", current_session_id="missing")


def test_reset_current_after_session_removed_lands_on_newest_remaining(store: AgentStore) -> None:
    agent = store.create("alpha", "Alpha")
    manager = ChatSessionManager(store.data_dir)
    manager.get(
        SessionAddress(project_id=None, agent_id="alpha", session_id=agent.current_session_id)
    ).append(ChatMessage.user("old", timestamp=EARLY_TIMESTAMP))
    manager.create("alpha", session_id="newer").append(
        ChatMessage.user("recent", timestamp=LATE_TIMESTAMP)
    )
    manager.create("alpha", session_id="moved")
    store.update("alpha", current_session_id="moved")
    # Simulate the move: the current session's files leave the source home.
    manager.delete(SessionAddress(project_id=None, agent_id="alpha", session_id="moved"))

    result = store.reset_current_after_session_removed("alpha", "moved")

    assert result.current_session_id == "newer"


def test_reset_current_after_session_removed_creates_fresh_when_none_remain(
    store: AgentStore,
) -> None:
    agent = store.create("solo", "Solo")
    manager = ChatSessionManager(store.data_dir)
    moved_id = agent.current_session_id
    manager.delete(
        SessionAddress(project_id=None, agent_id="solo", session_id=moved_id)
    )  # the only session is moved away

    result = store.reset_current_after_session_removed("solo", moved_id)

    assert result.current_session_id != moved_id
    assert (
        manager.exists(
            SessionAddress(project_id=None, agent_id="solo", session_id=result.current_session_id)
        )
        is True
    )
    assert [session.id for session in manager.list("solo")] == [result.current_session_id]


def test_reset_current_after_session_removed_leaves_pointer_when_not_current(
    store: AgentStore,
) -> None:
    agent = store.create("beta", "Beta")
    manager = ChatSessionManager(store.data_dir)
    current_id = agent.current_session_id
    manager.create("beta", session_id="other")
    manager.delete(
        SessionAddress(project_id=None, agent_id="beta", session_id="other")
    )  # a non-current session was moved away

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
    restored = agents_module._agent_from_dict(data, data_dir=store.data_dir)

    assert data["temperature"] is None
    assert data["thinking_effort"] is None
    assert restored.temperature is None
    assert restored.thinking_effort is None


def test_delete_archives_agent_data_and_workspace(store: AgentStore) -> None:
    agent = store.create("coder", "Coder Agent")
    store._session_manager().create("coder", session_id="session")

    archive_dir = store.delete("coder")

    assert archive_dir == store.data_dir / "archive" / "agents" / "coder"
    assert not (store.data_dir / "agents" / "coder").exists()
    assert not Path(agent.workspace).exists()
    assert (archive_dir / "agent" / "agent.json").exists()
    assert not store._session_manager().exists(
        SessionAddress(project_id=None, agent_id="coder", session_id="session")
    )
    # The default workspace lives inside the agent directory, so it is archived
    # within the agent tree, not as a separate ``workspace/`` sibling.
    assert (archive_dir / "agent" / "workspace" / "SOUL.md").exists()


def test_delete_removes_agent_from_persisted_order(store: AgentStore) -> None:
    store.create("alpha", "Alpha")
    store.create("beta", "Beta")
    store.delete("alpha")

    listing = store.list_with_order()
    persisted = json.loads((store.data_dir / "agents" / "order.json").read_text(encoding="utf-8"))

    assert [agent.id for agent in listing.agents] == ["beta"]
    assert persisted["agent_ids"] == ["beta"]


def test_delete_archives_external_workspace_beside_agent(store: AgentStore, tmp_path: Path) -> None:
    # A custom workspace outside the agent tree (e.g. a repo an identity agent is
    # rooted in) is not swept up by the agent-directory move, so delete archives
    # it separately as ``workspace/``.
    external_workspace = tmp_path / "rooted-repo"
    store.create("coder", "Coder Agent", workspace=external_workspace)

    archive_dir = store.delete("coder")

    assert not (store.data_dir / "agents" / "coder").exists()
    assert not external_workspace.exists()
    assert (archive_dir / "agent" / "agent.json").exists()
    assert (archive_dir / "workspace" / "SOUL.md").exists()


def test_delete_restores_active_agent_and_previous_archive_on_session_failure(
    store: AgentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.create("coder", "First")
    previous_archive = store.delete("coder")
    marker = previous_archive / "keep.txt"
    marker.write_text("previous", encoding="utf-8")
    store.create("coder", "Second")

    def fail_archive(_agent_id: str) -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(store._session_manager(), "archive_identity_agent_sessions", fail_archive)

    with pytest.raises(RuntimeError, match="database unavailable"):
        store.delete("coder")

    assert store.get("coder").name == "Second"
    assert marker.read_text(encoding="utf-8") == "previous"


def test_delete_agent_named_like_sibling_archive_roots_never_touches_them(
    store: AgentStore,
) -> None:
    # ``archive/sessions`` and ``archive/projects`` are the session/project archive
    # roots. An agent id equal to those names must archive under the agents subtree
    # instead of replace-deleting the sibling roots wholesale.
    for reserved_like_id in ("sessions", "projects"):
        store.create(reserved_like_id, f"Agent {reserved_like_id}")

    archived_session = store.data_dir / "archive" / "sessions" / "agents" / "other" / "s1.jsonl"
    archived_session.parent.mkdir(parents=True)
    archived_session.write_text('{"role":"user"}\n', encoding="utf-8")
    archived_project = store.data_dir / "archive" / "projects" / "vbot" / "project.json"
    archived_project.parent.mkdir(parents=True)
    archived_project.write_text("{}\n", encoding="utf-8")

    for reserved_like_id in ("sessions", "projects"):
        archive_dir = store.delete(reserved_like_id)
        assert archive_dir == store.data_dir / "archive" / "agents" / reserved_like_id
        assert (archive_dir / "agent" / "agent.json").exists()

    assert archived_session.exists()
    assert archived_project.exists()


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
    # Memory files belong to the memory system and are never seeded by the workspace.
    assert not (custom_workspace / "USER.md").exists()
    assert not (custom_workspace / "MEMORY.md").exists()
