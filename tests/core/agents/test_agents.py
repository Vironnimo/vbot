"""Agent store creation, loading, listing and roster order."""

import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from core.agents import (
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
    agent_path,
    persisted,
    rewrite,
)
from tests.core.agents.agents_test_support import store as store
from tests.core.agents.agents_test_support import template_dir as template_dir


def test_create_writes_agent_json_sessions_and_workspace(store: AgentStore) -> None:
    agent = store.create("coder", "Coder Agent")

    data = persisted(store, "coder")

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


def test_create_defaults_name_and_workspace_from_the_id(store: AgentStore) -> None:
    agent = store.create("minimal")

    assert agent.name == "minimal"
    assert store.get("minimal").name == "minimal"
    # A default-created Agent's stored workspace equals the reported default, so the
    # WebUI's "uses a custom workspace" check (workspace != default) is False.
    assert agent.workspace == store.default_workspace("minimal")
    assert agent.workspace == str((store.data_dir / "agents" / "minimal" / "workspace").resolve())


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


def test_minimal_agent_config_loads_all_optional_field_defaults(store: AgentStore) -> None:
    agent_dir = store.data_dir / "agents" / "minimal"
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.json").write_text(
        '{"format_version": 1, "id": "minimal"}\n', encoding="utf-8"
    )

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
    (invalid_dir / "agent.json").write_text(
        '{"format_version": 1, "name": "Missing id"}\n', encoding="utf-8"
    )

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


def test_create_with_custom_values_persists_schema_and_keeps_workspace_files(
    store: AgentStore, tmp_path: Path
) -> None:
    custom_workspace = tmp_path / "custom-workspace"
    custom_workspace.mkdir()
    (custom_workspace / "SOUL.md").write_text("custom soul", encoding="utf-8")
    tools = {
        "bash": {"allowed_env": ["OPENAI_API_KEY"]},
        "subagent": {"allowed_agents": ["researcher", "builder@vbot"]},
    }

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
        tools={**tools, "bash": {"allowed_env": ["OPENAI_API_KEY", "OPENAI_API_KEY"]}},
        custom_system_prompt_enabled=True,
    )

    assert agent.workspace == str(custom_workspace.resolve())
    assert agent.tool_access == ToolAccess(mode="selected")
    assert agent.allowed_skills == ["memory"]
    assert agent.tools == tools  # duplicate env grants collapse
    assert agent.memory_prompt_mode == "agent"
    assert agent.custom_system_prompt_enabled is True
    data = persisted(store, "researcher_1")
    assert data["workspace"] == str(custom_workspace.resolve())
    assert data["tools"] == tools
    # Seeding never overwrites an existing workspace file, and memory files belong to
    # the memory system.
    assert (custom_workspace / "SOUL.md").read_text(encoding="utf-8") == "custom soul"
    assert not (custom_workspace / "USER.md").exists()
    assert not (custom_workspace / "MEMORY.md").exists()


@pytest.mark.parametrize(
    ("policy", "updated"),
    [
        ({"mode": "all", "granted": ["analyze_image"]}, {"mode": "all"}),
        (
            {"mode": "selected", "allowed": ["analyze_image"], "granted": ["analyze_image"]},
            {"mode": "selected", "allowed": ["analyze_image"]},
        ),
        (
            {"mode": "selected", "allowed": ["read_file"], "denied": ["memory"]},
            {"mode": "selected", "allowed": ["read"]},
        ),
    ],
    ids=["vision-grant-all", "vision-grant-selected", "explicit-denial"],
)
def test_tool_access_round_trips_and_updates_keep_tool_settings(
    store: AgentStore, policy: dict[str, Any], updated: dict[str, Any]
) -> None:
    subagent = {"subagent": {"allowed_agents": ["worker"]}}
    store.create("coder", "Coder", tool_access=policy, tools=subagent)

    assert store.get("coder").tool_access.to_dict() == policy
    assert persisted(store, "coder")["tool_access"] == policy

    # Revoking a grant or disabling a Tool keeps that Tool's settings for later.
    changed = store.update("coder", tool_access=updated)

    assert changed.tool_access.to_dict() == updated
    assert changed.tools == subagent
    assert persisted(store, "coder")["tools"] == subagent


def test_workspace_inside_data_dir_persists_relative_and_follows_a_moved_data_dir(
    tmp_path: Path, template_dir: Path
) -> None:
    original_data_dir = tmp_path / "original-data"
    original_store = AgentStore(original_data_dir, template_dir=template_dir)
    shared = original_data_dir / "shared-workspaces" / "researcher"
    researcher = original_store.create("researcher", "Researcher", workspace=shared)
    coder = original_store.create("coder", "Coder")
    Path(coder.workspace, "MEMORY.md").write_text("portable memory", encoding="utf-8")

    assert researcher.workspace == str(shared.resolve())
    assert persisted(original_store, "researcher")["workspace"] == "shared-workspaces/researcher"

    moved_data_dir = tmp_path / "moved-data"
    original_store._session_manager().close()
    shutil.move(str(original_data_dir), str(moved_data_dir))
    moved_store = AgentStore(moved_data_dir, template_dir=template_dir)
    loaded = moved_store.get("coder")

    expected_workspace = moved_data_dir / "agents" / "coder" / "workspace"
    assert loaded.workspace == str(expected_workspace.resolve())
    assert Path(loaded.workspace, "MEMORY.md").read_text(encoding="utf-8") == "portable memory"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("name", 12, "name must be a string or null"),
        ("model", 12, "model must be a string"),
        ("fallback_models", 12, "fallback_models must be a list of strings"),
        ("fallback_models", ["openai/gpt-5.2", "openai/gpt-5.2"], "must not contain duplicates"),
        ("fallback_models", ["openai/gpt-5.2"] * 6, "accepts at most 5 entries"),
        ("temperature", "0.4", "temperature must be a number"),
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
    message: str,
) -> None:
    name = value if field == "name" else "Coder Agent"
    fields: dict[str, Any] = {} if field == "name" else {field: value}

    with pytest.raises(AgentError, match=re.escape(message)):
        store.create("coder", name, **fields)  # type: ignore[arg-type]
    assert store.exists("coder") is False


@pytest.mark.parametrize("thinking_effort", [None, "", "none", "max"])
def test_create_accepts_supported_thinking_efforts_and_null_settings(
    store: AgentStore,
    thinking_effort: str | None,
) -> None:
    agent = store.create("coder", "Coder", temperature=None, thinking_effort=thinking_effort)

    assert agent.thinking_effort == thinking_effort
    assert agent.temperature is None
    raw = store.get_raw("coder")
    assert (raw.temperature, raw.thinking_effort) == (None, thinking_effort)
    assert persisted(store, "coder")["thinking_effort"] == thinking_effort
    assert persisted(store, "coder")["temperature"] is None


def test_create_rejects_duplicate_agent(store: AgentStore) -> None:
    store.create("coder", "Coder Agent")

    with pytest.raises(AgentAlreadyExistsError, match="coder"):
        store.create("coder", "Coder Agent")


@pytest.mark.parametrize("agent_id", ["", "../escape", "with space", "slash/name"])
def test_create_rejects_unsafe_agent_id(store: AgentStore, agent_id: str) -> None:
    with pytest.raises(InvalidAgentIdError):
        store.create(agent_id, "Unsafe Agent")


@pytest.mark.parametrize("agent_id", ["missing", "MAIN"])
def test_unknown_or_case_variant_agent_id_is_not_found(store: AgentStore, agent_id: str) -> None:
    # Ids are exact. A case-insensitive filesystem (Windows) opens the stored ``main``
    # tree for ``MAIN``; that different id must still name no Agent on every platform,
    # and lifecycle operations must never touch the real Agent through it.
    store.create("main", "Main")

    for operation in (
        lambda: store.get(agent_id),
        lambda: store.get_raw(agent_id),
        lambda: store.update(agent_id, name="Renamed"),
        lambda: store.rename(agent_id, "other"),
        lambda: store.delete(agent_id),
        lambda: store.reset_current_after_session_removed(agent_id, "ses_missing"),
    ):
        with pytest.raises(AgentNotFoundError, match=agent_id):
            operation()

    assert store.exists(agent_id) is False
    assert store.get("main").name == "Main"
    assert [agent.id for agent in store.list()] == ["main"]


@pytest.mark.parametrize(
    "changes",
    [{"id": "other"}, {"tool_access": {"mode": "selected"}}],
    ids=["id-disagrees-with-directory", "invalid-schema"],
)
def test_get_rejects_an_invalid_stored_agent(store: AgentStore, changes: dict[str, Any]) -> None:
    store.create("coder", "Coder")
    rewrite(store, "coder", **changes)

    # The stored Agent is broken, not missing.
    with pytest.raises(AgentError) as exc_info:
        store.get("coder")

    assert not isinstance(exc_info.value, AgentNotFoundError)
    assert store.exists("coder") is False


def test_create_appends_agents_to_persisted_order(store: AgentStore) -> None:
    store.create("beta", "Beta Agent")
    store.create("alpha", "Alpha Agent")

    listing = store.list_with_order()
    order = store.data_dir / "agents" / "order.json"

    assert [agent.id for agent in listing.agents] == ["beta", "alpha"]
    assert json.loads(order.read_text(encoding="utf-8")) == {
        "format_version": 1,
        "revision": listing.order_revision,
        "agent_ids": ["beta", "alpha"],
    }

    # Without a persisted order the roster falls back to historical id order.
    order.unlink()
    fallback = store.list_with_order()

    assert [agent.id for agent in fallback.agents] == ["alpha", "beta"]
    assert fallback.order_revision == 1


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


@pytest.mark.parametrize("conflict", ["stale-revision", "roster-changed"])
def test_reorder_rejects_a_conflict_without_changing_order(
    store: AgentStore, conflict: str
) -> None:
    store.create("alpha", "Alpha Agent")
    initial = store.list_with_order()
    store.create("beta", "Beta Agent")
    if conflict == "stale-revision":
        current = store.list_with_order()
        store.reorder(["beta", "alpha"], expected_revision=current.order_revision)
        expected = ["beta", "alpha"]
        order, match = ["alpha", "beta"], "order changed"
    else:
        expected = ["alpha", "beta"]
        order, match = ["alpha"], "roster changed"

    with pytest.raises(AgentOrderConflictError, match=match):
        store.reorder(order, expected_revision=initial.order_revision)

    assert [agent.id for agent in store.list()] == expected


def test_agent_update_keeps_unknown_fields_of_every_modeled_level(store: AgentStore) -> None:
    store.create("coder", "Coder")
    rewrite(
        store,
        "coder",
        future_field={"kept": True},
        allowed_tools=["bash"],
        tool_access={"mode": "all", "future_access": 1},
        tools={
            "bash": {"allowed_env": ["HOME"], "future_bash": 2},
            "subagent": {"allowed_agents": ["*"], "future_subagent": 3},
            "custom": {"anything": 4},
        },
        compaction_policy={
            "enabled": True,
            "trigger": {"type": "input_tokens", "tokens": 1000, "future_trigger": 5},
            "strategy": {"type": "continuation"},
            "future_policy": 6,
        },
    )

    loaded = store.get("coder")
    store.update("coder", name="Renamed")

    assert loaded.tools["bash"] == {"allowed_env": ["HOME"]}
    assert loaded.compaction_policy is not None
    assert "future_policy" not in loaded.compaction_policy
    rewritten = persisted(store, "coder")
    assert rewritten["format_version"] == 1
    assert rewritten["name"] == "Renamed"
    assert rewritten["future_field"] == {"kept": True}
    assert rewritten["allowed_tools"] == ["bash"]
    assert rewritten["tool_access"] == {"mode": "all", "future_access": 1}
    assert rewritten["tools"]["bash"]["future_bash"] == 2
    assert rewritten["tools"]["subagent"]["future_subagent"] == 3
    assert rewritten["tools"]["custom"] == {"anything": 4}
    assert rewritten["compaction_policy"]["future_policy"] == 6
    assert rewritten["compaction_policy"]["trigger"]["future_trigger"] == 5


def test_agent_written_by_a_newer_vbot_is_refused_and_left_unchanged(store: AgentStore) -> None:
    store.create("coder", "Coder")
    rewrite(store, "coder", format_version=2)
    original = agent_path(store, "coder").read_text(encoding="utf-8")

    with pytest.raises(AgentError, match="written by a newer vBot"):
        store.get("coder")
    with pytest.raises(AgentError, match="written by a newer vBot"):
        store.update("coder", name="Renamed")
    assert agent_path(store, "coder").read_text(encoding="utf-8") == original


def test_invalid_agent_order_is_never_overwritten(store: AgentStore) -> None:
    store.create("beta", "Beta Agent")
    store.create("alpha", "Alpha Agent")
    order_path = store.data_dir / "agents" / "order.json"
    original = '{"format_version": 2, "revision": 9, "agent_ids": ["alpha", "beta"]}'
    order_path.write_text(original, encoding="utf-8")

    listing = store.list_with_order()
    with pytest.raises(AgentError, match="Refusing to overwrite Agent order"):
        store.reorder(["alpha", "beta"], expected_revision=listing.order_revision)

    assert order_path.read_text(encoding="utf-8") == original
