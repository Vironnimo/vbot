"""Global Agent defaults and load-time normalization of older Agent files."""

import shutil
from pathlib import Path
from typing import Any

import pytest

from core.agents import AgentStore
from tests.core.agents.agents_test_support import persisted, rewrite
from tests.core.agents.agents_test_support import store as store
from tests.core.agents.agents_test_support import template_dir as template_dir

_UNSET: dict[str, Any] = {
    "model": "",
    "fallback_models": [],
    "temperature": None,
    "thinking_effort": None,
}


def test_reads_resolve_live_defaults_while_files_and_raw_reads_keep_own_values(
    tmp_path: Path,
    template_dir: Path,
) -> None:
    # get_raw is the provenance seam the resolver reads to tell an own value from an
    # inherited default.
    defaults: dict[str, Any] = {
        "model": "openai/gpt-5.2",
        "fallback_models": ["openai/gpt-5.2-mini"],
        "temperature": 0.6,
        "thinking_effort": "high",
    }
    own: dict[str, Any] = {
        "model": "openai/gpt-mini",
        "fallback_models": ["openrouter/anthropic/claude-sonnet-4"],
        "temperature": 0.1,
        "thinking_effort": "low",
    }
    store = AgentStore(
        tmp_path / "data", template_dir=template_dir, defaults_provider=lambda: defaults
    )

    created = store.create("inherits", "Inheriting Agent", **_UNSET)
    store.create("own", "Own Agent", **own)

    def settings(agent: Any) -> dict[str, Any]:
        return {field: getattr(agent, field) for field in _UNSET}

    assert settings(created) == defaults
    assert settings(store.get("inherits")) == defaults
    assert settings(store.get_raw("inherits")) == _UNSET
    assert {field: persisted(store, "inherits")[field] for field in _UNSET} == _UNSET
    assert settings(store.get("own")) == own
    assert settings(store.get_raw("own")) == own
    # The roster carries both views from one read.
    roster = store.list_with_order()
    assert [settings(agent) for agent in roster.agents] == [defaults, own]
    assert roster.persisted == {
        agent_id: store.get_raw(agent_id) for agent_id in ("inherits", "own")
    }

    # A default change applies to the next read without rewriting the Agent file.
    defaults.update(model="openrouter/anthropic/claude-sonnet-4", temperature=0.9)
    assert store.get("inherits").model == "openrouter/anthropic/claude-sonnet-4"
    assert store.get("inherits").temperature == 0.9
    assert settings(store.get("own")) == own

    # Setting an own value writes exactly that value, never the default.
    assert store.update("inherits", temperature=0.4).temperature == 0.4
    assert persisted(store, "inherits")["temperature"] == 0.4
    assert persisted(store, "inherits")["model"] == ""


def test_tool_loading_inherits_each_part_the_agent_does_not_set(
    tmp_path: Path, template_dir: Path
) -> None:
    defaults: dict[str, Any] = {"tool_loading": {"on_demand": True, "always_loaded": ["read"]}}
    store = AgentStore(
        tmp_path / "data", template_dir=template_dir, defaults_provider=lambda: defaults
    )
    own: dict[str, dict[str, Any] | None] = {
        "inherits": None,
        "switched_off": {"on_demand": False},
        "own_list": {"always_loaded": []},
        "own_both": {"on_demand": False, "always_loaded": ["bash"]},
    }
    for agent_id, tool_loading in own.items():
        store.create(agent_id, agent_id, tool_loading=tool_loading)

    assert {agent_id: store.get(agent_id).tool_loading for agent_id in own} == {
        "inherits": {"on_demand": True, "always_loaded": ["read"]},
        "switched_off": {"on_demand": False, "always_loaded": ["read"]},
        "own_list": {"on_demand": True, "always_loaded": []},
        "own_both": {"on_demand": False, "always_loaded": ["bash"]},
    }
    # The Agent files and raw reads keep only the Agents' own parts.
    assert {agent_id: store.get_raw(agent_id).tool_loading for agent_id in own} == own
    assert "tool_loading" not in persisted(store, "inherits")

    # A default without a list leaves the built-in always-loaded Tools in place.
    defaults["tool_loading"] = {"on_demand": True}
    assert store.get("inherits").tool_loading == {"on_demand": True}
    assert store.get("switched_off").tool_loading == {"on_demand": False}


def test_missing_session_pointer_stays_empty_and_missing_workspace_is_repaired_on_load(
    store: AgentStore,
) -> None:
    store.create("legacy", "Legacy Agent")
    workspace_path = store.data_dir / "agents" / "legacy" / "workspace"
    shutil.rmtree(workspace_path)
    rewrite(store, "legacy", "current_session_id", "workspace")

    loaded = store.get("legacy")

    # A missing Session pointer stays empty: no Session is created for it.
    assert loaded.current_session_id == ""
    assert store._session_manager().list_addresses(None, agent_id="legacy") == []
    # A missing workspace becomes the recreated and seeded default workspace.
    assert loaded.workspace == str(workspace_path.resolve())
    assert (workspace_path / "SOUL.md").exists()
    data = persisted(store, "legacy")
    assert data.get("current_session_id", "") == ""
    assert data["workspace"] == "agents/legacy/workspace"


@pytest.mark.parametrize("librarian_switch", ["missing", "null"])
def test_missing_workspace_directory_and_switches_load_without_a_rewrite(
    store: AgentStore, librarian_switch: str
) -> None:
    agent = store.create("legacy", "Legacy Agent")
    workspace_path = Path(agent.workspace)
    shutil.rmtree(workspace_path)
    if librarian_switch == "missing":
        rewrite(store, "legacy", "custom_system_prompt_enabled", "librarian_enabled")
    else:
        rewrite(store, "legacy", "custom_system_prompt_enabled", librarian_enabled=None)

    loaded = store.get("legacy")

    assert loaded.workspace == agent.workspace
    assert (workspace_path / "SOUL.md").exists()
    assert loaded.custom_system_prompt_enabled is False
    # An agent.json from before the Librarian switch keeps the Librarian on.
    assert loaded.librarian_enabled is True
    assert "custom_system_prompt_enabled" not in persisted(store, "legacy")
