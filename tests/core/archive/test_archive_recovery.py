"""Startup recovery: interrupted archive operations end consistent; orphan payloads are adopted."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from core.agents import AgentError
from core.sessions import (
    ARCHIVE_KIND_AGENT,
    ARCHIVE_KIND_FILES,
    ARCHIVE_KIND_PROJECT,
    ARCHIVE_ORIGIN_RECOVERED,
    ARCHIVE_TREE_AGENT,
    ARCHIVE_TREE_PROJECT,
    ArchiveEntryBusyError,
    ArchiveEntryFilter,
    ArchiveTree,
    SessionAddress,
)
from tests.core.archive.archive_test_support import (
    ArchiveWorld,
    agent_with_session,
    legacy_agent_entry,
)
from tests.core.archive.archive_test_support import world as world


def _fail(*_args: Any, **_kwargs: Any) -> Any:
    raise OSError("stopped here")


def test_interrupted_archives_are_rolled_back(world: ArchiveWorld, tmp_path: Path) -> None:
    agents, ledger = world.agents, world.sessions.archive_ledger
    coder = agent_with_session(world, "coder")
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
@pytest.mark.parametrize("payload", ["partial-copy", "only-copy", "config-only", "changed-file"])
async def test_an_interrupted_archive_beside_an_intact_source_never_loses_either(
    world: ArchiveWorld, payload: str
) -> None:
    agents, ledger = world.agents, world.sessions.archive_ledger
    agents.create("coder", "Coder")
    home = world.data_dir / "agents" / "coder"
    (home / "workspace" / "only.txt").write_text("irreplaceable", encoding="utf-8")
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
        if payload == "only-copy":
            agents.create("coder", "Newcomer")
        else:
            home.mkdir()
            shutil.copyfile(copy / "agent.json", home / "agent.json")
            if payload == "changed-file":
                shutil.copytree(copy / "workspace", home / "workspace")
                (home / "workspace" / "only.txt").write_text("new contents", encoding="utf-8")

    world.service.recover()

    listings = (await world.service.list(ArchiveEntryFilter())).entries
    if payload == "partial-copy":
        assert agents.get("coder").name == "Coder"
        assert (listings, copy.exists()) == ((), False)
    else:
        # The old Agent's only copy stays, as a recovered entry under the same id.
        assert agents.get("coder").name == ("Newcomer" if payload == "only-copy" else "Coder")
        [listing] = listings
        assert (listing.entry.entry_id, listing.entry.origin, listing.label) == (
            ref.entry_id,
            ARCHIVE_ORIGIN_RECOVERED,
            "Coder",
        )
        assert (copy / "agent.json").is_file()
        assert (copy / "workspace" / "only.txt").read_text(encoding="utf-8") == "irreplaceable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "planned", "missing_review_owner"),
    [
        ("agent", False, False),
        ("agent", True, False),
        ("project", False, False),
        ("agent", True, True),
        ("project", False, True),
    ],
    ids=["agent-old-plan", "agent-current-plan", "project", "agent-review", "project-review"],
)
async def test_restore_recovery_rewrites_a_moved_tree_under_its_new_id(
    world: ArchiveWorld, tmp_path: Path, kind: str, planned: bool, missing_review_owner: bool
) -> None:
    sessions, ledger = world.sessions, world.sessions.archive_ledger
    if kind == "agent":
        agent = agent_with_session(world, "original")
        session_id = agent.current_session_id
    else:
        world.projects.create("original", "Original", tmp_path / "repo")
        session_id = sessions.create("coder", project_id="original").id
    review = None
    if missing_review_owner:
        from core.runs import RunKind

        owner = agent_with_session(world, "review-owner")
        source = SessionAddress(None, owner.id, owner.current_session_id)
        review = (await sessions.fork(source, run_kind=RunKind.MEMORY_REFLECTION)).address
        await sessions.move(
            source,
            SessionAddress(
                None if kind == "agent" else "original",
                "original" if kind == "agent" else "coder",
                source.session_id,
            ),
        )
    entry_id = (
        (await world.service.archive_agent("original")).entry_id
        if kind == "agent"
        else (await world.service.archive_project("original")).entry_id
    )
    owner_entry = (
        await world.service.archive_agent("review-owner") if missing_review_owner else None
    )
    plan: dict[str, Any] = {"target_id": "restored", "strip_channel_keys": True}
    if planned and kind == "agent":
        plan.update(workspace=None, root_project_id=None)
    ledger.begin_restore(entry_id, plan)
    payload = world.payload(entry_id, kind)
    target = world.data_dir / f"{kind}s" / "restored"
    os.replace(payload, target)
    world.service.recover()
    if owner_entry is not None:
        assert world.entry(entry_id).state == "restoring"
        assert review is not None and not sessions.exists(review)
        assert target.exists() and not payload.exists()
        await world.service.restore(owner_entry.entry_id)
    world.service.recover()
    assert ledger.entry(entry_id) is None
    if kind == "agent":
        restored = world.agents.get("restored")
        assert restored.current_session_id == session_id
        assert restored.workspace == str(target / "workspace")
        assert sessions.exists(SessionAddress(None, "restored", session_id))
    else:
        assert world.projects.get("restored").project_id == "restored"
        assert sessions.exists(SessionAddress("restored", "coder", session_id))
    if review is not None:
        assert sessions.exists(review)


@pytest.mark.asyncio
@pytest.mark.parametrize("id_reused", [False, True], ids=["id-free", "id-reused"])
async def test_a_failed_agent_cleanup_keeps_the_archive_and_completes_at_the_next_start(
    world: ArchiveWorld, monkeypatch: pytest.MonkeyPatch, id_reused: bool
) -> None:
    coder = world.agents.create("coder")
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
        assert (allowed, entry.facts["grants"]) == (
            [],
            [
                {
                    "agent_id": "manager",
                    "agent_created_at": world.agents.get("manager").created_at,
                    "target_created_at": coder.created_at,
                    "index": 0,
                }
            ],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "left",
    [
        "payload",
        "live-files",
        "invalid-live-files",
        "invalid-planned-files",
        "committed",
        "unreadable-live-files",
        "unreadable-payload",
    ],
)
async def test_an_interrupted_restore_finishes_from_live_files_and_never_moves_them_back(
    world: ArchiveWorld,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    left: str,
    deny_access: Callable[[Path], None],
) -> None:
    agents, ledger = world.agents, world.sessions.archive_ledger
    coder = agent_with_session(world, "coder")
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
    elif left == "unreadable-live-files":
        # The actual restore moved the files; its commit and compensation stop.
        monkeypatch.setattr(ledger, "commit_restore", _fail)
        from core.agents import _archive

        move = _archive.move_tree

        def interrupted_move(source: Path, destination: Path) -> None:
            if source == home:
                _fail()
            move(source, destination)

        monkeypatch.setattr(_archive, "move_tree", interrupted_move)
        with pytest.raises(AgentError):
            await world.service.restore(entry_id)
        monkeypatch.undo()
    else:
        # Stopped before the files moved, or after they did and before the commit.
        restore_plan = {"target_id": None, "strip_channel_keys": False}
        if left == "invalid-planned-files":
            restore_plan.update(workspace=None, root_project_id=None)
        ledger.begin_restore(entry_id, restore_plan)
        if left not in {"payload", "unreadable-payload"}:
            os.replace(payload, home)
        if left in {"invalid-live-files", "invalid-planned-files"}:
            (home / "agent.json").write_text("{ not json", encoding="utf-8")

    plan = world.entry(entry_id).facts.get("restore_plan")
    if left == "unreadable-live-files":
        deny_access(home.parent)
    elif left == "unreadable-payload":
        deny_access(payload.parent)

    world.service.recover()

    if left.startswith("unreadable-"):
        assert world.entry(entry_id).state == "restoring"
        assert world.entry(entry_id).facts["restore_plan"] == plan
        with pytest.raises(ArchiveEntryBusyError):
            await world.service.purge([entry_id])
        assert world.session_rows("coder") == [(coder.current_session_id, "archived")]
        monkeypatch.undo()
        world.service.recover()
    if left in {"payload", "unreadable-payload"}:
        assert world.entry(entry_id).state == "archived"
        assert not agents.exists("coder")
        assert (payload / "agent.json").is_file()
        return
    if left in {"invalid-live-files", "invalid-planned-files"}:
        # The files may hold new data: they stay live, and the entry says why it is stuck.
        assert world.entry(entry_id).state == "restoring"
        assert (home / "agent.json").is_file() and not payload.exists()
        [busy] = (await world.service.show(entry_id)).restore.blockers
        assert (busy.code, busy.details["path"]) == ("entry_busy", str(home))
        # A restore or purge refused meanwhile says the same, not "retry".
        for refused_call in (
            lambda: world.service.restore(entry_id),
            lambda: world.service.purge([entry_id]),
        ):
            with pytest.raises(ArchiveEntryBusyError) as refused:
                await refused_call()
            assert (refused.value.reason, refused.value.details["path"]) == (
                busy.message,
                str(home),
            )
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
@pytest.mark.parametrize(
    "stop",
    ["commit-fails", "crash-in-its-folder", "crash-in-the-archive", "crash-after-agent-move"],
)
async def test_a_moved_legacy_workspace_survives_a_failed_or_interrupted_restore(
    world: ArchiveWorld, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stop: str
) -> None:
    folder = (tmp_path / "repo").resolve()
    entry_id = legacy_agent_entry(world, folder)
    container = world.data_dir / "archive" / "coder"
    moved = container / "workspace"
    if stop == "crash-after-agent-move":
        from core.agents import _archive

        folder.mkdir()
        (folder / "occupied.txt").write_text("keep", encoding="utf-8")
        home = world.data_dir / "agents" / "coder"
        move = _archive.move_tree

        def interrupted_move(source: Path, destination: Path) -> None:
            if source == home:
                _fail()
            move(source, destination)

        monkeypatch.setattr(_archive, "write_json_document", _fail)
        monkeypatch.setattr(_archive, "move_tree", interrupted_move)
        with pytest.raises(AgentError):
            await world.service.restore(entry_id)
        monkeypatch.undo()
        assert world.entry(entry_id).state == "restoring"
        assert (home / "workspace" / "notes.md").read_text(encoding="utf-8") == "mine"
        world.service.recover()
        world.service.recover()
        assert world.sessions.archive_ledger.entry(entry_id) is None
        assert world.agents.get("coder").workspace == world.agents.default_workspace("coder")
        assert (folder / "occupied.txt").read_text(encoding="utf-8") == "keep"
        return
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
    world: ArchiveWorld,
    monkeypatch: pytest.MonkeyPatch,
    deny_access: Callable[[Path], None],
    tmp_path: Path,
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
    deny_access(archive / "entries" / "arc_locked")
    (archive / "entries" / "arc_empty").mkdir()
    user_files = [archive / "entries" / "notes" / "keep.txt", archive / "mine" / "keep.txt"]
    for path in user_files:
        path.parent.mkdir(parents=True)
        path.write_text("mine", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "keep.txt"
    outside_file.write_text("mine", encoding="utf-8")
    shutil.copytree(archive / "entries" / "arc_orphan" / "agent", outside / "agent")
    linked = archive / "entries" / "arc_linked"
    links = [linked]
    for role in ("agent", "project"):
        payload_link = archive / "entries" / f"arc_{role}_link" / role
        payload_link.parent.mkdir()
        links.append(payload_link)
    for link in links:
        target = outside if link == linked else outside / "agent"
        if sys.platform == "win32":
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
        else:
            link.symlink_to(target, target_is_directory=True)

    world.service.recover()

    listings = {
        listing.entry.entry_id: listing
        for listing in (await world.service.list(ArchiveEntryFilter())).entries
    }
    assert set(listings) == {"arc_orphan", "arc_agent_link", "arc_project_link"}
    for entry_id in ("arc_agent_link", "arc_project_link"):
        assert listings[entry_id].entry.kind == ARCHIVE_KIND_FILES
        assert not listings[entry_id].restorable
    listing = listings["arc_orphan"]
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
    assert outside_file.read_text(encoding="utf-8") == "mine"
    assert all(link.is_symlink() or link.is_junction() for link in links)
    await world.service.restore("arc_orphan")
    assert world.agents.get("coder").name == "Coder Agent"
    monkeypatch.undo()

    world.service.recover()

    assert world.entry("arc_locked").subject_id == "locked"
    assert world.sessions.archive_ledger.entry("arc_linked") is None
