"""ArchiveService: archive, restore, purge and reads per kind, over the real stores."""

from __future__ import annotations

import errno
import json
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path

import pytest

from core.archive import (
    ArchiveEntryBusyError,
    ArchiveEntryNotFoundError,
    ArchiveNotRestorableError,
    ArchiveRestoreConflictError,
    ArchiveService,
    PendingPurge,
    _purge,
)
from core.chat import ChatSessionError
from core.sessions import ArchiveEntryFilter, ArchiveTree, SessionAddress
from tests.core.archive.archive_test_support import ArchiveWorld, legacy_agent_entry
from tests.core.archive.archive_test_support import world as world


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    return repo


@pytest.mark.asyncio
async def test_an_archived_agent_returns_with_its_sessions_grants_and_roster_position(
    world: ArchiveWorld,
) -> None:
    agents, sessions = world.agents, world.sessions
    agents.create("manager", "Manager", tools={"subagent": {"allowed_agents": ["x", "coder"]}})
    coder = agents.create("coder", "Coder")
    agents.create("beta", "Beta")
    second = sessions.create("coder", session_id="second").address
    agents.reorder(
        ["manager", "coder", "beta"], expected_revision=agents.list_with_order().order_revision
    )

    archived = await world.service.archive_agent("coder")

    entry = world.entry(archived.entry_id)
    assert (entry.kind, entry.subject_id, entry.session_count) == ("agent", "coder", 2)
    assert archived.policy_agent_ids == ("manager",)
    assert not agents.exists("coder")
    assert (world.payload(archived.entry_id, "agent") / "agent.json").is_file()
    assert not sessions.exists(second)
    assert agents.get("manager").tools["subagent"]["allowed_agents"] == ["x"]
    assert [agent.id for agent in agents.list_with_order().agents] == ["manager", "beta"]
    assert world.recall_removals == [(None, "coder", None)]
    assert world.changes

    restored = await world.service.restore(archived.entry_id)

    assert (restored.target_id, len(restored.addresses)) == ("coder", 2)
    assert agents.get("coder").current_session_id == coder.current_session_id
    assert sessions.exists(second)
    assert agents.get("manager").tools["subagent"]["allowed_agents"] == ["x", "coder"]
    assert [agent.id for agent in agents.list_with_order().agents] == ["manager", "coder", "beta"]
    assert sessions.archive_ledger.entry(archived.entry_id) is None
    assert not (world.data_dir / "archive" / "entries" / archived.entry_id).exists()


@pytest.mark.asyncio
async def test_a_repeated_archive_keeps_both_entries_and_restore_as_avoids_the_taken_id(
    world: ArchiveWorld,
) -> None:
    agents, sessions = world.agents, world.sessions
    agents.create("manager", "Manager")
    agents.create("coder", "First")
    sessions.create("coder", session_id="first-work")
    child = sessions.create("manager", session_id="child").address
    sessions.set_metadata(
        child, {"subagent_parent": {"agent_id": "coder", "session_id": "first-work"}}
    )
    first = await world.service.archive_agent("coder")
    agents.create("coder", "Second")
    second = await world.service.archive_agent("coder")

    # Archiving the same id again never replaces the earlier entry's files.
    assert first.entry_id != second.entry_id
    assert world.payload(first.entry_id, "agent").is_dir()
    assert world.payload(second.entry_id, "agent").is_dir()
    await world.service.restore(second.entry_id)
    with pytest.raises(ArchiveRestoreConflictError, match="restore it under another id") as taken:
        await world.service.restore(first.entry_id)
    assert [conflict.code for conflict in taken.value.conflicts] == ["agent_id_taken"]
    assert (await world.service.restore_check(first.entry_id, target_id="coder-old")).possible

    restored = await world.service.restore(first.entry_id, target_id="coder-old")

    assert agents.get("coder").name == "Second"
    assert agents.get("coder-old").name == "First"
    assert SessionAddress(None, "coder-old", "first-work") in restored.addresses
    # A Sub-Agent link follows its parent Session to the new id.
    assert sessions.get_metadata(child)["subagent_parent"]["agent_id"] == "coder-old"


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace_state", ["present", "gone"])
async def test_an_external_workspace_stays_in_place_and_returns_with_the_agent(
    world: ArchiveWorld, tmp_path: Path, workspace_state: str
) -> None:
    external = tmp_path / "repo"
    workspace = world.agents.create("coder", workspace=external).workspace
    (external / "notes.md").write_text("mine", encoding="utf-8")

    archived = await world.service.archive_agent("coder")

    assert (external / "notes.md").read_text(encoding="utf-8") == "mine"
    if workspace_state == "gone":
        shutil.rmtree(external)
    restored = await world.service.restore(archived.entry_id)
    agent = world.agents.get("coder")
    if workspace_state == "present":
        assert (agent.workspace, restored.warnings) == (workspace, ())
    else:  # the default Workspace replaces a vanished one
        assert [warning.code for warning in restored.warnings] == ["external_workspace_missing"]
        assert agent.workspace == world.agents.default_workspace("coder")
        assert Path(agent.workspace).is_dir()


@pytest.mark.asyncio
@pytest.mark.parametrize("in_use", ["nothing", "its-folder", "its-folder-and-the-default"])
async def test_a_moved_legacy_workspace_returns_to_its_folder_or_the_default_one(
    world: ArchiveWorld, tmp_path: Path, in_use: str
) -> None:
    folder = (tmp_path / "repo").resolve()
    entry_id = legacy_agent_entry(world, folder)
    moved = world.data_dir / "archive" / "coder" / "workspace"
    # The moved folder is the user's own, which the entry names.
    assert (await world.service.show(entry_id)).user_folders == (
        ArchiveTree("archive/coder/workspace", "workspace", str(folder)),
    )
    if in_use != "nothing":
        folder.mkdir()
        (folder / "other.txt").write_text("other", encoding="utf-8")
    if in_use == "its-folder-and-the-default":
        (world.data_dir / "archive" / "coder" / "agent" / "workspace").mkdir(exist_ok=True)
        with pytest.raises(ArchiveNotRestorableError) as refused:
            await world.service.restore(entry_id)
        [blocker] = refused.value.blockers
        assert blocker.code == "workspace_path_taken"
        assert f"move or rename {folder}, then restore again" in blocker.message
        assert world.entry(entry_id).state == "archived"
        assert (moved / "notes.md").is_file()
        return

    restored = await world.service.restore(entry_id)

    workspace = Path(world.agents.get("coder").workspace)
    if in_use == "nothing":
        assert (workspace, restored.warnings) == (folder, ())
    else:  # the user's folder stays as it is; the archived Workspace becomes the default one
        assert [warning.code for warning in restored.warnings] == ["workspace_path_taken"]
        assert str(workspace) == world.agents.default_workspace("coder")
        assert (folder / "other.txt").is_file()
    assert (workspace / "notes.md").read_text(encoding="utf-8") == "mine"
    assert not moved.exists()


@pytest.mark.asyncio
async def test_an_archived_project_returns_and_roots_its_agents_again(
    world: ArchiveWorld, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    world.projects.create("vbot", "vBot", repo)
    workspace = world.agents.create("coder", workspace=tmp_path / "identity-home").workspace
    world.agents.update("coder", root_project_id="vbot")
    build = world.sessions.create("builder", session_id="build", project_id="vbot").address

    archived = await world.service.archive_project("vbot")

    assert archived.affected_agent_ids == ("coder",)
    entry = world.entry(archived.entry_id)
    assert (entry.kind, entry.subject_id, entry.session_count) == ("project", "vbot", 1)
    assert not world.projects.exists("vbot")
    assert not world.sessions.exists(build)
    unrooted = world.agents.get("coder")
    assert unrooted.root_project_id is None
    assert unrooted.workspace == world.agents.default_workspace("coder")
    assert repo.is_dir()

    await world.service.restore(archived.entry_id)

    assert world.projects.get("vbot").display_name == "vBot"
    assert world.sessions.exists(build)
    rooted = world.agents.get("coder")
    assert (rooted.root_project_id, rooted.workspace) == ("vbot", workspace)


@pytest.mark.asyncio
async def test_a_failed_project_archive_leaves_the_project_its_agents_and_no_entry(
    world: ArchiveWorld, tmp_path: Path
) -> None:
    world.projects.create("vbot", "vBot", _repo(tmp_path))
    workspace = world.agents.create("coder", workspace=tmp_path / "identity-home").workspace
    world.agents.update("coder", root_project_id="vbot")
    # Sessions an Extension manages keep their Project until the Extension releases them.
    world.sessions.create_bound_temporary_session(
        SessionAddress("vbot", "tmp_participant", "ses_participant"),
        owner_name="swarm",
        group_id="swr_group",
        participant_id="prt_peer",
        config={},
    )

    with pytest.raises(ChatSessionError, match=r"managed by an Extension \(swarm\)"):
        await world.service.archive_project("vbot")

    assert world.projects.get("vbot").display_name == "vBot"
    coder = world.agents.get("coder")
    assert (coder.root_project_id, coder.workspace) == ("vbot", workspace)
    assert not world.sessions.archive_ledger.page(ArchiveEntryFilter()).entries
    entries_dir = world.data_dir / "archive" / "entries"
    assert not entries_dir.exists() or not any(entries_dir.iterdir())


@pytest.mark.asyncio
async def test_a_session_archive_moves_the_current_pointer_and_restore_as_avoids_a_taken_address(
    world: ArchiveWorld,
) -> None:
    coder = world.agents.create("coder")
    current = SessionAddress(None, "coder", coder.current_session_id)
    world.sessions.create("coder", session_id="other")
    world.sessions.set_title(current, "Planning")

    archived = await world.service.archive_session(current)

    assert (archived.was_current, archived.next_session_id) == (True, "other")
    assert world.agents.get("coder").current_session_id == "other"
    assert not world.sessions.exists(current)
    assert world.recall_removals == [(None, "coder", current.session_id)]
    # Channel routing started a new Session at the archived address meanwhile.
    world.sessions.create("coder", session_id=current.session_id)
    with pytest.raises(ArchiveRestoreConflictError) as taken:
        await world.service.restore(archived.entry_id)
    assert [conflict.code for conflict in taken.value.conflicts] == ["session_address_taken"]

    restored = await world.service.restore(archived.entry_id, target_id="planning-old")

    assert restored.addresses == (SessionAddress(None, "coder", "planning-old"),)
    assert world.sessions.get_metadata(restored.addresses[0])["title"] == "Planning"


async def _newer_agent_payload(world: ArchiveWorld, tmp_path: Path) -> tuple[str, str | None]:
    world.agents.create("coder")
    entry_id = (await world.service.archive_agent("coder")).entry_id
    document = world.payload(entry_id, "agent") / "agent.json"
    data = json.loads(document.read_text(encoding="utf-8"))
    document.write_text(json.dumps({**data, "format_version": 99}), encoding="utf-8")
    return entry_id, None


async def _invalid_agent_target(world: ArchiveWorld, tmp_path: Path) -> tuple[str, str | None]:
    world.agents.create("coder")
    return (await world.service.archive_agent("coder")).entry_id, "Not An Id"


async def _claimed_project_repo(world: ArchiveWorld, tmp_path: Path) -> tuple[str, str | None]:
    repo = _repo(tmp_path)
    world.projects.create("vbot", "vBot", repo)
    entry_id = (await world.service.archive_project("vbot")).entry_id
    world.projects.create("other", "Other", repo)
    # Another id avoids no claimed repo.
    return entry_id, "vbot-old"


async def _archived_agent_scope(world: ArchiveWorld, tmp_path: Path) -> tuple[str, str | None]:
    world.agents.create("coder")
    notes = world.sessions.create("coder", session_id="notes").address
    entry_id = (await world.service.archive_session(notes)).entry_id
    await world.service.archive_agent("coder")
    return entry_id, None


async def _project_agent_outside_the_team(
    world: ArchiveWorld, tmp_path: Path
) -> tuple[str, str | None]:
    world.projects.create("vbot", "vBot", _repo(tmp_path))
    notes = world.sessions.create("builder", session_id="notes", project_id="vbot").address
    return (await world.service.archive_session(notes)).entry_id, None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arrange", "blocker"),
    [
        pytest.param(_newer_agent_payload, "newer_format", id="newer-format"),
        pytest.param(_invalid_agent_target, "invalid_target_id", id="invalid-target"),
        pytest.param(_claimed_project_repo, "project_cwd_claimed", id="claimed-repo"),
        pytest.param(_archived_agent_scope, "scope_missing", id="agent-archived"),
        pytest.param(_project_agent_outside_the_team, "scope_missing", id="not-in-team"),
    ],
)
async def test_a_blocked_restore_names_the_blocker_and_changes_nothing(
    world: ArchiveWorld,
    tmp_path: Path,
    arrange: Callable[[ArchiveWorld, Path], Awaitable[tuple[str, str | None]]],
    blocker: str,
) -> None:
    entry_id, target_id = await arrange(world, tmp_path)

    check = await world.service.restore_check(entry_id, target_id=target_id)
    with pytest.raises(ArchiveNotRestorableError) as refused:
        await world.service.restore(entry_id, target_id=target_id)

    assert [problem.code for problem in check.blockers] == [blocker]
    assert [problem.code for problem in refused.value.blockers] == [blocker]
    assert world.entry(entry_id).state == "archived"


@pytest.mark.asyncio
async def test_a_session_scope_blocker_names_the_entry_that_restores_its_agent(
    world: ArchiveWorld, tmp_path: Path
) -> None:
    entry_id, _target = await _archived_agent_scope(world, tmp_path)
    [agent_entry] = (await world.service.list(ArchiveEntryFilter(kind="agent"))).entries

    [scope] = (await world.service.restore_check(entry_id)).blockers

    assert scope.details == {"agent_id": "coder", "entry_id": agent_entry.entry.entry_id}


@pytest.mark.asyncio
async def test_purge_deletes_the_sessions_payload_and_entry_after_importing_usage(
    world: ArchiveWorld,
) -> None:
    coder = world.agents.create("coder")
    archived = await world.service.archive_agent("coder")
    assert world.session_rows("coder") == [(coder.current_session_id, "archived")]

    outcome = await world.service.purge([archived.entry_id])

    assert [(item.entry_id, item.session_count) for item in outcome.purged] == [
        (archived.entry_id, 1)
    ]
    assert outcome.pending == ()
    # Recorded usage reaches the usage ledger before its Sessions go.
    assert world.usage_imports == [None]
    assert world.session_rows("coder") == []
    assert world.sessions.archive_ledger.entry(archived.entry_id) is None
    assert not (world.data_dir / "archive" / "entries").exists()


@pytest.mark.asyncio
async def test_a_purge_that_stops_partway_stays_purging_and_continues_later(
    world: ArchiveWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.agents.create("coder")
    entry_id = (await world.service.archive_agent("coder")).entry_id

    def refuse(_path: Path, *, within: Path) -> None:
        raise PermissionError(errno.EACCES, "held open by another program")

    monkeypatch.setattr(_purge, "remove_tree", refuse)
    stopped = await world.service.purge([entry_id])
    monkeypatch.undo()

    assert stopped.pending == (PendingPurge(entry_id, "PermissionError"),)
    assert world.entry(entry_id).state == "purging"
    assert world.session_rows("coder") == []
    assert world.payload(entry_id, "agent").is_dir()

    resumed = await world.service.purge([entry_id])

    assert ([item.entry_id for item in resumed.purged], resumed.pending) == ([entry_id], ())
    assert world.sessions.archive_ledger.entry(entry_id) is None
    assert not world.payload(entry_id, "agent").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["unknown-entry", "busy-entry", "usage-import-fails"])
async def test_a_refused_purge_deletes_nothing(world: ArchiveWorld, refusal: str) -> None:
    coder = world.agents.create("coder")
    archived = await world.service.archive_agent("coder")

    if refusal == "unknown-entry":
        with pytest.raises(ArchiveEntryNotFoundError, match="arc_missing"):
            await world.service.purge([archived.entry_id, "arc_missing"])
    elif refusal == "busy-entry":
        world.sessions.archive_ledger.begin_restore(archived.entry_id, {"target_id": None})
        with pytest.raises(ArchiveEntryBusyError, match="restoring"):
            await world.service.purge([archived.entry_id])
    else:

        def fail() -> None:
            raise OSError("usage ledger unavailable")

        service = ArchiveService(replace(world.services, import_usage=fail))
        outcome = await service.purge([archived.entry_id])
        assert outcome.pending == (PendingPurge(archived.entry_id, "usage_import_failed"),)

    assert world.session_rows("coder") == [(coder.current_session_id, "archived")]
    assert world.payload(archived.entry_id, "agent").is_dir()


@pytest.mark.asyncio
async def test_entries_list_with_labels_and_show_their_sessions_files_and_restorability(
    world: ArchiveWorld,
) -> None:
    world.agents.create("manager")
    coder = world.agents.create("coder", "Coder Agent")
    notes = world.sessions.create("manager", session_id="notes").address
    world.sessions.set_title(notes, "Notes")
    agent_entry = await world.service.archive_agent("coder")
    session_entry = await world.service.archive_session(notes)

    page = await world.service.list(ArchiveEntryFilter())
    shutil.rmtree(world.payload(agent_entry.entry_id, "agent"))
    agent_detail = await world.service.show(agent_entry.entry_id)
    session_detail = await world.service.show(session_entry.entry_id)

    assert {item.entry.entry_id: (item.label, item.restorable) for item in page.entries} == {
        agent_entry.entry_id: ("Coder Agent", True),
        session_entry.entry_id: ("Notes", True),
    }
    assert [member.address.session_id for member in agent_detail.sessions] == [
        coder.current_session_id
    ]
    assert agent_detail.files == "missing"
    assert agent_detail.listing.not_restorable_reason == "payload_missing"
    assert [problem.code for problem in agent_detail.restore.blockers] == ["payload_missing"]
    assert (session_detail.files, session_detail.restore.possible) == ("none", True)
    assert agent_detail.user_folders == session_detail.user_folders == ()
