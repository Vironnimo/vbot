"""Terminal manager: groups behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

import core.tools._terminal_io as terminal_io
from core.tools.terminal_manager import (
    TerminalManager,
    TerminalManagerError,
    TerminalNotFoundError,
)
from tests.core.tools.terminal_manager_helpers import (
    AdapterFactory,
    spawn,
)
from tests.core.tools.terminal_manager_helpers import (
    terminal_manager as terminal_manager,
)


async def _spawn_manual(
    manager: TerminalManager,
    tmp_path: Path,
    *,
    group_id: str | None = None,
) -> str:
    result = await manager.spawn_for_operator(
        command=None,
        arguments=[],
        cwd=tmp_path,
        group_id=group_id,
    )
    return str(result["terminal_id"])


@pytest.mark.asyncio
async def test_user_groups_are_unique_persistent_and_renamable(tmp_path: Path) -> None:
    groups_path = tmp_path / "terminals" / "groups.json"
    manager = TerminalManager(
        groups_path=groups_path,
        data_dir=tmp_path,
        sweep_interval_seconds=3600,
    )
    try:
        created = manager.create_group_for_operator("Work")
        assert created["kind"] == "user"
        assert created["terminal_count"] == 0
        with pytest.raises(TerminalManagerError, match="already exists"):
            manager.create_group_for_operator("work")

        renamed = manager.rename_group_for_operator(created["group_id"], "Dev")
        assert renamed["name"] == "Dev"
        assert groups_path.is_file()
    finally:
        await manager.aclose()

    reloaded = TerminalManager(
        groups_path=groups_path,
        data_dir=tmp_path,
        sweep_interval_seconds=3600,
    )
    try:
        groups = reloaded.list_groups_for_operator()
        assert [(group["name"], group["kind"]) for group in groups] == [("Dev", "user")]
    finally:
        await reloaded.aclose()


@pytest.mark.asyncio
async def test_agent_group_is_created_and_reused_by_name(tmp_path: Path) -> None:
    manager = TerminalManager(sweep_interval_seconds=3600)
    try:
        first = manager.resolve_or_create_agent_group("codex")
        second = manager.resolve_or_create_agent_group("codex")
        assert first.group_id == second.group_id
        assert first.kind == "agent"

        # An operator user group with the same name wins the reuse lookup.
        user = manager.create_group_for_operator("My Codex")
        reused = manager.resolve_or_create_agent_group("my codex")
        assert reused.group_id == user["group_id"]
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_spawned_terminals_join_explicit_and_automatic_groups(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, _factory = terminal_manager
    group = manager.create_group_for_operator("Work")
    agent_session = await spawn(manager, tmp_path)
    manual_id = await _spawn_manual(manager, tmp_path)
    grouped_id = await _spawn_manual(manager, tmp_path, group_id=group["group_id"])

    by_id = {summary["terminal_id"]: summary for summary in manager.list_for_operator()}
    assert by_id[agent_session.terminal_id]["group_id"] == "auto:agent:agent-a"
    assert by_id[manual_id]["group_id"] == "auto:manual"
    assert by_id[grouped_id]["group_id"] == group["group_id"]

    groups = manager.list_groups_for_operator()
    by_name = {item["name"]: item for item in groups}
    assert by_name["Work"]["terminal_count"] == 1
    assert by_name["Manual"]["terminal_count"] == 1
    assert by_name["Agent agent-a"]["terminal_count"] == 1
    assert "finished" not in by_name


@pytest.mark.asyncio
async def test_killed_terminal_moves_to_finished_group_only_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terminal_io, "TERMINAL_INITIAL_INPUT_QUIET_SECONDS", 0.01)
    factory = AdapterFactory("READY> ")
    manager = TerminalManager(adapter_factory=factory, sweep_interval_seconds=3600)
    manager.start()
    try:
        session = await spawn(manager, tmp_path)
        await manager.kill_for_operator(session.terminal_id)

        by_id = {summary["terminal_id"]: summary for summary in manager.list_for_operator()}
        assert by_id[session.terminal_id]["group_id"] == "finished"
        groups = manager.list_groups_for_operator()
        finished = [group for group in groups if group["kind"] == "finished"]
        assert len(finished) == 1
        assert finished[0]["terminal_count"] == 1

        manager.forget_for_operator(session.terminal_id)
        groups = manager.list_groups_for_operator()
        assert all(group["kind"] != "finished" for group in groups)
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_group_order_is_persisted_and_new_terminals_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    groups_path = tmp_path / "terminals" / "groups.json"
    factory = AdapterFactory()
    manager = TerminalManager(
        adapter_factory=factory,
        groups_path=groups_path,
        data_dir=tmp_path,
        sweep_interval_seconds=3600,
    )
    manager.start()
    try:
        group = manager.create_group_for_operator("Work")
        group_id = group["group_id"]
        first = await _spawn_manual(manager, tmp_path, group_id=group_id)
        second = await _spawn_manual(manager, tmp_path, group_id=group_id)

        manager.set_group_order_for_operator(group_id, [second, first])
        listed = [item["terminal_id"] for item in manager.list_for_operator()]
        assert listed[:2] == [second, first]

        third = await _spawn_manual(manager, tmp_path, group_id=group_id)
        listed = [item["terminal_id"] for item in manager.list_for_operator()]
        assert listed == [second, first, third]

        with pytest.raises(TerminalManagerError, match="do not belong"):
            manager.set_group_order_for_operator(group_id, ["other-terminal"])
    finally:
        await manager.aclose()

    reloaded = TerminalManager(
        groups_path=groups_path,
        data_dir=tmp_path,
        sweep_interval_seconds=3600,
    )
    groups = reloaded.list_groups_for_operator()
    assert groups[0]["order"] == [second, first]


@pytest.mark.asyncio
async def test_deleting_a_group_kills_every_terminal_in_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terminal_io, "TERMINAL_INITIAL_INPUT_QUIET_SECONDS", 0.01)
    factory = AdapterFactory("READY> ")
    manager = TerminalManager(adapter_factory=factory, sweep_interval_seconds=3600)
    manager.start()
    try:
        group = manager.create_group_for_operator("Work")
        group_id = group["group_id"]
        first = await _spawn_manual(manager, tmp_path, group_id=group_id)
        second = await _spawn_manual(manager, tmp_path, group_id=group_id)
        outsider = await _spawn_manual(manager, tmp_path)

        result = await manager.delete_group_for_operator(group_id)
        assert result["terminals_killed"] == 2
        group_ids = {item["group_id"] for item in manager.list_groups_for_operator()}
        assert group_id not in group_ids

        by_id = {summary["terminal_id"]: summary for summary in manager.list_for_operator()}
        assert by_id[first]["state"] == "exited"
        assert by_id[second]["state"] == "exited"
        assert by_id[first]["group_id"] == "finished"
        assert by_id[second]["group_id"] == "finished"
        assert by_id[outsider]["state"] != "exited"

        with pytest.raises(TerminalNotFoundError):
            await manager.delete_group_for_operator(group_id)
    finally:
        await manager.aclose()
