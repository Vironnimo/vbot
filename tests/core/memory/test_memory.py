"""Tests for pinned memory file backend."""

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from core.memory import (
    MEMORY_PROMPT_MODE_AGENT,
    MEMORY_PROMPT_MODE_AGENT_USER,
    MEMORY_PROMPT_MODE_OFF,
    MemoryError,
    MemoryService,
)


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


def test_memory_service_concurrent_adds_do_not_lose_entries(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = MemoryService()

    worker_count = 40
    barrier = threading.Barrier(worker_count)

    def add(index: int) -> None:
        # Release every worker at once so their read-modify-write windows overlap —
        # the exact condition that silently drops entries without a per-file lock.
        barrier.wait(timeout=10)
        service.add_entry(workspace, "agent", f"fact number {index}")

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        for future in [executor.submit(add, index) for index in range(worker_count)]:
            future.result()

    entries = service.list_entries(workspace, "agent")
    assert {entry.content for entry in entries} == {
        f"fact number {index}" for index in range(worker_count)
    }
    assert len(entries) == worker_count


def test_memory_service_rejects_invalid_entry_id(tmp_path: Path) -> None:
    service = MemoryService()

    with pytest.raises(MemoryError, match="entry_id"):
        service.remove_entry(tmp_path, "user", 1)


def test_memory_service_builds_prompt_block_for_selected_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "MEMORY.md").write_text("Agent memory", encoding="utf-8")
    (workspace / "USER.md").write_text("User memory", encoding="utf-8")
    service = MemoryService()

    agent_only = service.build_prompt_block(workspace, MEMORY_PROMPT_MODE_AGENT)
    agent_and_user = service.build_prompt_block(workspace, MEMORY_PROMPT_MODE_AGENT_USER)
    disabled = service.build_prompt_block(workspace, MEMORY_PROMPT_MODE_OFF)

    assert agent_only == '<memory>\n<file name="MEMORY.md">\nAgent memory\n</file>\n</memory>'
    assert '<file name="MEMORY.md">' in agent_and_user
    assert '<file name="USER.md">' in agent_and_user
    assert disabled == ""


def test_memory_service_prompt_block_omits_missing_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "MEMORY.md").write_text("Agent memory", encoding="utf-8")
    service = MemoryService()

    prompt_block = service.build_prompt_block(workspace, MEMORY_PROMPT_MODE_AGENT_USER)

    assert "Agent memory" in prompt_block
    assert "USER.md" not in prompt_block
