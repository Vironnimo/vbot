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
from core.memory.memory import _MAX_ENTRY_LENGTH, _MAX_SCOPE_BUDGET, _MEMORY_GUIDANCE


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


def test_memory_service_preserves_literal_backslash_dash(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    service = MemoryService()

    service.add_entry(workspace, "agent", "pass \\-v for verbose output")
    service.add_entry(workspace, "agent", "-leading dash survives")

    contents = [entry.content for entry in service.list_entries(workspace, "agent")]
    assert contents == ["pass \\-v for verbose output", "-leading dash survives"]


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

    assert agent_only == (
        f'<memory>\n{_MEMORY_GUIDANCE}\n\n<file name="MEMORY.md">\nAgent memory\n</file>\n</memory>'
    )
    assert '<file name="MEMORY.md">' in agent_and_user
    assert '<file name="USER.md">' in agent_and_user
    assert disabled == ""


def test_memory_service_prompt_block_includes_guidance_when_enabled(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "MEMORY.md").write_text("Agent memory", encoding="utf-8")
    service = MemoryService()

    enabled = service.build_prompt_block(workspace, MEMORY_PROMPT_MODE_AGENT)
    disabled = service.build_prompt_block(workspace, MEMORY_PROMPT_MODE_OFF)

    assert _MEMORY_GUIDANCE in enabled
    # Guidance never appears without a rendered memory block.
    assert disabled == ""


def test_memory_service_prompt_block_omits_missing_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "MEMORY.md").write_text("Agent memory", encoding="utf-8")
    service = MemoryService()

    prompt_block = service.build_prompt_block(workspace, MEMORY_PROMPT_MODE_AGENT_USER)

    assert "Agent memory" in prompt_block
    assert "USER.md" not in prompt_block


def test_memory_service_rejects_add_exceeding_scope_budget(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    service = MemoryService()
    entry_len = _MAX_ENTRY_LENGTH
    fill_count = _MAX_SCOPE_BUDGET["agent"] // entry_len

    for index in range(fill_count):
        service.add_entry(workspace, "agent", chr(ord("a") + index) * entry_len)

    with pytest.raises(MemoryError, match="full"):
        service.add_entry(workspace, "agent", "z" * entry_len)

    assert len(service.list_entries(workspace, "agent")) == fill_count


def test_memory_service_remove_frees_scope_budget(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    service = MemoryService()
    entry_len = _MAX_ENTRY_LENGTH
    fill_count = _MAX_SCOPE_BUDGET["agent"] // entry_len

    for index in range(fill_count):
        service.add_entry(workspace, "agent", chr(ord("a") + index) * entry_len)
    with pytest.raises(MemoryError, match="full"):
        service.add_entry(workspace, "agent", "z" * entry_len)

    service.remove_entry(workspace, "agent", 1)
    added = service.add_entry(workspace, "agent", "z" * entry_len)

    assert added.content == "z" * entry_len
    assert len(service.list_entries(workspace, "agent")) == fill_count


def test_memory_service_scope_budgets_are_independent(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    service = MemoryService()
    entry_len = _MAX_ENTRY_LENGTH
    agent_count = _MAX_SCOPE_BUDGET["agent"] // entry_len
    user_count = _MAX_SCOPE_BUDGET["user"] // entry_len

    for index in range(agent_count):
        service.add_entry(workspace, "agent", chr(ord("a") + index) * entry_len)
    # The agent scope is now full; the user scope has its own independent budget.
    for index in range(user_count):
        service.add_entry(workspace, "user", chr(ord("a") + index) * entry_len)

    assert len(service.list_entries(workspace, "agent")) == agent_count
    assert len(service.list_entries(workspace, "user")) == user_count


def test_memory_service_replace_respects_scope_budget(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    service = MemoryService()
    budget = _MAX_SCOPE_BUDGET["user"]
    first = budget // 2
    second = budget - first  # first + second fills exactly to the budget

    service.add_entry(workspace, "user", "a" * first)
    service.add_entry(workspace, "user", "b" * (second - 100))

    # Growing the second entry one char past the budget is rejected; the
    # original entry is preserved because the write never happened.
    with pytest.raises(MemoryError, match="full"):
        service.replace_entry(workspace, "user", 2, "c" * (second + 1))
    assert service.list_entries(workspace, "user")[1].content == "b" * (second - 100)

    # Growing it to exactly the budget is allowed (total == budget, not over).
    replaced = service.replace_entry(workspace, "user", 2, "c" * second)
    assert replaced.content == "c" * second
