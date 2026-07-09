"""Tests for pinned memory file backend."""

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from core.memory import (
    MEMORY_BLOCK_ID,
    MEMORY_BLOCK_OWNER,
    MEMORY_FILES_PRODUCER_NAME,
    MEMORY_PROMPT_MODE_AGENT,
    MEMORY_PROMPT_MODE_AGENT_USER,
    MEMORY_PROMPT_MODE_OFF,
    MemoryError,
    MemoryService,
    memory_block_definition,
    memory_prompt_file_paths,
    read_memory_files,
)
from core.memory.memory import _MAX_ENTRY_LENGTH, _MAX_SCOPE_BUDGET, _MEMORY_GUIDANCE


def test_memory_service_stores_entries_as_bare_bullets(tmp_path: Path) -> None:
    # The file holds only the entries now (bare "- " bullets) — no preamble, no
    # "## Entries" heading. Any freeform prose already in the file is dropped on the
    # first write; the memory tool fully owns the file's content.
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
    assert content == "- Prefers concise answers.\n- Uses Windows.\n"
    assert "Existing prose." not in content
    assert "## Entries" not in content
    assert "# User Profile" not in content


def test_memory_service_reads_hand_written_bullets_as_entries(tmp_path: Path) -> None:
    # Entries are just "- " bullet lines; there is no origin tracking, so a bullet
    # typed into the file by hand is a real entry, indistinguishable from a tool-added
    # one. A non-bullet line is not an entry and is ignored.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "MEMORY.md").write_text("- sonne ist toll\nloose prose\n", encoding="utf-8")
    service = MemoryService()

    entries = service.list_entries(workspace, "agent")

    assert [entry.content for entry in entries] == ["sonne ist toll"]


def test_memory_service_creates_missing_agent_memory_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    service = MemoryService()

    entry = service.add_entry(workspace, "agent", "Check session_search before guessing.")

    assert entry.id == 1
    memory_file = workspace / "MEMORY.md"
    assert memory_file.exists()
    content = memory_file.read_text(encoding="utf-8")
    assert content == "- Check session_search before guessing.\n"
    assert "# Agent Memory" not in content


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


def test_read_prompt_files_renders_scope_heading_and_entries(tmp_path: Path) -> None:
    # The producer's data half: each selected scope under its heading label with its
    # "- " bullet entries — no <file> wrapper, no <memory> wrapper, and no guidance
    # (those live in the declared memory:guidance block). Agent scope renders first.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "MEMORY.md").write_text("- Agent fact\n", encoding="utf-8")
    (workspace / "USER.md").write_text("- User fact\n", encoding="utf-8")
    service = MemoryService()

    agent_only = service.read_prompt_files(workspace, MEMORY_PROMPT_MODE_AGENT)
    agent_and_user = service.read_prompt_files(workspace, MEMORY_PROMPT_MODE_AGENT_USER)
    disabled = service.read_prompt_files(workspace, MEMORY_PROMPT_MODE_OFF)

    assert agent_only == "# Agent Memory\n- Agent fact"
    assert "<memory>" not in agent_only
    assert "<file name=" not in agent_only
    assert _MEMORY_GUIDANCE not in agent_only
    assert agent_and_user == "# Agent Memory\n- Agent fact\n\n# User Profile\n- User fact"
    assert disabled == ""


def test_read_prompt_files_renders_placeholder_for_missing_file(tmp_path: Path) -> None:
    # Lazy ownership: a not-yet-created file is not omitted — its scope renders the
    # empty-scope placeholder (identical to an emptied on-disk file), so the model
    # always sees the scope, and reading never creates the file.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "MEMORY.md").write_text("- Agent fact\n", encoding="utf-8")
    service = MemoryService()

    files = service.read_prompt_files(workspace, MEMORY_PROMPT_MODE_AGENT_USER)

    assert files == "# Agent Memory\n- Agent fact\n\n# User Profile\nNo entries yet."
    assert not (workspace / "USER.md").exists()


def test_read_prompt_files_renders_placeholders_when_no_files_exist(tmp_path: Path) -> None:
    # With no memory files on disk, an enabled mode still renders both selected scopes
    # via the empty-scope placeholder; only off reads nothing. Rendering never creates
    # a file as a side effect.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = MemoryService()

    rendered = read_memory_files(workspace, MEMORY_PROMPT_MODE_AGENT_USER, provider=service)

    assert rendered == "# Agent Memory\nNo entries yet.\n\n# User Profile\nNo entries yet."
    assert read_memory_files(workspace, MEMORY_PROMPT_MODE_OFF, provider=service) == ""
    assert not (workspace / "MEMORY.md").exists()
    assert not (workspace / "USER.md").exists()


def test_read_prompt_files_missing_matches_empty_on_disk_file(tmp_path: Path) -> None:
    # The lazy-ownership guarantee: the prompt is identical whether a memory file
    # exists but is empty or has not been created yet.
    virtual_workspace = tmp_path / "virtual"
    virtual_workspace.mkdir()
    real_workspace = tmp_path / "real"
    real_workspace.mkdir()
    service = MemoryService()
    entry = service.add_entry(real_workspace, "agent", "temporary")
    service.remove_entry(real_workspace, "agent", entry.id)
    assert (real_workspace / "MEMORY.md").exists()

    virtual = service.read_prompt_files(virtual_workspace, MEMORY_PROMPT_MODE_AGENT)
    real = service.read_prompt_files(real_workspace, MEMORY_PROMPT_MODE_AGENT)

    assert virtual == real


def test_memory_prompt_file_paths_returns_existing_selected_files(tmp_path: Path) -> None:
    # The read-before-write stamping source: resolved absolute paths of the memory
    # files a mode injects that exist on disk, in mode order.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "MEMORY.md").write_text("Agent memory", encoding="utf-8")
    (workspace / "USER.md").write_text("User memory", encoding="utf-8")

    agent_only = memory_prompt_file_paths(workspace, MEMORY_PROMPT_MODE_AGENT)
    agent_and_user = memory_prompt_file_paths(workspace, MEMORY_PROMPT_MODE_AGENT_USER)

    assert agent_only == [(workspace / "MEMORY.md").resolve()]
    assert agent_and_user == [
        (workspace / "MEMORY.md").resolve(),
        (workspace / "USER.md").resolve(),
    ]


def test_memory_prompt_file_paths_skips_absent_files(tmp_path: Path) -> None:
    # A not-yet-created memory file (rendered as default content, no bytes on disk)
    # has nothing to stamp, so it is omitted.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "MEMORY.md").write_text("Agent memory", encoding="utf-8")

    paths = memory_prompt_file_paths(workspace, MEMORY_PROMPT_MODE_AGENT_USER)

    assert paths == [(workspace / "MEMORY.md").resolve()]


def test_memory_prompt_file_paths_off_mode_is_empty(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "MEMORY.md").write_text("Agent memory", encoding="utf-8")

    assert memory_prompt_file_paths(workspace, MEMORY_PROMPT_MODE_OFF) == []


def test_memory_block_definition_declares_guidance_and_embedded_marker() -> None:
    # The declared memory:guidance block ships the guidance prose plus the embedded
    # {generated:memory_files} marker inside a <memory> wrapper, owner "memory".
    definition = memory_block_definition()

    assert definition.id == MEMORY_BLOCK_ID
    assert definition.owner == MEMORY_BLOCK_OWNER
    assert definition.kind == "text"  # static, editable
    assert definition.editable is True
    assert definition.default_text is not None
    assert definition.default_text.startswith("<memory>")
    assert definition.default_text.endswith("</memory>")
    assert _MEMORY_GUIDANCE in definition.default_text
    assert f"{{generated:{MEMORY_FILES_PRODUCER_NAME}}}" in definition.default_text


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
