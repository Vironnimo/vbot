"""Tests for agents defaults."""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from core.agents import (
    AgentNotFoundError,
    AgentStore,
)
from core.agents._config import _apply_defaults
from core.sessions import SessionAddress
from core.settings import AgentDefaults
from tests.core.agents.agents_test_support import (
    store as store,
)
from tests.core.agents.agents_test_support import (
    template_dir as template_dir,
)


def test_apply_defaults_fills_empty_model(store: AgentStore) -> None:
    agent = store.create("coder_model", "Coder Agent", model="")

    resolved = _apply_defaults(agent, AgentDefaults.from_dict({"model": "openai/gpt-5.2"}))

    assert resolved.model == "openai/gpt-5.2"


def test_apply_defaults_fills_none_temperature(store: AgentStore) -> None:
    agent = store.create("coder_temperature", "Coder Agent", temperature=None)

    resolved = _apply_defaults(agent, AgentDefaults.from_dict({"temperature": 0.7}))

    assert resolved.temperature == 0.7


def test_apply_defaults_leaves_explicit_values_unchanged(store: AgentStore) -> None:
    agent = store.create(
        "coder_explicit",
        "Coder Agent",
        model="openai/gpt-5.2",
        fallback_models=["openrouter/anthropic/claude-sonnet-4"],
        temperature=0.3,
        thinking_effort="high",
    )

    resolved = _apply_defaults(
        agent,
        AgentDefaults.from_dict(
            {
                "model": "openrouter/openai/gpt-4.1",
                "fallback_models": ["openai/gpt-5.2-mini"],
                "temperature": 0.9,
                "thinking_effort": "low",
            }
        ),
    )

    assert resolved.model == "openai/gpt-5.2"
    assert resolved.fallback_models == ["openrouter/anthropic/claude-sonnet-4"]
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


def test_get_raw_returns_unbaked_values_even_with_defaults(
    tmp_path: Path,
    template_dir: Path,
) -> None:
    # get_raw skips the defaults bake: the raw "" model and None temperature/thinking
    # survive even though a global default exists (the provenance seam the resolver
    # reads to tell an own value from an inherited default).
    store = AgentStore(
        tmp_path / "data",
        template_dir=template_dir,
        defaults_provider=lambda: {
            "model": "openai/gpt-5.2",
            "fallback_models": ["openai/gpt-5.2-mini"],
            "temperature": 0.6,
            "thinking_effort": "high",
        },
    )
    store.create(
        "coder_raw",
        "Coder Agent",
        model="",
        fallback_models=[],
        temperature=None,
        thinking_effort=None,
    )

    baked = store.get("coder_raw")
    raw = store.get_raw("coder_raw")

    # get bakes the defaults; get_raw preserves the un-set persisted values.
    assert baked.model == "openai/gpt-5.2"
    assert raw.model == ""
    assert raw.fallback_models == []
    assert raw.temperature is None
    assert raw.thinking_effort is None


def test_get_raw_preserves_explicit_own_values(
    tmp_path: Path,
    template_dir: Path,
) -> None:
    store = AgentStore(
        tmp_path / "data",
        template_dir=template_dir,
        defaults_provider=lambda: {"model": "openai/gpt-5.2", "temperature": 0.6},
    )
    store.create("coder_own", "Coder Agent", model="openai/gpt-mini", temperature=0.1)

    raw = store.get_raw("coder_own")

    assert raw.model == "openai/gpt-mini"
    assert raw.temperature == 0.1


def test_get_raw_raises_for_unknown_agent(store: AgentStore) -> None:
    with pytest.raises(AgentNotFoundError, match="missing"):
        store.get_raw("missing")


def test_create_returns_resolved_defaults_but_persists_raw_values(
    tmp_path: Path,
    template_dir: Path,
) -> None:
    store = AgentStore(
        tmp_path / "data",
        template_dir=template_dir,
        defaults_provider=lambda: {
            "model": "openai/gpt-5.2",
            "fallback_models": ["openai/gpt-5.2-mini"],
            "temperature": 0.6,
            "thinking_effort": "high",
        },
    )

    created = store.create(
        "coder_create_defaults",
        "Coder Agent",
        model="",
        fallback_models=[],
        temperature=None,
        thinking_effort=None,
    )

    assert created.model == "openai/gpt-5.2"
    assert created.fallback_models == ["openai/gpt-5.2-mini"]
    assert created.temperature == 0.6
    assert created.thinking_effort == "high"

    persisted = json.loads(
        (store.data_dir / "agents" / "coder_create_defaults" / "agent.json").read_text(
            encoding="utf-8"
        )
    )
    assert persisted["model"] == ""
    assert persisted["fallback_models"] == []
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
    agent_path.write_text(json.dumps(data), encoding="utf-8")

    loaded = store.get("legacy")

    assert loaded.current_session_id
    assert loaded.current_session_id != agent.current_session_id
    assert store._session_manager().exists(
        SessionAddress(project_id=None, agent_id="legacy", session_id=loaded.current_session_id)
    )
    normalized_data = json.loads(agent_path.read_text(encoding="utf-8"))
    assert normalized_data["current_session_id"] == loaded.current_session_id


def test_agent_without_workspace_is_normalized_to_default_workspace(store: AgentStore) -> None:
    store.create("missing_workspace", "Missing Workspace Agent")
    agent_path = store.data_dir / "agents" / "missing_workspace" / "agent.json"
    workspace_path = store.data_dir / "agents" / "missing_workspace" / "workspace"
    shutil.rmtree(workspace_path)
    data = json.loads(agent_path.read_text(encoding="utf-8"))
    data.pop("workspace")
    agent_path.write_text(json.dumps(data), encoding="utf-8")

    loaded = store.get("missing_workspace")

    assert loaded.workspace == str(workspace_path.resolve())
    assert workspace_path.is_dir()
    assert (workspace_path / "SOUL.md").exists()
    normalized_data = json.loads(agent_path.read_text(encoding="utf-8"))
    assert normalized_data["workspace"] == "agents/missing_workspace/workspace"


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
