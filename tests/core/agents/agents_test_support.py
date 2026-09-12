"""Shared fixtures and fakes for agents behavior tests."""





from pathlib import Path

import pytest

from core.agents import (
    AgentStore,
)
from core.sessions.format import write_bootstrap_marker

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
