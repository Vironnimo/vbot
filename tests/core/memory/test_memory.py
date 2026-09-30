"""Pinned memory: bullet-entry files per scope, budgets, text edits and prompt rendering."""

import logging
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
    MemoryBudgetError,
    MemoryError,
    MemoryMatchError,
    MemoryPromptMode,
    MemoryScope,
    MemoryService,
    MemoryTextChange,
    MemoryWriter,
    memory_block_definition,
    memory_prompt_file_paths,
    read_memory_files,
)


@pytest.fixture
def service() -> MemoryService:
    return MemoryService()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path / "workspace"


def _contents(service: MemoryService, workspace: Path, scope: MemoryScope) -> list[str]:
    return [entry.content for entry in service.list_entries(workspace, scope)]


@pytest.mark.parametrize(
    ("scope", "filename", "existing"),
    [
        ("user", "USER.md", "# User Profile\n\nExisting prose.\n"),
        ("agent", "MEMORY.md", None),
    ],
    ids=["prose-is-dropped", "missing-file-is-created"],
)
def test_add_entry_owns_the_file_as_bare_bullets(
    service: MemoryService,
    workspace: Path,
    scope: MemoryScope,
    filename: str,
    existing: str | None,
) -> None:
    # The memory tool fully owns the file: only "- " bullets, no preamble or heading.
    if existing is not None:
        workspace.mkdir()
        (workspace / filename).write_text(existing, encoding="utf-8")

    first = service.add_entry(workspace, scope, "Prefers concise answers.")
    second = service.add_entry(workspace, scope, "Uses Windows.")

    assert (first.id, second.id) == (1, 2)
    assert _contents(service, workspace, scope) == ["Prefers concise answers.", "Uses Windows."]
    assert (workspace / filename).read_text(encoding="utf-8") == (
        "- Prefers concise answers.\n- Uses Windows.\n"
    )


def test_hand_written_bullets_are_entries_and_other_lines_are_ignored(
    service: MemoryService, workspace: Path
) -> None:
    # There is no origin tracking: a bullet typed into the file by hand is a real entry.
    workspace.mkdir()
    memory_file = workspace / "MEMORY.md"
    memory_file.write_text(
        "- sonne ist toll\n  - eingerueckt bleibt\n\t- tab bleibt\nloose prose\n",
        encoding="utf-8",
    )

    assert _contents(service, workspace, "agent") == [
        "sonne ist toll",
        "eingerueckt bleibt",
        "tab bleibt",
    ]

    service.add_entry(workspace, "agent", "neu")

    assert memory_file.read_text(encoding="utf-8") == (
        "- sonne ist toll\n- eingerueckt bleibt\n- tab bleibt\n- neu\n"
    )


def test_entries_keep_literal_backslash_and_leading_dash(
    service: MemoryService, workspace: Path
) -> None:
    service.add_entry(workspace, "agent", "pass \\-v for verbose output")
    service.add_entry(workspace, "agent", "-leading dash survives")

    assert _contents(service, workspace, "agent") == [
        "pass \\-v for verbose output",
        "-leading dash survives",
    ]


def test_entries_are_replaced_and_removed_by_id(
    service: MemoryService, workspace: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="vbot.memory"):
        service.add_entry(
            workspace, "agent", "old fact", writer=MemoryWriter(agent_id="agent-one", actor="rpc")
        )
        service.add_entry(workspace, "agent", "second fact")
        service.add_entry(workspace, "agent", "second fact")

        replaced = service.replace_entry(workspace, "agent", 1, "new fact")
        removed = service.remove_entry(workspace, "agent", 2)

    assert replaced.content == "new fact"
    assert removed.content == "second fact"
    assert _contents(service, workspace, "agent") == ["new fact"]
    with pytest.raises(MemoryError):
        service.remove_entry(workspace, "agent", 2)
    # One line per changed file (the duplicate add is silent), never entry text.
    messages = [record.getMessage() for record in caplog.records if record.name == "vbot.memory"]
    assert len(messages) == 4
    assert "actor=rpc" in messages[0]
    assert not any("fact" in message for message in messages)


@pytest.mark.parametrize(
    "worker_count", [8, pytest.param(40, marks=[pytest.mark.stress, pytest.mark.timeout(120)])]
)
def test_concurrent_adds_do_not_lose_entries(
    service: MemoryService, workspace: Path, worker_count: int
) -> None:
    workspace.mkdir()
    barrier = threading.Barrier(worker_count)

    def add(index: int) -> None:
        # Release every worker at once so their read-modify-write windows overlap —
        # the exact condition that silently drops entries without a per-file lock.
        barrier.wait(timeout=10)
        service.add_entry(workspace, "agent", f"fact number {index}")

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        for future in [executor.submit(add, index) for index in range(worker_count)]:
            future.result()

    assert sorted(_contents(service, workspace, "agent")) == sorted(
        f"fact number {index}" for index in range(worker_count)
    )


def test_full_scope_rejects_growth_until_an_entry_is_removed(
    service: MemoryService, workspace: Path
) -> None:
    scopes: tuple[MemoryScope, ...] = ("agent", "user")
    budgets = {scope: service.scope_usage(workspace, scope)[1] for scope in scopes}
    for scope, budget in budgets.items():
        service.add_entry(workspace, scope, "a" * (budget // 2))
        service.add_entry(workspace, scope, "b" * (budget - budget // 2))
    agent_budget = budgets["agent"]
    half = agent_budget // 2

    with pytest.raises(MemoryBudgetError):
        service.add_entry(workspace, "agent", "z")
    service.remove_entry(workspace, "agent", 1)
    added = service.add_entry(workspace, "agent", "z" * half)

    assert added.content == "z" * half
    # Each scope has its own budget.
    assert service.scope_usage(workspace, "agent") == (agent_budget, agent_budget)
    assert service.scope_usage(workspace, "user") == (budgets["user"], budgets["user"])


def test_replace_may_grow_an_entry_exactly_to_the_budget(
    service: MemoryService, workspace: Path
) -> None:
    _used, budget = service.scope_usage(workspace, "user")
    first = budget // 2
    second = budget - first
    service.add_entry(workspace, "user", "a" * first)
    service.add_entry(workspace, "user", "b" * (second - 100))

    # One character past the budget is rejected and the entry stays unchanged.
    with pytest.raises(MemoryError):
        service.replace_entry(workspace, "user", 2, "c" * (second + 1))
    assert _contents(service, workspace, "user")[1] == "b" * (second - 100)

    replaced = service.replace_entry(workspace, "user", 2, "c" * second)
    assert replaced.content == "c" * second


def test_budget_error_reports_resulting_total(service: MemoryService, workspace: Path) -> None:
    service.add_entry(workspace, "user", "a" * 2000)

    with pytest.raises(MemoryBudgetError) as error:
        service.add_entry(workspace, "user", "b" * 1500)

    assert (error.value.scope, error.value.total, error.value.budget) == ("user", 3500, 3000)
    assert "free at least 500 characters" in str(error.value)


def test_text_addressed_changes_write_lf_and_report_previous_text(
    service: MemoryService, workspace: Path
) -> None:
    service.add_entry(workspace, "agent", "Uses pytest.")
    service.add_entry(workspace, "agent", "Deploys from main.")

    replaced = service.replace_matching(workspace, "agent", "pytest", "Uses pytest with xdist.")
    removed = service.remove_matching(workspace, "agent", "- Deploys from main.")

    assert replaced == MemoryTextChange("agent", "Uses pytest.", "Uses pytest with xdist.")
    assert removed == MemoryTextChange("agent", "Deploys from main.", None)
    assert (workspace / "MEMORY.md").read_bytes() == b"- Uses pytest with xdist.\n"
    assert service.scope_usage(workspace, "agent") == (23, 4000)


def test_replace_matching_folds_into_an_identical_existing_entry(
    service: MemoryService, workspace: Path
) -> None:
    service.add_entry(workspace, "user", "Prefers short answers.")
    service.add_entry(workspace, "user", "Prefers concise answers.")

    service.replace_matching(workspace, "user", "short", "Prefers concise answers.")

    assert _contents(service, workspace, "user") == ["Prefers concise answers."]


def test_match_errors_carry_matches_and_current_entries(
    service: MemoryService, workspace: Path
) -> None:
    service.add_entry(workspace, "agent", "Host A needs VPN.")
    service.add_entry(workspace, "agent", "Host B needs VPN.")

    with pytest.raises(MemoryMatchError) as ambiguous:
        service.remove_matching(workspace, "agent", "needs VPN")
    with pytest.raises(MemoryMatchError) as missing:
        service.replace_matching(workspace, "agent", "Postgres", "x")

    assert ambiguous.value.matches == ("Host A needs VPN.", "Host B needs VPN.")
    assert missing.value.matches == ()
    assert missing.value.entries == ("Host A needs VPN.", "Host B needs VPN.")
    assert len(service.list_entries(workspace, "agent")) == 2


def test_a_file_that_is_not_utf8_fails_as_memory_error_and_is_kept(
    service: MemoryService, workspace: Path
) -> None:
    workspace.mkdir()
    memory_file = workspace / "MEMORY.md"
    memory_file.write_bytes(b"- Caf\xe9 in Latin-1.\n")

    with pytest.raises(MemoryError):
        service.read_prompt_files(workspace, MEMORY_PROMPT_MODE_AGENT)
    with pytest.raises(MemoryError):
        service.add_entry(workspace, "agent", "New fact.")

    assert memory_file.read_bytes() == b"- Caf\xe9 in Latin-1.\n"


def test_prompt_renders_selected_scopes_in_mode_order(
    service: MemoryService, workspace: Path
) -> None:
    # The producer's data half: scope headings and bullets only; the <memory> wrapper
    # and the guidance live in the declared memory:guidance block.
    workspace.mkdir()
    (workspace / "MEMORY.md").write_text("- Agent fact\n", encoding="utf-8")
    (workspace / "USER.md").write_text("- User fact\n", encoding="utf-8")

    agent_only = service.read_prompt_files(workspace, MEMORY_PROMPT_MODE_AGENT)
    agent_and_user = service.read_prompt_files(workspace, MEMORY_PROMPT_MODE_AGENT_USER)

    assert "Agent fact" in agent_only
    assert "User fact" not in agent_only
    assert "<memory>" not in agent_only
    assert "<file name=" not in agent_only
    assert agent_and_user.index("Agent fact") < agent_and_user.index("User fact")
    assert service.read_prompt_files(workspace, MEMORY_PROMPT_MODE_OFF) == ""


def test_missing_memory_files_render_like_empty_ones_without_being_created(
    service: MemoryService, workspace: Path
) -> None:
    # Lazy ownership: a not-yet-created scope renders the empty-scope placeholder, so
    # the Model always sees the scope, and rendering never creates a file.
    workspace.mkdir()

    missing = read_memory_files(workspace, MEMORY_PROMPT_MODE_AGENT_USER, provider=service)

    assert "No entries yet." in missing
    assert read_memory_files(workspace, MEMORY_PROMPT_MODE_OFF, provider=service) == ""
    assert list(workspace.iterdir()) == []
    entry = service.add_entry(workspace, "agent", "temporary")
    service.remove_entry(workspace, "agent", entry.id)
    (workspace / "USER.md").write_text("", encoding="utf-8")
    assert (workspace / "MEMORY.md").exists()
    assert service.read_prompt_files(workspace, MEMORY_PROMPT_MODE_AGENT_USER) == missing


@pytest.mark.parametrize(
    ("mode", "existing", "expected"),
    [
        (MEMORY_PROMPT_MODE_AGENT, ["MEMORY.md", "USER.md"], ["MEMORY.md"]),
        (MEMORY_PROMPT_MODE_AGENT_USER, ["MEMORY.md", "USER.md"], ["MEMORY.md", "USER.md"]),
        (MEMORY_PROMPT_MODE_AGENT_USER, ["MEMORY.md"], ["MEMORY.md"]),
        (MEMORY_PROMPT_MODE_OFF, ["MEMORY.md"], []),
    ],
)
def test_memory_prompt_file_paths_lists_existing_selected_files(
    workspace: Path, mode: MemoryPromptMode, existing: list[str], expected: list[str]
) -> None:
    # The read-before-write stamping source: absent files have nothing to stamp.
    workspace.mkdir()
    for name in existing:
        (workspace / name).write_text("- fact\n", encoding="utf-8")

    assert memory_prompt_file_paths(workspace, mode) == [
        (workspace / name).resolve() for name in expected
    ]


def test_memory_block_definition_declares_guidance_and_embedded_marker() -> None:
    definition = memory_block_definition()

    assert definition.id == MEMORY_BLOCK_ID
    assert definition.owner == MEMORY_BLOCK_OWNER
    assert definition.kind == "text"
    assert definition.editable is True
    assert definition.default_text is not None
    assert definition.default_text.startswith("<memory>")
    assert definition.default_text.endswith("</memory>")
    assert f"{{generated:{MEMORY_FILES_PRODUCER_NAME}}}" in definition.default_text
