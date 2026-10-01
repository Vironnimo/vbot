"""Startup recovery: interrupted archive operations end consistent; orphan payloads are adopted."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from core.sessions import (
    ARCHIVE_KIND_AGENT,
    ARCHIVE_KIND_PROJECT,
    ARCHIVE_ORIGIN_RECOVERED,
    ARCHIVE_TREE_AGENT,
    ARCHIVE_TREE_PROJECT,
    ArchiveEntryFilter,
    ArchiveTree,
    SessionAddress,
)
from tests.core.archive.archive_test_support import ArchiveWorld
from tests.core.archive.archive_test_support import world as world


def _fail(*_args: Any, **_kwargs: Any) -> Any:
    raise OSError("stopped here")


def test_interrupted_archives_are_rolled_back(world: ArchiveWorld, tmp_path: Path) -> None:
    agents, ledger = world.agents, world.sessions.archive_ledger
    coder = agents.create("coder")
    repo = tmp_path / "repo"
    repo.mkdir()
    world.projects.create("vbot", "vBot", repo)
    workspace = agents.create("rooted", workspace=tmp_path / "home").workspace
    agents.update("rooted", root_project_id="vbot")
    # The process stopped after the files moved and before the Sessions were committed.
    agent_ref = ledger.begin(
        ARCHIVE_KIND_AGENT,
        subject_id="coder",
        agent_id="coder",
        trees=lambda entry_id: (
            ArchiveTree(f"archive/entries/{entry_id}/agent", ARCHIVE_TREE_AGENT, "agents/coder"),
        ),
    )
    project_ref = ledger.begin(
        ARCHIVE_KIND_PROJECT,
        subject_id="vbot",
        project_id="vbot",
        facts={
            "unrooted_agents": [
                {"agent_id": "rooted", "workspace_before": workspace, "workspace_reset": True}
            ]
        },
        trees=lambda entry_id: (
            ArchiveTree(
                f"archive/entries/{entry_id}/project", ARCHIVE_TREE_PROJECT, "projects/vbot"
            ),
        ),
    )
    agents.update("rooted", root_project_id=None, workspace=agents.default_workspace("rooted"))
    for ref, name, source in (
        (agent_ref, "agent", world.data_dir / "agents" / "coder"),
        (project_ref, "project", world.data_dir / "projects" / "vbot"),
    ):
        world.payload(ref.entry_id, name).parent.mkdir(parents=True)
        os.replace(source, world.payload(ref.entry_id, name))

    world.service.recover()

    assert agents.get("coder").current_session_id == coder.current_session_id
    assert world.sessions.exists(SessionAddress(None, "coder", coder.current_session_id))
    assert world.projects.get("vbot").display_name == "vBot"
    rooted = agents.get("rooted")
    assert (rooted.root_project_id, rooted.workspace) == ("vbot", workspace)
    assert not ledger.page(ArchiveEntryFilter()).entries
    entries = world.data_dir / "archive" / "entries"
    assert not entries.exists() or not any(entries.iterdir())
    assert world.changes


@pytest.mark.asyncio
@pytest.mark.parametrize("id_reused", [False, True], ids=["id-free", "id-reused"])
async def test_a_failed_agent_cleanup_keeps_the_archive_and_completes_at_the_next_start(
    world: ArchiveWorld, monkeypatch: pytest.MonkeyPatch, id_reused: bool
) -> None:
    world.agents.create("coder")
    world.agents.create("manager", tools={"subagent": {"allowed_agents": ["coder"]}})
    monkeypatch.setattr(world.agents, "remove_delegation_grants", _fail)

    archived = await world.service.archive_agent("coder")

    monkeypatch.undo()
    entry = world.entry(archived.entry_id)
    assert (entry.state, entry.cleanup_pending) == ("archived", True)
    assert not world.agents.exists("coder")
    assert (world.skill_invalidations, world.recall_removals) == (
        ["coder"],
        [(None, "coder", None)],
    )
    assert world.changes
    if id_reused:
        # A new Agent took the free id; the delegation lists naming it are now its grants.
        world.agents.create("coder")

    world.service.recover()

    entry = world.entry(archived.entry_id)
    assert (entry.state, entry.cleanup_pending) == ("archived", False)
    allowed = world.agents.get("manager").tools["subagent"]["allowed_agents"]
    if id_reused:
        assert (allowed, entry.facts.get("grants")) == (["coder"], None)
    else:
        assert (allowed, entry.facts["grants"]) == ([], [{"agent_id": "manager", "index": 0}])


@pytest.mark.asyncio
async def test_an_interrupted_restore_is_rolled_back_or_completed(
    world: ArchiveWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    agents, ledger = world.agents, world.sessions.archive_ledger
    agents.create("coder")
    agents.create("manager", tools={"subagent": {"allowed_agents": ["coder"]}})
    entry_id = (await world.service.archive_agent("coder")).entry_id
    # Stopped after the files moved back and before the Sessions were committed.
    ledger.begin_restore(entry_id, {"target_id": None, "strip_channel_keys": False})
    os.replace(world.payload(entry_id, "agent"), world.data_dir / "agents" / "coder")

    world.service.recover()

    assert world.entry(entry_id).state == "archived"
    assert not agents.exists("coder")
    assert (world.payload(entry_id, "agent") / "agent.json").is_file()

    # Stopped after the commit, before the follow-up re-added the grants.
    monkeypatch.setattr(agents, "restore_delegation_grants", _fail)
    with pytest.raises(OSError, match="stopped here"):
        await world.service.restore(entry_id)
    monkeypatch.undo()
    assert world.entry(entry_id).state == "restored"

    world.service.recover()

    assert ledger.entry(entry_id) is None
    assert agents.get("manager").tools["subagent"]["allowed_agents"] == ["coder"]


@pytest.mark.asyncio
async def test_a_purging_entry_waits_for_the_next_purge(world: ArchiveWorld) -> None:
    world.agents.create("coder")
    entry_id = (await world.service.archive_agent("coder")).entry_id
    world.sessions.archive_ledger.begin_purge(entry_id)

    world.service.recover()

    assert world.entry(entry_id).state == "purging"
    outcome = await world.service.purge([entry_id])
    assert [item.entry_id for item in outcome.purged] == [entry_id]
    assert world.session_rows("coder") == []


@pytest.mark.asyncio
async def test_orphan_entry_payloads_are_adopted_and_nothing_else_under_archive(
    world: ArchiveWorld,
) -> None:
    world.agents.create("coder", "Coder Agent")
    archive = world.data_dir / "archive"
    # A data snapshot restore can bring back a payload whose entry row it predates.
    with world.agents.archive_files("coder", archive / "entries" / "arc_orphan" / "agent"):
        pass
    (archive / "entries" / "arc_empty").mkdir()
    user_files = [archive / "entries" / "notes" / "keep.txt", archive / "mine" / "keep.txt"]
    for path in user_files:
        path.parent.mkdir(parents=True)
        path.write_text("mine", encoding="utf-8")

    world.service.recover()

    [listing] = (await world.service.list(ArchiveEntryFilter())).entries
    assert (listing.entry.entry_id, listing.entry.kind, listing.entry.subject_id) == (
        "arc_orphan",
        ARCHIVE_KIND_AGENT,
        "coder",
    )
    assert (listing.entry.origin, listing.label, listing.restorable) == (
        ARCHIVE_ORIGIN_RECOVERED,
        "Coder Agent",
        True,
    )
    assert not (archive / "entries" / "arc_empty").exists()
    assert all(path.read_text(encoding="utf-8") == "mine" for path in user_files)
    await world.service.restore("arc_orphan")
    assert world.agents.get("coder").name == "Coder Agent"
