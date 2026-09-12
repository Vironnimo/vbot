"""Tests for agents rename."""

import json
from pathlib import Path

import pytest

from core.agents import (
    Agent,
    AgentAlreadyExistsError,
    AgentStore,
)
from core.sessions import ChatSessionManager, SessionAddress
from tests.core.agents.agents_test_support import (
    store as store,
)
from tests.core.agents.agents_test_support import (
    template_dir as template_dir,
)


def test_rename_moves_complete_agent_tree_and_rebases_internal_workspace(
    store: AgentStore,
) -> None:
    created = store.create("coder", "Coder Agent")
    old_dir = store.data_dir / "agents" / "coder"
    custom_workspace = old_dir / "homes" / "primary"
    store.update("coder", workspace=custom_workspace)
    (old_dir / "prompts").mkdir()
    (old_dir / "prompts" / "runtime.md").write_text("custom prompt", encoding="utf-8")
    (old_dir / "skills" / "private-skill").mkdir(parents=True)
    (old_dir / "skills" / "private-skill" / "SKILL.md").write_text("# Private\n", encoding="utf-8")
    ChatSessionManager(store.data_dir).create("coder", session_id="kept-session")

    result = store.rename("coder", "researcher")

    new_dir = store.data_dir / "agents" / "researcher"
    assert not old_dir.exists()
    assert result.agent.id == "researcher"
    assert result.agent.name == "Coder Agent"
    assert result.agent.created_at == created.created_at
    assert result.agent.updated_at != created.updated_at
    assert result.agent.workspace == str((new_dir / "homes" / "primary").resolve())
    assert store._session_manager().exists(
        SessionAddress(project_id=None, agent_id="researcher", session_id="kept-session")
    )
    assert not store._session_manager().exists(
        SessionAddress(project_id=None, agent_id="coder", session_id="kept-session")
    )
    assert (new_dir / "prompts" / "runtime.md").read_text(encoding="utf-8") == "custom prompt"
    assert (new_dir / "skills" / "private-skill" / "SKILL.md").is_file()
    persisted = json.loads((new_dir / "agent.json").read_text(encoding="utf-8"))
    assert persisted["id"] == "researcher"
    assert persisted["workspace"] == "agents/researcher/homes/primary"


def test_rename_preserves_external_workspace(store: AgentStore, tmp_path: Path) -> None:
    workspace = tmp_path / "external-workspace"
    store.create("coder", "Coder Agent", workspace=workspace)

    renamed = store.rename("coder", "researcher").agent

    assert renamed.workspace == str(workspace.resolve())
    assert workspace.is_dir()
    assert (workspace / "SOUL.md").is_file()


def test_restore_rename_retargets_sessions_back_to_the_original_agent(store: AgentStore) -> None:
    created = store.create("coder", "Coder Agent")
    result = store.rename("coder", "researcher")

    store.restore_rename(result)

    assert store._session_manager().exists(
        SessionAddress(None, "coder", created.current_session_id)
    )
    assert not store._session_manager().exists(
        SessionAddress(None, "researcher", created.current_session_id)
    )


def test_rename_supports_case_only_id_change(store: AgentStore) -> None:
    store.create("coder", "Coder Agent")

    renamed = store.rename("coder", "Coder").agent

    assert renamed.id == "Coder"
    assert store.get("Coder").id == "Coder"
    assert sorted(path.name for path in (store.data_dir / "agents").iterdir()) == [
        "Coder",
        "order.json",
    ]


def test_rename_preserves_agent_order_position(store: AgentStore) -> None:
    store.create("alpha", "Alpha")
    store.create("beta", "Beta")
    store.create("gamma", "Gamma")
    listing = store.list_with_order()
    store.reorder(
        ["gamma", "alpha", "beta"],
        expected_revision=listing.order_revision,
    )

    store.rename("alpha", "renamed")

    assert [agent.id for agent in store.list()] == ["gamma", "renamed", "beta"]


def test_rename_rejects_existing_destination(store: AgentStore) -> None:
    store.create("coder", "Coder Agent")
    store.create("researcher", "Researcher Agent")

    with pytest.raises(AgentAlreadyExistsError, match="researcher"):
        store.rename("coder", "researcher")

    assert store.get("coder").name == "Coder Agent"
    assert store.get("researcher").name == "Researcher Agent"


def test_rename_rolls_tree_back_when_config_write_fails(
    store: AgentStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store.create("coder", "Coder Agent")
    original_write = store._write_agent

    def fail_new_config(agent: Agent) -> None:
        if agent.id == "researcher":
            raise OSError("disk full")
        original_write(agent)

    monkeypatch.setattr(store, "_write_agent", fail_new_config)

    with pytest.raises(OSError, match="disk full"):
        store.rename("coder", "researcher")

    assert store.get("coder").id == "coder"
    assert not (store.data_dir / "agents" / "researcher").exists()


def test_retarget_allowed_agent_references_is_exact_and_reversible(store: AgentStore) -> None:
    store.create(
        "coder",
        "Coder Agent",
        tools={"subagent": {"allowed_agents": ["coder", "coder@project"]}},
    )
    store.create(
        "manager",
        "Manager Agent",
        tools={"subagent": {"allowed_agents": ["coder", "researcher", "coder@project"]}},
    )
    store.rename("coder", "researcher")
    manager_before = store.get_raw("manager")

    update = store.retarget_allowed_agent_references("coder", "researcher")

    assert update.agent_ids == ("researcher", "manager")
    assert store.get("manager").tools["subagent"]["allowed_agents"] == [
        "researcher",
        "coder@project",
    ]
    assert store.get("researcher").tools["subagent"]["allowed_agents"] == [
        "researcher",
        "coder@project",
    ]

    store.restore_allowed_agent_references(update)

    assert store.get_raw("manager") == manager_before
