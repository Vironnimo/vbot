"""Tests for pinned memory file backend."""

from pathlib import Path

import pytest

from core.memory import MemoryError, MemoryService


def test_memory_service_preserves_preamble_and_manages_entries(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    user_file = workspace / "USER.md"
    user_file.write_text("# User Profile\n\nExisting prose.\n", encoding="utf-8")
    service = MemoryService()

    first = service.add_entry(workspace, "user", "Prefers concise answers.")
    second = service.add_entry(workspace, "user", "Uses Windows.")

    assert first.id == 1
    assert second.id == 2
    assert [entry.content for entry in service.list_entries(workspace, "user")] == [
        "Prefers concise answers.",
        "Uses Windows.",
    ]
    content = user_file.read_text(encoding="utf-8")
    assert "Existing prose." in content
    assert "## Entries" in content
    assert "- Prefers concise answers." in content


def test_memory_service_creates_missing_agent_memory_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    service = MemoryService()

    entry = service.add_entry(workspace, "agent", "Check session_search before guessing.")

    assert entry.id == 1
    memory_file = workspace / "MEMORY.md"
    assert memory_file.exists()
    assert "# Agent Memory" in memory_file.read_text(encoding="utf-8")


def test_memory_service_replace_and_remove_entries(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    service = MemoryService()
    service.add_entry(workspace, "agent", "old fact")
    service.add_entry(workspace, "agent", "second fact")

    replaced = service.replace_entry(workspace, "agent", 1, "new fact")
    removed = service.remove_entry(workspace, "agent", 2)

    assert replaced.content == "new fact"
    assert removed.content == "second fact"
    assert [entry.content for entry in service.list_entries(workspace, "agent")] == ["new fact"]


def test_memory_service_rejects_invalid_entry_id(tmp_path: Path) -> None:
    service = MemoryService()

    with pytest.raises(MemoryError, match="entry_id"):
        service.remove_entry(tmp_path, "user", 1)
