"""Startup recovery: interrupted archive operations end consistent; orphan payloads are adopted."""

from __future__ import annotations

import errno
import os
import shutil
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
from tests.core.archive.archive_test_support import ArchiveWorld, legacy_agent_entry
from tests.core.archive.archive_test_support import world as world


def _fail(*_args: Any, **_kwargs: Any) -> Any:
    raise OSError("stopped here")


def _refuse_access(monkeypatch: pytest.MonkeyPatch, locked: Path) -> None:
    """Make ``locked`` unreadable the way a folder without read permission is.

    Its own entry stays visible, but listing it fails, and so does every check
    of a path inside it, as ``Path.is_dir`` and ``Path.is_file`` raise before
    Python 3.14.
    """

    def refuse(method: str, inside_only: bool) -> None:
        real = getattr(Path, method)

        def guarded(self: Path, *args: Any, **kwargs: Any) -> Any:
            if locked in self.parents or (not inside_only and self == locked):
                raise PermissionError(errno.EACCES, "Access is denied", str(self))
            return real(self, *args, **kwargs)

        monkeypatch.setattr(Path, method, guarded)

    refuse("iterdir", inside_only=False)
    for method in ("stat", "is_dir", "is_file", "exists"):
        refuse(method, inside_only=True)


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
@pytest.mark.parametrize("payload", ["partial-copy", "only-copy"])
async def test_an_interrupted_archive_beside_an_intact_source_never_loses_either(
    world: ArchiveWorld, payload: str
) -> None:
    agents, ledger = world.agents, world.sessions.archive_ledger
    agents.create("coder", "Coder")
    home = world.data_dir / "agents" / "coder"
    ref = ledger.begin(
        ARCHIVE_KIND_AGENT,
        subject_id="coder",
        agent_id="coder",
        trees=lambda entry_id: (
            ArchiveTree(f"archive/entries/{entry_id}/agent", ARCHIVE_TREE_AGENT, "agents/coder"),
        ),
    )
    copy = world.payload(ref.entry_id, "agent")
    if payload == "partial-copy":
        # A move between volumes stopped while it copied; the source is whole.
        shutil.copytree(home, copy)
        shutil.rmtree(copy / "workspace")
    else:
        # The archive failed and its files could not go back; a new Agent then took the id.
        copy.parent.mkdir(parents=True)
        os.replace(home, copy)
        agents.create("coder", "Newcomer")

    world.service.recover()

    listings = (await world.service.list(ArchiveEntryFilter())).entries
    if payload == "partial-copy":
        assert agents.get("coder").name == "Coder"
        assert (listings, copy.exists()) == ((), False)
    else:
        # The old Agent's only copy stays, as a recovered entry under the same id.
        assert agents.get("coder").name == "Newcomer"
        [listing] = listings
        assert (listing.entry.entry_id, listing.entry.origin, listing.label) == (
            ref.entry_id,
            ARCHIVE_ORIGIN_RECOVERED,
            "Coder",
        )
        assert (copy / "agent.json").is_file()


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
@pytest.mark.parametrize("left", ["payload", "live-files", "invalid-live-files", "committed"])
async def test_an_interrupted_restore_finishes_from_live_files_and_never_moves_them_back(
    world: ArchiveWorld, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, left: str
) -> None:
    agents, ledger = world.agents, world.sessions.archive_ledger
    coder = agents.create("coder")
    agents.create("manager", tools={"subagent": {"allowed_agents": ["coder"]}})
    entry_id = (await world.service.archive_agent("coder")).entry_id
    payload, home = world.payload(entry_id, "agent"), world.data_dir / "agents" / "coder"
    if left == "committed":
        # Stopped after the commit, before the follow-up re-added the grants.
        monkeypatch.setattr(agents, "restore_delegation_grants", _fail)
        with pytest.raises(OSError, match="stopped here"):
            await world.service.restore(entry_id)
        monkeypatch.undo()
        assert world.entry(entry_id).state == "restored"
    else:
        # Stopped before the files moved, or after they did and before the commit.
        ledger.begin_restore(entry_id, {"target_id": None, "strip_channel_keys": False})
        if left != "payload":
            os.replace(payload, home)
        if left == "invalid-live-files":
            (home / "agent.json").write_text("{ not json", encoding="utf-8")

    world.service.recover()

    if left == "payload":
        assert world.entry(entry_id).state == "archived"
        assert not agents.exists("coder")
        assert (payload / "agent.json").is_file()
        return
    if left == "invalid-live-files":
        # The files may hold new data: they stay live, and the entry says why it is stuck.
        assert world.entry(entry_id).state == "restoring"
        assert (home / "agent.json").is_file() and not payload.exists()
        [busy] = (await world.service.show(entry_id)).restore.blockers
        assert (busy.code, busy.details["path"]) == ("entry_busy", str(home))
        assert "holds no valid Agent coder" in busy.message
        # Once the folder is moved away, the next start undoes the restore.
        shutil.move(home, tmp_path / "rescued")
        world.service.recover()
        assert world.entry(entry_id).state == "archived"
        assert [item.entry_id for item in (await world.service.purge([entry_id])).purged] == [
            entry_id
        ]
        return
    assert ledger.entry(entry_id) is None
    assert agents.get("coder").current_session_id == coder.current_session_id
    assert world.session_rows("coder") == [(coder.current_session_id, "live")]
    assert agents.get("manager").tools["subagent"]["allowed_agents"] == ["coder"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["commit-fails", "crash-in-its-folder", "crash-in-the-archive"])
async def test_a_moved_legacy_workspace_survives_a_failed_or_interrupted_restore(
    world: ArchiveWorld, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stop: str
) -> None:
    folder = (tmp_path / "repo").resolve()
    entry_id = legacy_agent_entry(world, folder)
    container = world.data_dir / "archive" / "coder"
    moved = container / "workspace"
    if stop == "commit-fails":
        monkeypatch.setattr(world.sessions.archive_ledger, "commit_restore", _fail)
        with pytest.raises(OSError, match="stopped here"):
            await world.service.restore(entry_id)
        monkeypatch.undo()
        # The failed restore returned the Workspace to the archive.
        assert (moved / "notes.md").is_file() and not folder.exists()
    else:
        # Stopped after the Workspace moved and before the Agent's files did.
        destination = folder if stop == "crash-in-its-folder" else container / "agent" / "workspace"
        world.sessions.archive_ledger.begin_restore(
            entry_id,
            {
                "target_id": None,
                "strip_channel_keys": False,
                "workspace_destination": str(destination),
            },
        )
        os.replace(moved, destination)

        world.service.recover()

        assert world.entry(entry_id).state == "archived"
        # The user's own folder stays where it is; a copy inside the archive goes back.
        kept = folder if stop == "crash-in-its-folder" else moved
        assert (kept / "notes.md").is_file()

    await world.service.restore(entry_id)

    assert world.agents.get("coder").workspace == str(folder)
    assert (folder / "notes.md").read_text(encoding="utf-8") == "mine"


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
    world: ArchiveWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.agents.create("coder", "Coder Agent")
    world.agents.create("locked", "Locked Agent")
    archive = world.data_dir / "archive"
    # A data snapshot restore can bring back a payload whose entry row it predates.
    with world.agents.archive_files("coder", archive / "entries" / "arc_orphan" / "agent"):
        pass
    with world.agents.archive_files("locked", archive / "entries" / "arc_locked" / "agent"):
        pass
    # An unreadable payload never stops the start; it is adopted once it can be read.
    _refuse_access(monkeypatch, archive / "entries" / "arc_locked")
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
    monkeypatch.undo()

    world.service.recover()

    assert world.entry("arc_locked").subject_id == "locked"
