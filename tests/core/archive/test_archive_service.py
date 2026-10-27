"""ArchiveService: archive, restore, purge and reads per kind, over the real stores."""

from __future__ import annotations

import errno
import json
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.agents import AgentReferencedError
from core.archive import (
    AgentArchiveOutcome,
    ArchiveEntryBusyError,
    ArchiveEntryNotFoundError,
    ArchiveNotRestorableError,
    ArchiveRestoreConflictError,
    ArchiveService,
    KeptEntry,
    PendingPurge,
    ProjectArchiveOutcome,
    SkippedPurge,
    _purge,
    convert_archived_projects,
)
from core.chat import ChatMessage, ChatSessionError
from core.projects import ProjectError
from core.runs import RunAdmissionBlockedError, RunKind
from core.sessions import (
    ARCHIVE_KIND_FILES,
    ARCHIVE_TREE_FILES,
    SESSION_WORKING_PROJECT_META_KEY,
    ArchiveEntry,
    ArchiveEntryFilter,
    ArchiveScope,
    ArchiveTree,
    SessionAddress,
)
from core.utils.timestamps import format_canonical_timestamp, utc_now_timestamp
from tests.core.archive.archive_test_support import (
    ArchiveWorld,
    agent_with_session,
    legacy_agent_entry,
)
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
    coder = agent_with_session(world, "coder", "Coder")
    agents.create("beta", "Beta")
    second = sessions.create("coder", session_id="second").address
    agents.reorder(
        ["manager", "coder", "beta"], expected_revision=agents.list_with_order().order_revision
    )
    first_scratch = await world.scratch_areas.folder_for(
        SessionAddress(None, "coder", coder.current_session_id)
    )
    second_scratch = await world.scratch_areas.folder_for(second)

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
    assert set(world.closed_transports) == {
        SessionAddress(None, "coder", coder.current_session_id),
        second,
    }
    assert world.changes
    # The commands of its Sessions were stopped without delivery first.
    assert world.commands.closed == [("agent", "coder", None)]
    assert not first_scratch.area.exists() and not second_scratch.area.exists()

    restored = await world.service.restore(archived.entry_id)

    assert (restored.target_id, len(restored.addresses)) == ("coder", 2)
    assert restored.grant_agent_ids == ("manager",)
    assert agents.get("coder").current_session_id == coder.current_session_id
    assert sessions.exists(second)
    assert agents.get("manager").tools["subagent"]["allowed_agents"] == ["x", "coder"]
    assert [agent.id for agent in agents.list_with_order().agents] == ["manager", "coder", "beta"]
    assert sessions.archive_ledger.entry(archived.entry_id) is None
    assert not (world.data_dir / "archive" / "entries" / archived.entry_id).exists()
    assert not first_scratch.area.exists() and not second_scratch.area.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("archived_child", [False, True])
@pytest.mark.parametrize("proven_parent", [False, True], ids=["unproven-legacy", "proven"])
async def test_a_repeated_archive_keeps_both_entries_and_restore_as_avoids_the_taken_id(
    world: ArchiveWorld,
    archived_child: bool,
    proven_parent: bool,
) -> None:
    agents, sessions = world.agents, world.sessions
    agents.create("manager", "Manager")
    agents.create("coder", "First")
    sessions.create("coder", session_id="first-work")
    child = sessions.create("manager", session_id="child").address
    sessions.set_metadata(
        child, {"subagent_parent": {"agent_id": "coder", "session_id": "first-work"}}
    )
    child_entry = await world.service.archive_session(child) if archived_child else None
    first = await world.service.archive_agent("coder")
    if not proven_parent:
        # An upgraded archive with ambiguous original/replacement lineage must
        # report the partial recovery, never infer identity from history time.
        sessions.database.write(
            lambda connection: connection.execute(
                "UPDATE sessions SET subagent_parent_generation_id = NULL "
                "WHERE agent_id = 'manager' AND session_id = 'child'"
            )
        )
    agents.create("coder", "Second")
    # Public ids are caller-selected: a replacement can reuse the exact Parent
    # address, not merely its Agent id.
    replacement_parent = sessions.create("coder", session_id="first-work").address
    replacement_child = sessions.create("manager", session_id="replacement-child").address
    sessions.set_metadata(
        replacement_child, {"subagent_parent": {"agent_id": "coder", "session_id": "first-work"}}
    )
    if not archived_child:
        # An unrelated metadata edit must not rebind the original link to the
        # new Parent now at that address.
        sessions.mutate_metadata(child, lambda metadata: metadata.update(title="Original child"))
    second = await world.service.archive_agent("coder")

    # Archiving the same id again never replaces the earlier entry's files.
    assert first.entry_id != second.entry_id
    assert world.payload(first.entry_id, "agent").is_dir()
    assert world.payload(second.entry_id, "agent").is_dir()
    await world.service.restore(second.entry_id)
    with pytest.raises(ArchiveRestoreConflictError, match="restore it under another id") as taken:
        await world.service.restore(first.entry_id)
    assert [conflict.code for conflict in taken.value.conflicts] == ["agent_id_taken"]
    check = await world.service.restore_check(first.entry_id, target_id="coder-old")
    assert check.possible

    restored = await world.service.restore(first.entry_id, target_id="coder-old")

    assert agents.get("coder").name == "Second"
    assert agents.get("coder-old").name == "First"
    assert SessionAddress(None, "coder-old", "first-work") in restored.addresses
    # A Sub-Agent link follows its parent even when the child is restored later.
    if child_entry is not None:
        await world.service.restore(child_entry.entry_id)
    assert sessions.subagent_children(SessionAddress(None, "coder-old", "first-work")) == (
        [child] if proven_parent else []
    )
    assert sessions.get_metadata(child)["subagent_parent"]["agent_id"] == (
        "coder-old" if proven_parent else "coder"
    )
    assert sessions.subagent_children(replacement_parent) == (
        [replacement_child] if proven_parent else [child, replacement_child]
    )
    expected_warnings = [] if proven_parent else ["subagent_parent_unproven"]
    assert [warning.code for warning in check.warnings] == expected_warnings
    assert [warning.code for warning in restored.warnings] == expected_warnings


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("holder_state", "warning_code"),
    [
        ("legacy", "grant_holder_unproven"),
        ("invalid", "grant_holder_unproven"),
        ("replaced", "grant_holder_replaced"),
        ("missing", "grant_target_missing"),
    ],
)
async def test_agent_restore_reports_grants_it_cannot_safely_reinstate(
    world: ArchiveWorld, holder_state: str, warning_code: str
) -> None:
    agents, ledger = world.agents, world.sessions.archive_ledger
    agents.create("coder")
    agents.create("manager", tools={"subagent": {"allowed_agents": ["coder"]}})
    archived = await world.service.archive_agent("coder")
    if holder_state in ("legacy", "invalid"):
        [grant] = world.entry(archived.entry_id).facts["grants"]
        grant.pop("agent_created_at")
        if holder_state == "invalid":
            grant["agent_created_at"] = "not-a-timestamp"
        ledger.update_facts(world.entry(archived.entry_id).entry_key, {"grants": [grant]})
    else:
        await world.service.archive_agent("manager")
        if holder_state == "replaced":
            agents.create("manager", tools={"subagent": {"allowed_agents": []}})

    check = await world.service.restore_check(archived.entry_id, target_id="coder-old")
    restored = await world.service.restore(archived.entry_id, target_id="coder-old")

    assert check.possible
    assert [warning.code for warning in check.warnings] == [warning_code]
    assert [warning.code for warning in restored.warnings] == [warning_code]
    assert restored.grant_agent_ids == ()
    if holder_state != "missing":
        assert agents.get("manager").tools["subagent"]["allowed_agents"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("location", "workspace_state"),
    [
        (location, state)
        for location in ("outside-data-dir", "in-data-dir")
        for state in ("present", "gone", "unavailable")
    ],
)
async def test_an_external_workspace_stays_in_place_and_returns_with_the_agent(
    world: ArchiveWorld,
    tmp_path: Path,
    location: str,
    workspace_state: str,
    deny_access: Callable[[Path], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Outside the Agent's own directory, even inside the data directory.
    external = tmp_path / "repo" if location == "outside-data-dir" else world.data_dir / "shared"
    workspace = world.agents.create("coder", workspace=external).workspace
    (external / "notes.md").write_text("mine", encoding="utf-8")

    archived = await world.service.archive_agent("coder")

    assert (external / "notes.md").read_text(encoding="utf-8") == "mine"
    # Named by its absolute path.
    assert archived.external_workspace == workspace
    shown = await world.service.show(archived.entry_id)
    assert Path(shown.external_workspace or "") == Path(workspace)
    if workspace_state == "gone":
        shutil.rmtree(external)
    elif workspace_state == "unavailable":
        deny_access(external)
    if workspace_state != "present":
        check = await world.service.restore_check(archived.entry_id)
        assert [blocker.code for blocker in check.blockers] == [
            "external_workspace_missing"
            if workspace_state == "gone"
            else "external_workspace_unavailable"
        ]
        with pytest.raises(ArchiveNotRestorableError):
            await world.service.restore(archived.entry_id)
        assert not world.agents.exists("coder")
        entry = world.sessions.archive_ledger.entry(archived.entry_id)
        assert entry is not None and entry.state == "archived"
        if workspace_state == "gone":
            external.mkdir()
        else:
            monkeypatch.undo()
    restored = await world.service.restore(archived.entry_id)
    agent = world.agents.get("coder")
    assert (agent.workspace, restored.warnings) == (workspace, ())


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
@pytest.mark.parametrize("target_id", [None, "vbot-2"], ids=["same-id", "new-id"])
@pytest.mark.parametrize("replace_agent", [False, True])
async def test_an_archived_project_returns_and_roots_its_agents_again(
    world: ArchiveWorld, tmp_path: Path, target_id: str | None, replace_agent: bool
) -> None:
    repo = _repo(tmp_path)
    world.projects.create("vbot", "vBot", repo)
    workspace = world.agents.create("coder", workspace=tmp_path / "identity-home").workspace
    world.agents.update("coder", root_project_id="vbot")
    build = world.sessions.create("builder", session_id="build", project_id="vbot").address
    notes = world.sessions.create("coder", session_id="notes", working_project_id="vbot").address
    build_scratch = await world.scratch_areas.folder_for(build)
    notes_scratch = await world.scratch_areas.folder_for(notes)

    archived = await world.service.archive_project("vbot")

    assert archived.affected_agent_ids == ("coder",)
    assert world.commands.closed == [("project", "vbot")]
    entry = world.entry(archived.entry_id)
    assert (entry.kind, entry.subject_id, entry.session_count) == ("project", "vbot", 1)
    assert not world.projects.exists("vbot")
    assert not world.sessions.exists(build)
    unrooted = world.agents.get("coder")
    assert unrooted.root_project_id is None
    assert unrooted.workspace == world.agents.default_workspace("coder")
    assert repo.is_dir()
    # An Identity Session stays where it works; it cannot run until the Project returns.
    assert world.sessions.metadata_value(notes, SESSION_WORKING_PROJECT_META_KEY) == "vbot"
    assert not build_scratch.area.exists() and notes_scratch.area.exists()
    assert world.closed_transports == [build]

    if replace_agent:
        await world.service.archive_agent("coder")
        replacement = world.agents.create("coder")
    await world.service.restore(archived.entry_id, target_id=target_id)

    project_id = target_id or "vbot"
    assert world.projects.get(project_id).display_name == "vBot"
    assert world.sessions.exists(replace(build, project_id=project_id))
    rooted = world.agents.get("coder")
    assert (rooted.root_project_id, rooted.workspace) == (
        (None, replacement.workspace) if replace_agent else (project_id, workspace)
    )
    # Its Sessions work in the Project again, under its new id too.
    if not replace_agent:
        assert world.sessions.metadata_value(notes, SESSION_WORKING_PROJECT_META_KEY) == project_id


@pytest.mark.asyncio
async def test_a_project_archived_in_format_1_returns_once_converted(
    world: ArchiveWorld, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    (repo / "AGENTS.md").write_text("a", encoding="utf-8")
    (repo / "CLAUDE.md").write_text("c", encoding="utf-8")
    world.projects.create("vbot", "vBot", repo)
    archived = await world.service.archive_project("vbot")
    # As a vBot before format version 2 archived it: its CLAUDE.md Source on.
    document = world.payload(archived.entry_id, "project") / "project.json"
    stored = json.loads(document.read_text(encoding="utf-8"))
    stored.update(
        format_version=1,
        auto_load=["AGENTS.md"],
        sources=[{"id": "claude.instructions", "enabled": True}],
        created_at="2026-10-01T10:00:00.000000Z",
    )
    document.write_text(json.dumps(stored), encoding="utf-8")
    world.sessions.archive_ledger.update_facts(
        world.entry(archived.entry_id).entry_key, {"payload_format": 1}
    )
    page = await world.service.list(ArchiveEntryFilter())
    assert [item.restorable for item in page.entries] == [False]

    assert convert_archived_projects(world.data_dir, world.sessions.archive_ledger) == (
        archived.entry_id,
    )
    assert convert_archived_projects(world.data_dir, world.sessions.archive_ledger) == ()
    await world.service.restore(archived.entry_id)

    restored = world.projects.get("vbot")
    assert restored.auto_load == ["AGENTS.md", "CLAUDE.md"]
    assert restored.instruction_files == ["AGENTS.md", "CLAUDE.md"]


@pytest.mark.asyncio
@pytest.mark.parametrize("target_id", [None, "vbot-2"], ids=["same-id", "new-id"])
async def test_a_project_archive_without_association_provenance_never_guesses_ownership(
    world: ArchiveWorld, tmp_path: Path, target_id: str | None
) -> None:
    world.projects.create("vbot", "vBot", _repo(tmp_path))
    world.agents.create("coder")
    notes = world.sessions.create("coder", session_id="notes", working_project_id="vbot").address
    archived = await world.service.archive_project("vbot")

    # A current Generation 1 archive written before provenance was available.
    def remove_provenance(connection) -> None:
        connection.execute("DELETE FROM session_working_project_archives")
        connection.execute(
            "UPDATE archive_entries SET facts_json = "
            "json_remove(facts_json, '$.working_project_provenance')"
        )

    world.sessions.database.write(remove_provenance)

    check = await world.service.restore_check(archived.entry_id, target_id=target_id)
    restored = await world.service.restore(archived.entry_id, target_id=target_id)

    assert world.projects.exists(target_id or "vbot")
    assert world.sessions.metadata_value(notes, SESSION_WORKING_PROJECT_META_KEY) == "vbot"
    expected = [] if target_id is None else ["working_project_association_unproven"]
    assert [warning.code for warning in check.warnings] == expected
    assert [warning.code for warning in restored.warnings] == expected


@pytest.mark.asyncio
async def test_project_restore_keeps_a_later_fork_in_the_replacement_repository(
    world: ArchiveWorld, tmp_path: Path
) -> None:
    sessions = world.sessions
    original_repo, replacement_repo = _repo(tmp_path, "original"), _repo(tmp_path, "replacement")
    world.projects.create("app", "Original", original_repo)
    world.agents.create("coder")
    source = sessions.create("coder", session_id="old-history", working_project_id=None)
    original = sessions.create("coder", session_id="original", working_project_id="app").address
    archived = await world.service.archive_project("app")
    world.projects.create("app", "Replacement", replacement_repo)
    in_project = await sessions.fork(source.address, target_project_id="app")
    replacement = (await sessions.fork(in_project.address, target_project_id=None)).address

    await world.service.restore(archived.entry_id, target_id="app-old")

    def working_directory(address: SessionAddress) -> Path:
        project_id = sessions.metadata_value(address, SESSION_WORKING_PROJECT_META_KEY)
        return Path(world.projects.get(project_id).cwd)

    assert working_directory(original) == original_repo.resolve()
    assert working_directory(replacement) == replacement_repo.resolve()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload_state", ["returned", "unavailable"])
async def test_a_failed_project_archive_preserves_the_project_and_its_agents(
    world: ArchiveWorld,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    deny_access: Callable[[Path], None],
    payload_state: str,
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
    scratch = await world.scratch_areas.folder_for(
        SessionAddress("vbot", "tmp_participant", "ses_participant")
    )

    ledger = world.sessions.archive_ledger
    if payload_state == "unavailable":
        commit_scope = ledger.commit_scope

        def refuse_return(source: Path, _destination: Path) -> None:
            raise PermissionError(errno.EACCES, "Cannot return archive payload", str(source))

        def deny_payload_before_commit(
            entry_key: int, scope: ArchiveScope, *, cleanup_pending: bool = False
        ) -> int:
            entry = ledger.entry_by_key(entry_key)
            assert entry is not None
            deny_access((world.data_dir / entry.trees[0].path).parent)
            # The fixture denies reads only; the rollback rename fails too.
            monkeypatch.setattr("core.projects.store.move_tree", refuse_return)
            return commit_scope(entry_key, scope, cleanup_pending=cleanup_pending)

        monkeypatch.setattr(ledger, "commit_scope", deny_payload_before_commit)

    expected_error = ProjectError if payload_state == "unavailable" else ChatSessionError
    with pytest.raises(expected_error) as failure:
        await world.service.archive_project("vbot")

    if payload_state == "unavailable":
        # Compensation could not return the payload. Its recovery provenance
        # must survive the failed presence check, as must the original failure.
        assert isinstance(failure.value.__cause__, PermissionError)
        [pending] = ledger.unsettled()
        assert pending.state == "archiving"
        assert pending.trees[0].source_path == "projects/vbot"
        monkeypatch.undo()
        assert (world.data_dir / pending.trees[0].path).is_dir()
        world.service.recover()

    assert world.projects.get("vbot").display_name == "vBot"
    coder = world.agents.get("coder")
    assert (coder.root_project_id, coder.workspace) == ("vbot", workspace)
    assert not world.sessions.archive_ledger.page(ArchiveEntryFilter()).entries
    entries_dir = world.data_dir / "archive" / "entries"
    assert not entries_dir.exists() or not any(entries_dir.iterdir())
    assert scratch.area.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("current", ["session", "review-fork", "moved-review-fork"])
async def test_a_session_archive_takes_its_review_forks_and_moves_the_current_pointer(
    world: ArchiveWorld, current: str
) -> None:
    coder = agent_with_session(world, "coder")
    session = SessionAddress(None, "coder", coder.current_session_id)
    world.sessions.create("coder", session_id="other")
    world.sessions.set_title(session, "Planning")
    review = (await world.sessions.fork(session, run_kind=RunKind.MEMORY_REFLECTION)).address
    scratch = await world.scratch_areas.folder_for(session)
    review_scratch = await world.scratch_areas.folder_for(review)
    if current.endswith("review-fork"):
        world.agents.update("coder", current_session_id=review.session_id)
    if current == "moved-review-fork":
        agent_with_session(world, "other")
        target = SessionAddress(None, "other", session.session_id)
        await world.sessions.move(session, target)
        session = target
    # A review still in flight keeps its Session from leaving.
    async with world.runs.session_admission_guard(review):
        with pytest.raises(RunAdmissionBlockedError):
            await world.service.archive_session(session)

    archived = await world.service.archive_session(session)

    assert (archived.was_current, archived.next_session_id) == (
        True,
        "other-first" if current == "moved-review-fork" else "other",
    )
    assert archived.addresses == (session, review)
    assert world.agents.get("coder").current_session_id == "other"
    assert not world.sessions.exists(session) and not world.sessions.exists(review)
    assert world.commands.closed == [("session", session), ("session", review)]
    assert world.recall_removals == [(None, session.agent_id, session.session_id)]
    assert world.closed_transports == [session, review]
    assert not scratch.area.exists() and not review_scratch.area.exists()
    assert (await world.service.show(archived.entry_id)).listing.accepts_target_id
    # Channel routing started a new Session at the archived address meanwhile.
    world.sessions.create(session.agent_id, session_id=session.session_id)
    with pytest.raises(ArchiveRestoreConflictError) as taken:
        await world.service.restore(archived.entry_id)
    assert [conflict.code for conflict in taken.value.conflicts] == ["session_address_taken"]

    if current == "moved-review-fork":
        owner_archive = await world.service.archive_agent("coder")
        check = await world.service.restore_check(archived.entry_id, target_id="planning-old")
        assert [blocker.code for blocker in check.blockers] == ["scope_missing"]
        with pytest.raises(ArchiveNotRestorableError):
            await world.service.restore(archived.entry_id, target_id="planning-old")
        assert not world.sessions.exists(review)
        await world.service.restore(owner_archive.entry_id)

    restored = await world.service.restore(archived.entry_id, target_id="planning-old")

    # The new id names the Session; its review fork keeps its own and follows it again.
    renamed = SessionAddress(None, session.agent_id, "planning-old")
    assert restored.addresses == (renamed, review)
    assert world.sessions.get_metadata(renamed)["title"] == "Planning"
    assert world.sessions.review_forks(renamed) == (review,)


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_kind", ["agent", "project"])
@pytest.mark.parametrize("review_project", [None, "original"])
async def test_scope_archive_carries_moved_reviews_with_their_original_owners(
    world: ArchiveWorld, tmp_path: Path, scope_kind: str, review_project: str | None
) -> None:
    sessions = world.sessions
    if review_project is None:
        agent = agent_with_session(world, "original")
        source = SessionAddress(None, "original", agent.current_session_id)
        sessions.create("original", session_id="landing")
    else:
        world.projects.create(review_project, "Original", _repo(tmp_path, "original"))
        world.team.members.add((review_project, "reviewer"))
        source = sessions.create("reviewer", project_id=review_project).address
    sessions.get(source).append(ChatMessage.user("retained history"))
    review = (
        await sessions.fork(
            source, target_project_id=review_project, run_kind=RunKind.MEMORY_REFLECTION
        )
    ).address
    nested = (
        await sessions.fork(
            review, target_project_id=review_project, run_kind=RunKind.SKILL_REFLECTION
        )
    ).address
    ordinary = (await sessions.fork(review, target_project_id=review_project)).address
    if review_project is None:
        world.agents.update("original", current_session_id=review.session_id)
    review_scratch = await world.scratch_areas.folder_for(review)
    archive: Callable[[str], Awaitable[AgentArchiveOutcome | ProjectArchiveOutcome]]
    if scope_kind == "agent":
        world.agents.create("destination")
        target = SessionAddress(None, "destination", source.session_id)
        archive = world.service.archive_agent
    else:
        world.projects.create("destination", "Destination", _repo(tmp_path, "destination"))
        target = SessionAddress("destination", "worker", source.session_id)
        archive = world.service.archive_project
    await sessions.move(source, target)

    # A review's original owner lies outside the removed Agent/Project, but its
    # running or guarded work still blocks the entire archive before resources close.
    async with world.runs.session_admission_guard(nested):
        with pytest.raises(RunAdmissionBlockedError):
            await archive("destination")
    assert not world.commands.closed
    archived = await archive("destination")
    assert archived.session_count == 3
    assert archived.review_addresses == (review, nested)
    assert not any(sessions.exists(address) for address in (target, review, nested))
    assert sessions.exists(ordinary)
    assert world.commands.closed[-2:] == [("session", review), ("session", nested)]
    assert not review_scratch.area.exists()
    if review_project is None:
        assert world.agents.get("original").current_session_id != review.session_id

    # Every original owner is required, even when restoring an Agent or Project
    # under a new name. Its unrelated user fork belongs to its separate archive.
    original_archive = (
        await world.service.archive_agent("original")
        if review_project is None
        else await world.service.archive_project(review_project)
    )
    check = await world.service.restore_check(archived.entry_id, target_id="restored")
    assert [problem.code for problem in check.blockers] == ["scope_missing"]
    with pytest.raises(ArchiveNotRestorableError):
        await world.service.restore(archived.entry_id, target_id="restored")
    await world.service.restore(original_archive.entry_id)
    if review_project is not None:
        world.team.members.remove((review_project, "reviewer"))
        check = await world.service.restore_check(archived.entry_id, target_id="restored")
        assert [problem.code for problem in check.blockers] == ["scope_missing"]
        world.team.members.add((review_project, "reviewer"))
    async with world.runs.session_admission_guard(review):
        with pytest.raises(RunAdmissionBlockedError):
            await world.service.restore(archived.entry_id, target_id="restored")
    restored = await world.service.restore(archived.entry_id, target_id="restored")
    moved = replace(target, **{"agent_id" if scope_kind == "agent" else "project_id": "restored"})
    assert set(restored.addresses) == {moved, review, nested}
    assert sessions.review_forks(moved) == (review, nested)

    # Permanent removal follows the same membership; only an ordinary fork
    # receives materialized history and stays live.
    repeated = await archive("restored")
    await world.service.purge([repeated.entry_id])
    assert sessions.exists(ordinary)
    assert [message.content for message in sessions.get(ordinary).load_active()] == [
        "retained history"
    ]
    assert not any(sessions.exists(address) for address in (moved, review, nested))


@pytest.mark.asyncio
async def test_restoring_a_parent_under_a_new_session_id_keeps_its_children(
    world: ArchiveWorld,
) -> None:
    agent = agent_with_session(world, "coder")
    parent = SessionAddress(None, "coder", agent.current_session_id)
    sessions = world.sessions
    link = {"agent_id": "coder", "session_id": parent.session_id, "run_id": "parent-run"}
    child = sessions.create("coder", session_id="child").address
    archived_child = sessions.create("coder", session_id="archived-child").address
    relinked = sessions.create("coder", session_id="old-but-linked-later").address
    for address in (child, archived_child):
        sessions.set_metadata(address, {"subagent_parent": link})
    child_entry = await world.service.archive_session(archived_child)
    entry = await world.service.archive_session(parent)
    sessions.create("coder", session_id=parent.session_id)
    new_child = sessions.create("coder", session_id="new-child").address
    sessions.set_metadata(new_child, {"subagent_parent": link})
    sessions.set_metadata(relinked, {"subagent_parent": link})

    await world.service.restore(entry.entry_id, target_id="restored")

    renamed = SessionAddress(None, "coder", "restored")
    assert sessions.subagent_children(renamed) == [child]
    assert sessions.subagent_children(parent) == [relinked, new_child]
    await world.service.restore(child_entry.entry_id)
    assert sessions.subagent_children(renamed) == [child, archived_child]
    assert sessions.get_metadata(child)["subagent_parent"]["run_id"] == "parent-run"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["session", "agent", "project"])
async def test_archiving_only_a_subagent_keeps_its_area_until_the_root_is_archived(
    world: ArchiveWorld, tmp_path: Path, kind: str
) -> None:
    world.agents.create("parent")
    world.agents.create("unrelated")
    parent = world.sessions.create("parent", session_id="same-public-id").address
    unrelated = world.sessions.create("unrelated", session_id=parent.session_id).address
    project_id = "workers" if kind == "project" else None
    if project_id:
        world.projects.create(project_id, "Workers", _repo(tmp_path))
    else:
        world.agents.create("worker")
    child = world.sessions.create("worker", session_id="child", project_id=project_id).address
    world.sessions.set_metadata(
        child,
        {
            "subagent_parent": {
                "id": "sub_child",
                "agent_id": parent.agent_id,
                "session_id": parent.session_id,
                "project_id": parent.project_id,
            }
        },
    )
    parent_scratch = await world.scratch_areas.folder_for(parent)
    child_scratch = await world.scratch_areas.folder_for(child)
    unrelated_scratch = await world.scratch_areas.folder_for(unrelated)
    assert child_scratch.area == parent_scratch.area
    assert unrelated_scratch.area != parent_scratch.area

    if kind == "session":
        await world.service.archive_session(child)
    elif kind == "agent":
        await world.service.archive_agent(child.agent_id)
    else:
        assert project_id is not None
        await world.service.archive_project(project_id)

    assert parent_scratch.area.exists() and child_scratch.own.exists()
    await world.service.archive_session(parent)
    assert not parent_scratch.area.exists()
    assert unrelated_scratch.area.exists()


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


async def _reserved_agent_target(world: ArchiveWorld, tmp_path: Path) -> tuple[str, str | None]:
    world.agents.create("coder")
    # Windows reserves device names; a new id is checked before it reads as taken there.
    return (await world.service.archive_agent("coder")).entry_id, "nul"


async def _reserved_project_target(world: ArchiveWorld, tmp_path: Path) -> tuple[str, str | None]:
    world.projects.create("vbot", "vBot", _repo(tmp_path))
    return (await world.service.archive_project("vbot")).entry_id, "Com1"


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
        pytest.param(_reserved_agent_target, "reserved_target_id", id="reserved-agent-target"),
        pytest.param(_reserved_project_target, "reserved_target_id", id="reserved-project-target"),
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
async def test_a_renamed_agent_takes_its_archived_sessions_and_an_earlier_one_keeps_its_own(
    world: ArchiveWorld,
) -> None:
    world.agents.create("coder")
    world.sessions.create("coder", session_id="earlier")
    earlier = (await world.service.archive_agent("coder")).entry_id
    agent_with_session(world, "coder")
    notes = world.sessions.create("coder", session_id="notes").address
    deleted = (await world.service.archive_session(notes)).entry_id

    world.agents.finish_rename(world.agents.rename("coder", "researcher").rename)

    assert world.entry(deleted).agent_id == "researcher"
    assert (await world.service.restore(deleted)).addresses == (
        SessionAddress(None, "researcher", "notes"),
    )
    # The earlier ``coder`` returns with its own Session under its own id.
    assert (await world.service.restore(earlier)).addresses == (
        SessionAddress(None, "coder", "earlier"),
    )


@pytest.mark.asyncio
async def test_purge_deletes_the_sessions_payload_and_entry_after_importing_usage(
    world: ArchiveWorld,
) -> None:
    coder = agent_with_session(world, "coder")
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

    def refuse(_path: Path, **_kwargs: Any) -> None:
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
@pytest.mark.parametrize("case", ["id-free", "id-reused", "cleanup-fails"])
async def test_purging_an_agent_whose_cleanup_did_not_finish_completes_it_first(
    world: ArchiveWorld, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("delegation lists unavailable")

    world.agents.create("coder")
    world.agents.create("manager", tools={"subagent": {"allowed_agents": ["coder"]}})
    monkeypatch.setattr(world.agents, "remove_delegation_grants", fail)
    entry_id = (await world.service.archive_agent("coder")).entry_id
    assert world.entry(entry_id).cleanup_pending
    if case != "cleanup-fails":
        monkeypatch.undo()
    if case == "id-reused":
        world.agents.create("coder")

    outcome = await world.service.purge([entry_id])

    allowed = world.agents.get("manager").tools["subagent"]["allowed_agents"]
    if case == "cleanup-fails":
        # Nothing is deleted without the cleanup; the retention sweep continues it.
        assert outcome.pending == (PendingPurge(entry_id, "OSError"),)
        assert world.entry(entry_id).state == "purging"
        assert world.payload(entry_id, "agent").is_dir()
        assert allowed == ["coder"]
    else:
        assert [item.entry_id for item in outcome.purged] == [entry_id]
        assert world.sessions.archive_ledger.entry(entry_id) is None
        # A live Agent that took the id keeps the grants naming it.
        assert allowed == (["coder"] if case == "id-reused" else [])


@pytest.mark.asyncio
async def test_an_archived_agents_secret_grants_wait_for_its_restore_and_go_with_its_purge(
    world: ArchiveWorld,
) -> None:
    agents, secrets = world.agents, world.secrets

    def granted(agent_id: str) -> list[str]:
        return [secret.name for secret in secrets.naming_identity_agent(agent_id)]

    agent_with_session(world, "coder")
    agents.create("manager")
    for name, holders in (("API", ["coder"]), ("SHARED", ["coder", "manager"])):
        secrets.create(
            name=name,
            kind="token",
            values={"value": f"{name}-value-1234"},
            actor="user",
            agents=holders,
        )

    archived = await world.service.archive_agent("coder")
    # The grants wait for the restore, so no namesake may take them.
    with pytest.raises(AgentReferencedError) as refused:
        agents.create("coder")
    assert refused.value.references == ("secret:API", "secret:SHARED")
    await world.service.restore(archived.entry_id)
    assert granted("coder") == ["API", "SHARED"]

    # Restored under another id, the Agent takes its grants along.
    archived = await world.service.archive_agent("coder")
    await world.service.restore(archived.entry_id, target_id="coder-new")
    assert granted("coder") == [] and granted("coder-new") == ["API", "SHARED"]

    # Another archived Agent cannot return under an id whose grants wait.
    waiting = await world.service.archive_agent("coder-new")
    agent_with_session(world, "other")
    other = await world.service.archive_agent("other")
    with pytest.raises(ArchiveRestoreConflictError) as conflict:
        await world.service.restore(other.entry_id, target_id="coder-new")
    assert [item.code for item in conflict.value.conflicts] == ["agent_id_referenced"]

    # A purge drops the grants, never the Secrets or another Agent's grants.
    await world.service.purge([waiting.entry_id])
    assert granted("coder-new") == []
    assert {secret.name: secret.agents for secret in secrets.all()} == {
        "API": (),
        "SHARED": ("manager",),
    }
    assert (await world.service.restore_check(other.entry_id, target_id="coder-new")).possible

    # A live Agent that took the id again keeps the grants naming it.
    agents.create("other")
    api = next(secret for secret in secrets.all() if secret.name == "API")
    secrets.update(api.id, agents=["other"], actor="user")
    await world.service.purge([other.entry_id])
    assert granted("other") == ["API"]


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["unknown-entry", "busy-entry", "usage-import-fails"])
async def test_a_refused_purge_deletes_nothing(world: ArchiveWorld, refusal: str) -> None:
    coder = agent_with_session(world, "coder")
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
        assert outcome.skipped == (
            SkippedPurge(archived.entry_id, "usage_import_failed", "archived"),
        )
        assert outcome.pending == ()
        # Nothing was claimed: the entry stays restorable.
        assert world.entry(archived.entry_id).state == "archived"

    assert world.session_rows("coder") == [(coder.current_session_id, "archived")]
    assert world.payload(archived.entry_id, "agent").is_dir()


@pytest.mark.asyncio
@pytest.mark.parametrize("taken_by", ["restore", "purge"])
async def test_an_entry_taken_before_the_claim_is_skipped_or_gone(
    world: ArchiveWorld, monkeypatch: pytest.MonkeyPatch, taken_by: str
) -> None:
    coder = agent_with_session(world, "coder")
    entry_id = (await world.service.archive_agent("coder")).entry_id
    ledger = world.sessions.archive_ledger
    begin_purge = ledger.begin_purge

    def taken_first(claimed_id: str) -> ArchiveEntry:
        # Another operation reaches the entry between the purge's check and its claim.
        monkeypatch.setattr(ledger, "begin_purge", begin_purge)
        if taken_by == "restore":
            ledger.begin_restore(claimed_id, {"target_id": None})
        else:
            entry = begin_purge(claimed_id)
            while ledger.purge_next_session(entry.entry_key):
                pass
            ledger.finish_purge(entry.entry_key)
        return begin_purge(claimed_id)

    monkeypatch.setattr(ledger, "begin_purge", taken_first)
    outcome = await world.service.purge([entry_id])

    assert outcome.purged == outcome.pending == ()
    if taken_by == "restore":
        # Held by the restore: nothing of it is deleted, and nothing retries it.
        assert (outcome.skipped, outcome.gone) == (
            (SkippedPurge(entry_id, "busy", "restoring"),),
            (),
        )
        assert world.session_rows("coder") == [(coder.current_session_id, "archived")]
        assert world.payload(entry_id, "agent").is_dir()
    else:
        assert (outcome.skipped, outcome.gone) == ((), (entry_id,))


@pytest.mark.asyncio
@pytest.mark.parametrize("payload_state", ["missing", "unavailable"])
async def test_entries_list_with_labels_and_show_their_sessions_files_and_restorability(
    world: ArchiveWorld,
    deny_access: Callable[[Path], None],
    payload_state: str,
) -> None:
    world.agents.create("manager")
    coder = agent_with_session(world, "coder", "Coder Agent")
    notes = world.sessions.create("manager", session_id="notes").address
    world.sessions.set_title(notes, "Notes")
    agent_entry = await world.service.archive_agent("coder")
    session_entry = await world.service.archive_session(notes)

    page = await world.service.list(ArchiveEntryFilter())
    if payload_state == "missing":
        shutil.rmtree(world.payload(agent_entry.entry_id, "agent"))
    else:
        deny_access(world.payload(agent_entry.entry_id, "agent").parent)
    agent_detail = await world.service.show(agent_entry.entry_id)
    session_detail = await world.service.show(session_entry.entry_id)

    assert {item.entry.entry_id: (item.label, item.restorable) for item in page.entries} == {
        agent_entry.entry_id: ("Coder Agent", True),
        session_entry.entry_id: ("Notes", True),
    }
    assert [member.address.session_id for member in agent_detail.sessions] == [
        coder.current_session_id
    ]
    assert agent_detail.files == payload_state
    assert agent_detail.listing.not_restorable_reason == f"payload_{payload_state}"
    assert [problem.code for problem in agent_detail.restore.blockers] == [
        f"payload_{payload_state}"
    ]
    with pytest.raises(ArchiveNotRestorableError):
        await world.service.restore(agent_entry.entry_id)
    assert (session_detail.files, session_detail.restore.possible) == ("none", True)
    assert agent_detail.user_folders == session_detail.user_folders == ()


@pytest.mark.asyncio
async def test_purge_at_ends_the_retention_period_of_entries_retention_deletes(
    world: ArchiveWorld, tmp_path: Path
) -> None:
    world.agents.create("manager")
    world.agents.create("writer")
    archived = await world.service.archive_agent("writer")
    pending = await world.service.archive_agent("manager")
    world.sessions.archive_ledger.begin_purge(pending.entry_id)
    # Retention never deletes folders the user may own, nor a payload found without
    # its entry; only a manual purge does.
    legacy = legacy_agent_entry(world, tmp_path / "notes")

    page = await world.service.list(ArchiveEntryFilter())
    kept = await ArchiveService(replace(world.services, retention_days=lambda: None)).list(
        ArchiveEntryFilter()
    )

    start = datetime.fromisoformat(world.entry(archived.entry_id).retention_start)
    assert page.retention_days == 30
    assert {item.entry.entry_id: item.purge_at for item in page.entries} == {
        archived.entry_id: format_canonical_timestamp(start + timedelta(days=30)),
        pending.entry_id: None,
        legacy: None,
    }
    assert kept.retention_days is None
    assert {item.purge_at for item in kept.entries} == {None}
    assert {item.entry.entry_id for item in page.entries if item.may_hold_user_folders} == {legacy}


@pytest.mark.asyncio
async def test_a_purge_of_every_matching_entry_keeps_entries_that_may_hold_user_folders(
    world: ArchiveWorld, tmp_path: Path
) -> None:
    world.agents.create("manager")
    world.agents.create("writer")
    archived = await world.service.archive_agent("writer")
    legacy = legacy_agent_entry(world, tmp_path / "notes")
    older = world.data_dir / "archive" / "old-files"
    older.mkdir()
    (older / "notes.md").write_text("mine", encoding="utf-8")
    files = world.sessions.archive_ledger.adopt_payload(
        "arc_files",
        ARCHIVE_KIND_FILES,
        subject_id="old-files",
        archived_at=utc_now_timestamp(),
        trees=(ArchiveTree("archive/old-files", ARCHIVE_TREE_FILES, None),),
    ).entry_id

    every = await world.service.purge(all_matching=ArchiveEntryFilter())

    assert [item.entry_id for item in every.purged] == [archived.entry_id]
    assert set(every.kept) == {KeptEntry(legacy, "user_folders"), KeptEntry(files, "files")}
    assert (world.data_dir / "archive" / "coder" / "workspace" / "notes.md").is_file()
    assert (older / "notes.md").is_file()
    # Only naming an entry deletes it, folders included.
    named = await world.service.purge([legacy, files])
    assert {item.entry_id for item in named.purged} == {legacy, files}
    assert named.kept == ()
    assert not (world.data_dir / "archive" / "coder").exists()
    assert not older.exists()
