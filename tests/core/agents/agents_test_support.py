"""Shared fixtures and helpers for Agent store tests."""

import json
from pathlib import Path
from typing import Any

import pytest

from core.agents import Agent, AgentStore
from core.database import write_bootstrap_marker

# The agent domain seeds only SOUL.md; USER.md/MEMORY.md are the memory system's and
# are created lazily on first write, never by workspace seeding.
TEMPLATE_FILES = ("SOUL.md",)


@pytest.fixture
def template_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "templates"
    directory.mkdir()
    for filename in TEMPLATE_FILES:
        (directory / filename).write_text(f"# {filename}\n", encoding="utf-8")
    return directory


@pytest.fixture
def store(tmp_path: Path, template_dir: Path) -> AgentStore:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    write_bootstrap_marker(data_dir)
    return AgentStore(data_dir, template_dir=template_dir)


def agent_path(store: AgentStore, agent_id: str) -> Path:
    return store.data_dir / "agents" / agent_id / "agent.json"


def persisted(store: AgentStore, agent_id: str) -> dict[str, Any]:
    """The raw ``agent.json`` document of one Agent."""
    data: dict[str, Any] = json.loads(agent_path(store, agent_id).read_text(encoding="utf-8"))
    return data


def rewrite(store: AgentStore, agent_id: str, *removed: str, **changes: Any) -> None:
    """Edit ``agent.json`` offline: drop ``removed`` keys and set ``changes``."""
    data = persisted(store, agent_id)
    for key in removed:
        data.pop(key)
    data.update(changes)
    agent_path(store, agent_id).write_text(json.dumps(data), encoding="utf-8")


def create_with_session(
    store: AgentStore, agent_id: str, name: str | None = None, *, session_id: str = "first"
) -> Agent:
    """Create an Agent plus one Session and point the Agent at it.

    Agent creation itself creates no Session; tests that need a current Session
    create it the way a first message does.
    """
    store.create(agent_id, name)
    store._session_manager().create(agent_id, session_id=session_id)
    return store.update(agent_id, current_session_id=session_id)
