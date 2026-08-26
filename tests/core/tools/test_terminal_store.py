"""Unit tests for the durable operator store behind Terminal groups and launches."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.tools.terminal_store import (
    TERMINAL_LAUNCH_HISTORY_MAX_ENTRIES,
    TerminalGroup,
    TerminalOperatorStore,
    validate_group_name,
)

_SEED_TIMESTAMP = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _write_groups(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def test_remember_launch_round_trips_through_the_file(tmp_path: Path) -> None:
    history_path = tmp_path / "launch-history.json"
    store = TerminalOperatorStore(
        launch_history_path=history_path, groups_path=None, data_dir=tmp_path
    )

    store.remember_launch(command="pwsh", arguments=["-NoLogo"], workdir="C:/repo")
    store.remember_launch(command="nvim", arguments=(), workdir=None)

    reloaded = TerminalOperatorStore(
        launch_history_path=history_path, groups_path=None, data_dir=tmp_path
    )
    assert [entry.command for entry in reloaded.launch_history] == ["nvim", "pwsh"]
    assert reloaded.launch_history[1].arguments == ("-NoLogo",)
    assert reloaded.launch_history[1].workdir == "C:/repo"


def test_identical_configuration_moves_to_front_instead_of_duplicating(tmp_path: Path) -> None:
    store = TerminalOperatorStore(launch_history_path=None, groups_path=None, data_dir=tmp_path)

    store.remember_launch(command="pwsh", arguments=["-NoLogo"], workdir="C:/repo")
    first_used_at = store.launch_history[0].used_at
    store.remember_launch(command="pwsh", arguments=["-NoLogo"], workdir="C:/repo")

    assert len(store.launch_history) == 1
    assert store.launch_history[0].command == "pwsh"
    assert store.launch_history[0].used_at >= first_used_at


def test_launch_history_is_capped_at_the_maximum(tmp_path: Path) -> None:
    store = TerminalOperatorStore(launch_history_path=None, groups_path=None, data_dir=tmp_path)

    for index in range(TERMINAL_LAUNCH_HISTORY_MAX_ENTRIES + 10):
        store.remember_launch(command=f"cmd-{index}", arguments=(), workdir=None)

    assert len(store.launch_history) == TERMINAL_LAUNCH_HISTORY_MAX_ENTRIES
    assert store.launch_history[0].command == f"cmd-{TERMINAL_LAUNCH_HISTORY_MAX_ENTRIES + 9}"


def test_corrupt_documents_degrade_to_empty_collections(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    history_path = tmp_path / "launch-history.json"
    groups_path = tmp_path / "groups.json"
    history_path.write_text("not json", encoding="utf-8")
    _write_groups(groups_path, {"version": 1, "groups": [{"id": "broken"}]})

    with caplog.at_level("WARNING"):
        store = TerminalOperatorStore(
            launch_history_path=history_path, groups_path=groups_path, data_dir=tmp_path
        )

    assert store.launch_history == []
    assert store.groups == {}
    assert any("launch history" in message for message in caplog.messages)
    assert any("groups" in message for message in caplog.messages)


def test_persist_groups_writes_only_user_groups_and_survives_reload(tmp_path: Path) -> None:
    groups_path = tmp_path / "groups.json"
    store = TerminalOperatorStore(
        launch_history_path=None, groups_path=groups_path, data_dir=tmp_path
    )

    user_group = TerminalGroup(
        group_id="u-1",
        name="Builds",
        kind="user",
        order=["t-2", "t-1"],
        created_at=_SEED_TIMESTAMP,
    )
    agent_group = TerminalGroup(
        group_id="a-1", name="Agent", kind="agent", order=[], created_at=_SEED_TIMESTAMP
    )
    store.groups[user_group.group_id] = user_group
    store.groups[agent_group.group_id] = agent_group
    store.persist_groups()

    reloaded = TerminalOperatorStore(
        launch_history_path=None, groups_path=groups_path, data_dir=tmp_path
    )
    assert list(reloaded.groups) == ["u-1"]
    restored = reloaded.groups["u-1"]
    assert restored.name == "Builds"
    assert restored.kind == "user"
    assert restored.order == ["t-2", "t-1"]
    assert restored.created_at == user_group.created_at


def test_group_name_lookup_is_case_insensitive_across_user_and_agent_kinds() -> None:
    store = TerminalOperatorStore(launch_history_path=None, groups_path=None, data_dir=None)
    store.groups["u-1"] = TerminalGroup(
        group_id="u-1", name="Builds", kind="user", order=[], created_at=_SEED_TIMESTAMP
    )
    store.groups["auto:agent:x"] = TerminalGroup(
        group_id="auto:agent:x",
        name="BUILD",
        kind="automatic",
        order=[],
        created_at=_SEED_TIMESTAMP,
    )

    found = store.group_by_name("builds")
    assert found is not None and found.group_id == "u-1"
    # Automatic groups never satisfy a by-name lookup or block a new name.
    assert store.group_by_name("build") is None
    assert store.group_name_taken("builds") is True
    assert store.group_name_taken("builds", exclude="u-1") is False


def test_validate_group_name_trims_and_rejects_invalid_input() -> None:
    assert validate_group_name("  Builds ") == "Builds"
    with pytest.raises(ValueError):
        validate_group_name("   ")
    with pytest.raises(ValueError):
        validate_group_name("x" * 81)
