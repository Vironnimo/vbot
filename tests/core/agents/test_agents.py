"""Tests for agents."""

import json
import shutil
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from core.agents import (
    Agent,
    AgentAlreadyExistsError,
    AgentError,
    AgentNotFoundError,
    AgentOrderConflictError,
    AgentStore,
    InvalidAgentIdError,
)
from core.sessions import SessionAddress
from core.tools.availability import ToolAccess
from tests.core.agents.agents_test_support import (
    TEMPLATE_FILES,
)
from tests.core.agents.agents_test_support import (
    store as store,
)
from tests.core.agents.agents_test_support import (
    template_dir as template_dir,
)


def test_agent_dataclass_is_frozen() -> None:
    agent = Agent(
        id="coder",
        name="Coder Agent",
        model="openai/gpt-5.2",
        fallback_models=[],
        workspace="C:/workspace",
        temperature=0.1,
        thinking_effort="",
        tool_access=ToolAccess(mode="all"),
        allowed_skills=["*"],
        tools={},
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
    assert data["fallback_models"] == []
    assert data["workspace"] == "agents/coder/workspace"
    assert data["root_project_id"] is None
    assert data["temperature"] is None
    assert data["thinking_effort"] is None
    assert data["memory_prompt_mode"] == "agent_user"
    assert data["tool_access"] == {"mode": "all"}
    assert data["allowed_skills"] == ["*"]
    assert "tools" not in data
    assert data["custom_system_prompt_enabled"] is False
    assert isinstance(data["current_session_id"], str)
    assert data["current_session_id"]
    assert data["created_at"].endswith("Z")
    assert data["updated_at"] == data["created_at"]
    assert (store.data_dir / "sessions.db").is_file()
    assert store._session_manager().exists(
        SessionAddress(project_id=None, agent_id="coder", session_id=data["current_session_id"])
    )
    assert agent.current_session_id == data["current_session_id"]
    assert agent == store.get("coder")

    workspace_path = Path(agent.workspace)
    for filename in TEMPLATE_FILES:
        assert (workspace_path / filename).read_text(encoding="utf-8") == f"# {filename}\n"


def test_missing_workspace_template_does_not_block_agent_creation(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    missing_templates = tmp_path / "missing-templates"
    store = AgentStore(tmp_path / "data", template_dir=missing_templates)

    with caplog.at_level("WARNING", logger="vbot.agents"):
        agent = store.create("repair-agent")

    assert store.get("repair-agent").id == agent.id
    assert not (Path(agent.workspace) / "SOUL.md").exists()
    assert str(missing_templates / "SOUL.md") in caplog.text


def test_create_rolls_back_the_session_when_workspace_setup_fails(
    store: AgentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_workspace: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, "_seed_workspace", fail)

    with pytest.raises(OSError, match="disk full"):
        store.create("coder")

    assert store._session_manager().list("coder") == []
    assert not (store.data_dir / "agents" / "coder").exists()


def test_agent_roster_scan_failure_returns_empty_roster(
    store: AgentStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    agents_dir = store.data_dir / "agents"
    agents_dir.mkdir(parents=True)

    def fail_scan(*_args: object, **_kwargs: object) -> list[Path]:
        raise OSError("scan failed")

    monkeypatch.setattr(Path, "glob", fail_scan)

    with caplog.at_level("WARNING", logger="vbot.agents"):
        result = store.list_with_order()

    assert result.agents == ()
    assert str(agents_dir) in caplog.text


def test_create_requires_only_agent_id(store: AgentStore) -> None:
    agent = store.create("minimal")

    assert agent.id == "minimal"
    assert agent.name == "minimal"
    assert store.get("minimal").name == "minimal"


def test_default_workspace_matches_created_agent_workspace(store: AgentStore) -> None:
    agent = store.create("coder", "Coder Agent")

    default = store.default_workspace("coder")
    assert default == str((store.data_dir / "agents" / "coder" / "workspace").resolve())
    # A default-created agent's stored workspace equals the reported default, so
    # the WebUI's "uses a custom workspace" check (workspace != default) is False.
    assert agent.workspace == default


def test_minimal_agent_config_loads_all_optional_field_defaults(store: AgentStore) -> None:
    agent_dir = store.data_dir / "agents" / "minimal"
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.json").write_text('{"id": "minimal"}\n', encoding="utf-8")

    agent = store.get("minimal")

    assert agent.name == "minimal"
    assert agent.model == ""
    assert agent.fallback_models == []
    assert agent.temperature is None
    assert agent.thinking_effort is None
    assert agent.tool_access == ToolAccess(mode="all")
    assert agent.allowed_skills == ["*"]
    assert agent.tools == {}
    assert agent.memory_prompt_mode == "agent_user"
    assert agent.custom_system_prompt_enabled is False
    assert agent.root_project_id is None
    assert agent.current_session_id
    assert agent.created_at
    assert agent.updated_at
    assert Path(agent.workspace) == agent_dir / "workspace"


def test_list_skips_invalid_agent_without_hiding_valid_agents(
    store: AgentStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store.create("valid", "Valid")
    invalid_dir = store.data_dir / "agents" / "invalid"
    invalid_dir.mkdir(parents=True)
    (invalid_dir / "agent.json").write_text('{"name": "Missing id"}\n', encoding="utf-8")

    agents = store.list()

    assert [agent.id for agent in agents] == ["valid"]
    assert store.exists("invalid") is False
    assert caplog.records


def test_ensure_bootstrap_avoids_invalid_main_directory(store: AgentStore) -> None:
    invalid_dir = store.data_dir / "agents" / "main"
    invalid_dir.mkdir(parents=True)
    (invalid_dir / "agent.json").write_text("not json\n", encoding="utf-8")

    created = store.ensure_bootstrap()

    assert created is not None
    assert created.id == "main-2"
    assert [agent.id for agent in store.list()] == ["main-2"]


def test_create_with_custom_values_persists_schema(store: AgentStore, tmp_path: Path) -> None:
    custom_workspace = tmp_path / "custom-workspace"
    agent = store.create(
        "researcher_1",
        "Research Agent",
        model="openrouter/deepseek/deepseek-v4-pro",
        fallback_models=["openai/gpt-5.2", "anthropic/claude-haiku-4.5"],
        workspace=custom_workspace,
        temperature=0.7,
        thinking_effort="high",
        memory_prompt_mode="agent",
        tool_access={"mode": "selected", "allowed": []},
        allowed_skills=["memory"],
        tools={
            "bash": {"allowed_env": ["OPENAI_API_KEY", "OPENAI_API_KEY"]},
            "subagent": {"allowed_agents": ["researcher", "builder@vbot"]},
        },
        custom_system_prompt_enabled=True,
    )

    assert agent.workspace == str(custom_workspace.resolve())
    assert agent.tool_access == ToolAccess(mode="selected")
    assert agent.allowed_skills == ["memory"]
    assert agent.tools == {
        "bash": {"allowed_env": ["OPENAI_API_KEY"]},
        "subagent": {"allowed_agents": ["researcher", "builder@vbot"]},
    }
    assert agent.memory_prompt_mode == "agent"
    assert agent.custom_system_prompt_enabled is True
    assert (custom_workspace / "SOUL.md").exists()
    agent_path = store.data_dir / "agents" / "researcher_1" / "agent.json"
    data = json.loads(agent_path.read_text(encoding="utf-8"))
    assert data["workspace"] == str(custom_workspace.resolve())
    assert data["tools"] == {
        "bash": {"allowed_env": ["OPENAI_API_KEY"]},
        "subagent": {"allowed_agents": ["researcher", "builder@vbot"]},
    }


def test_disabling_subagent_tools_preserves_their_settings(store: AgentStore) -> None:
    store.create(
        "orchestrator",
        "Orchestrator",
        tools={"subagent": {"allowed_agents": ["worker"]}},
    )

    updated = store.update(
        "orchestrator",
        tool_access={"mode": "selected", "allowed": ["read"]},
    )

    assert updated.tools == {"subagent": {"allowed_agents": ["worker"]}}
    agent_path = store.data_dir / "agents" / "orchestrator" / "agent.json"
    data = json.loads(agent_path.read_text(encoding="utf-8"))
    assert data["tools"] == {"subagent": {"allowed_agents": ["worker"]}}


def test_create_persists_workspace_inside_data_dir_relative(store: AgentStore) -> None:
    workspace = store.data_dir / "shared-workspaces" / "researcher"

    agent = store.create("researcher", "Researcher", workspace=workspace)

    assert agent.workspace == str(workspace.resolve())
    agent_path = store.data_dir / "agents" / "researcher" / "agent.json"
    data = json.loads(agent_path.read_text(encoding="utf-8"))
    assert data["workspace"] == "shared-workspaces/researcher"


def test_relative_default_workspace_follows_moved_data_dir(
    tmp_path: Path,
    template_dir: Path,
) -> None:
    original_data_dir = tmp_path / "original-data"
    original_store = AgentStore(original_data_dir, template_dir=template_dir)
    original = original_store.create("coder", "Coder")
    Path(original.workspace, "MEMORY.md").write_text("portable memory", encoding="utf-8")
    moved_data_dir = tmp_path / "moved-data"

    original_store._session_manager().close()
    shutil.move(str(original_data_dir), str(moved_data_dir))
    moved_store = AgentStore(moved_data_dir, template_dir=template_dir)
    loaded = moved_store.get("coder")

    expected_workspace = moved_data_dir / "agents" / "coder" / "workspace"
    assert loaded.workspace == str(expected_workspace.resolve())
    assert Path(loaded.workspace, "MEMORY.md").read_text(encoding="utf-8") == "portable memory"


@pytest.mark.parametrize("mode", ["all", "selected"])
def test_analyze_image_vision_grant_round_trips_and_can_be_revoked(
    store: AgentStore, mode: str
) -> None:
    policy = {"mode": mode, "granted": ["analyze_image"]}
    if mode == "selected":
        policy["allowed"] = ["analyze_image"]
    store.create("vision", "Vision Agent", tool_access=policy)
    assert store.get("vision").tool_access.to_dict() == policy
    policy.pop("granted")
    store.update("vision", tool_access=policy)
    assert store.get("vision").tool_access.to_dict() == policy


def test_create_persists_memory_as_an_explicit_denial(
    store: AgentStore,
) -> None:
    agent = store.create(
        "coder",
        "Coder Agent",
        tool_access={
            "mode": "selected",
            "allowed": ["read_file"],
            "denied": ["memory"],
        },
        memory_prompt_mode="agent_user",
    )

    agent_path = store.data_dir / "agents" / "coder" / "agent.json"
    data = json.loads(agent_path.read_text(encoding="utf-8"))

    assert agent.tool_access == ToolAccess(
        mode="selected",
        allowed=("read_file",),
        denied=("memory",),
    )
    assert data["tool_access"] == {
        "mode": "selected",
        "allowed": ["read_file"],
        "denied": ["memory"],
    }


@pytest.mark.parametrize(
    ("field", "value", "_message"),
    [
        ("name", 12, "name must be a string or null"),
        ("model", 12, "model must be a string"),
        ("fallback_models", 12, "fallback_models must be a list of strings"),
        ("fallback_models", ["openai/gpt-5.2", "openai/gpt-5.2"], "must not contain duplicates"),
        ("fallback_models", ["openai/gpt-5.2"] * 6, "accepts at most 5 entries"),
        ("temperature", "0.4", "temperature must be a number"),
        ("temperature", -0.1, "temperature must be between"),
        ("temperature", 2.1, "temperature must be between"),
        ("thinking_effort", "extreme", "thinking_effort must be one of"),
        ("memory_prompt_mode", "sometimes", "memory_prompt_mode must be one of"),
        ("memory_prompt_mode", True, "memory_prompt_mode must be a string"),
        ("tool_access", "read_file", "tool_access must be an object"),
        (
            "tool_access",
            {"mode": "selected", "allowed": ["read_file", 1]},
            "tool_access.allowed must be a list of strings",
        ),
        ("allowed_skills", "debugging", "allowed_skills must be a list of strings"),
        ("allowed_skills", ["debugging", None], "allowed_skills must be a list of strings"),
        ("tools", [], "tools must be an object"),
        (
            "tools",
            {"subagent": {"allowed_agents": ["worker", 1]}},
            "tools.subagent.allowed_agents must be a list of strings",
        ),
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
    _message: str,
) -> None:
    name = value if field == "name" else "Coder Agent"
    fields: dict[str, Any] = {} if field == "name" else {field: value}

    with pytest.raises(AgentError):
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
    data["tool_access"] = {"mode": "selected"}
    agent_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(AgentError):
        store.get("broken")


def test_create_appends_agents_to_persisted_order(store: AgentStore) -> None:
    store.create("beta", "Beta Agent")
    store.create("alpha", "Alpha Agent")

    listing = store.list_with_order()
    persisted = json.loads((store.data_dir / "agents" / "order.json").read_text(encoding="utf-8"))

    assert [agent.id for agent in listing.agents] == ["beta", "alpha"]
    assert persisted == {
        "revision": listing.order_revision,
        "agent_ids": ["beta", "alpha"],
    }


def test_missing_order_preserves_historical_id_order(store: AgentStore) -> None:
    store.create("beta", "Beta Agent")
    store.create("alpha", "Alpha Agent")
    (store.data_dir / "agents" / "order.json").unlink()

    listing = store.list_with_order()

    assert [agent.id for agent in listing.agents] == ["alpha", "beta"]
    assert listing.order_revision == 1


def test_reorder_persists_complete_roster_with_revision(store: AgentStore) -> None:
    store.create("alpha", "Alpha Agent")
    store.create("beta", "Beta Agent")
    initial = store.list_with_order()

    reordered = store.reorder(
        ["beta", "alpha"],
        expected_revision=initial.order_revision,
    )

    assert [agent.id for agent in reordered.agents] == ["beta", "alpha"]
    assert reordered.order_revision == initial.order_revision + 1
    assert reordered.order_changed is True
    assert [agent.id for agent in store.list()] == ["beta", "alpha"]


def test_reorder_rejects_stale_revision_without_changing_order(store: AgentStore) -> None:
    store.create("alpha", "Alpha Agent")
    store.create("beta", "Beta Agent")
    initial = store.list_with_order()
    store.reorder(["beta", "alpha"], expected_revision=initial.order_revision)

    with pytest.raises(AgentOrderConflictError, match="order changed"):
        store.reorder(["alpha", "beta"], expected_revision=initial.order_revision)

    assert [agent.id for agent in store.list()] == ["beta", "alpha"]


def test_reorder_rejects_changed_roster(store: AgentStore) -> None:
    store.create("alpha", "Alpha Agent")
    initial = store.list_with_order()
    store.create("beta", "Beta Agent")

    with pytest.raises(AgentOrderConflictError, match="roster changed"):
        store.reorder(["alpha"], expected_revision=initial.order_revision)

    assert [agent.id for agent in store.list()] == ["alpha", "beta"]
