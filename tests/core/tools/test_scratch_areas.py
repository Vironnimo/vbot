"""Scratch Areas preserve tree ownership and remove only inactive temporary data."""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from core.sessions import ChatSessionManager, SessionAddress
from core.subagents.links import ancestors
from core.tools import scratch_areas as scratch_module
from core.tools.scratch_areas import SCRATCH_RETENTION, ScratchAreas, ScratchUnavailableError


class Tree:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.now = 1_900_000_000.0
        self.parents: dict[SessionAddress, SessionAddress] = {}
        self.generations: dict[SessionAddress, str] = {}
        self.busy: set[SessionAddress] = set()

    def session(
        self,
        session: str,
        *,
        agent: str = "worker",
        project: str | None = None,
        parent: SessionAddress | None = None,
    ) -> SessionAddress:
        address = SessionAddress(project, agent, session)
        self.generations[address] = f"gen_{len(self.generations)}"
        if parent is not None:
            self.parents[address] = parent
        return address

    def ancestors(self, address: SessionAddress) -> list[SessionAddress]:
        result: list[SessionAddress] = []
        while address in self.parents:
            address = self.parents[address]
            result.append(address)
        return result

    def owner(self) -> ScratchAreas:
        return ScratchAreas(
            self.path,
            identity=self.generations.get,
            ancestors=self.ancestors,
            busy_sessions=lambda: self.busy,
            clock=lambda: self.now,
        )


@pytest.mark.asyncio
async def test_tree_shares_an_area_with_distinct_participant_folders_and_fork(
    tmp_path: Path,
) -> None:
    tree = Tree(tmp_path)
    root = tree.session("main")
    child = tree.session("main", agent="second", parent=root)
    grandchild = tree.session("main", project="project", parent=child)
    fork = tree.session("fork")
    other = tree.session("main", project="other")
    owner = tree.owner()

    root_folder, child_folder, grandchild_folder, fork_folder, other_folder = await asyncio.gather(
        *(owner.folder_for(address) for address in (root, child, grandchild, fork, other))
    )

    assert root_folder.area == child_folder.area == grandchild_folder.area
    assert len({root_folder.own, child_folder.own, grandchild_folder.own}) == 3
    assert len({root_folder.area, fork_folder.area, other_folder.area}) == 3
    assert root_folder.area.name == tree.generations[root]
    assert child_folder.own.name == tree.generations[child]
    assert all(folder.tmp.is_dir() for folder in (root_folder, child_folder, grandchild_folder))
    assert not (await owner.folder_for(root)).recreated


@pytest.mark.asyncio
async def test_recreation_and_process_launch_keep_a_durable_incarnation(tmp_path: Path) -> None:
    tree = Tree(tmp_path)
    address = tree.session("root")
    owner = tree.owner()
    first = await owner.folder_for(address)
    assert first.recreated
    assert [entry.name for entry in first.own.iterdir()] == ["tmp"]
    assert [entry.name for entry in first.area.iterdir()] == [first.own.name]
    (first.own / "work.txt").write_text("intermediate", encoding="utf-8")
    assert (await tree.owner().folder_for(address)).incarnation == first.incarnation
    shutil.rmtree(first.tmp)
    only_tmp = await owner.folder_for(address)
    assert only_tmp.tmp.is_dir()
    assert only_tmp.recreated
    assert only_tmp.incarnation != first.incarnation
    shutil.rmtree(first.own)

    recreated_by_process = await owner.folder_for(address)
    next_run = await tree.owner().folder_for(address)

    assert recreated_by_process.recreated
    assert recreated_by_process.incarnation != first.incarnation
    assert next_run.incarnation == recreated_by_process.incarnation
    assert not next_run.recreated
    (owner.state_root / next_run.area.name / f"{next_run.own.name}.incarnation").unlink()
    assert (await owner.folder_for(address)).incarnation != next_run.incarnation


@pytest.mark.asyncio
async def test_retention_survives_restart_and_busy_descendant_refreshes_activity(
    tmp_path: Path,
) -> None:
    tree = Tree(tmp_path)
    old_root = tree.session("old")
    active_root = tree.session("active")
    child = tree.session("child", parent=active_root)
    young_root = tree.session("young")
    owner = tree.owner()
    old = await owner.folder_for(old_root)
    active = await owner.folder_for(active_root)
    tree.now += SCRATCH_RETENTION.total_seconds() - 60
    young = await owner.folder_for(young_root)
    tree.now += 61
    tree.busy.add(child)
    restarted = tree.owner()

    assert await restarted.sweep() == 1
    assert not old.area.exists()
    assert young.area.is_dir()
    assert active.area.is_dir()
    assert (owner.state_root / active.area.name / ".activity").stat().st_mtime == tree.now
    tree.busy.clear()
    tree.now += SCRATCH_RETENTION.total_seconds() + 1
    assert await restarted.sweep() == 2


@pytest.mark.parametrize("damage", ["missing", "directory", "link", "state_root_removed"])
@pytest.mark.asyncio
async def test_missing_activity_state_adopts_old_areas_with_one_retention_grace(
    tmp_path: Path, damage: str
) -> None:
    tree = Tree(tmp_path)
    owner = tree.owner()
    folder = await owner.folder_for(tree.session("root"))
    marker = owner.state_root / folder.area.name / ".activity"
    marker.unlink()
    if damage == "directory":
        marker.mkdir()
    elif damage == "link":
        outside = tmp_path / "untouched.txt"
        outside.write_text("keep", encoding="utf-8")
        try:
            marker.symlink_to(outside)
        except OSError:
            pytest.skip("host does not allow symbolic links")
    elif damage == "state_root_removed":
        shutil.rmtree(owner.state_root)
    # A recently active area from the old layout may have a very old directory
    # time. Its arbitrary legacy marker must neither be trusted nor be required.
    (folder.area / ".activity").write_bytes(b"\xff arbitrary Agent data")
    os.utime(folder.area, (tree.now, tree.now))
    tree.now += SCRATCH_RETENTION.total_seconds() + 1
    assert await owner.sweep() == 0
    assert marker.stat().st_mtime == tree.now
    if damage == "link":
        assert outside.read_text(encoding="utf-8") == "keep"
    assert await tree.owner().sweep() == 0
    tree.now += SCRATCH_RETENTION.total_seconds() + 1
    assert await owner.sweep() == 1
    assert not folder.area.exists()


@pytest.mark.asyncio
async def test_archive_capture_removes_only_explicit_roots_after_live_rows_disappear(
    tmp_path: Path,
) -> None:
    tree = Tree(tmp_path)
    root = tree.session("root")
    child = tree.session("child", parent=root)
    unrelated = tree.session("unrelated")
    owner = tree.owner()
    root_folder = await owner.folder_for(root)
    child_folder = await owner.folder_for(child)
    other_folder = await owner.folder_for(unrelated)
    await owner.remove_area_of([child])
    assert root_folder.area.exists()
    captured = await owner.roots_for_archive([root, child])
    assert captured == (tree.generations[root],)
    del tree.generations[root]

    await owner.remove_areas(captured)

    assert not root_folder.area.exists()
    assert other_folder.area.is_dir()
    surviving = await tree.owner().folder_for(child)
    assert surviving.recreated
    assert surviving.area != child_folder.area
    assert surviving.area.name == tree.generations[child]
    assert await owner.roots_for_archive([child]) == (tree.generations[child],)
    # Reusing the former root address must not adopt its older Scratch state.
    tree.generations[root] = "new_generation"
    assert (await owner.folder_for(root)).area != root_folder.area


@pytest.mark.parametrize("kind", ["symlink", "junction"])
@pytest.mark.asyncio
async def test_removal_never_follows_links_and_clears_read_only_files(
    tmp_path: Path, kind: str
) -> None:
    tree = Tree(tmp_path / "data")
    address = tree.session("root")
    owner = tree.owner()
    folder = await owner.folder_for(address)
    outside = tmp_path / "outside"
    outside.mkdir()
    valuable = outside / "keep.txt"
    valuable.write_text("keep", encoding="utf-8")
    _directory_link(folder.own / "linked", outside, kind)
    readonly = folder.own / "readonly"
    readonly.write_text("remove", encoding="utf-8")
    readonly.chmod(stat.S_IREAD)

    await owner.remove_area_of([address])

    assert not folder.area.exists()
    assert valuable.read_text(encoding="utf-8") == "keep"
    assert list(owner.root.iterdir()) == []


def _directory_link(path: Path, target: Path, kind: str) -> None:
    if kind == "junction":
        if sys.platform != "win32":
            pytest.skip("junctions exist only on Windows")
        import _winapi

        _winapi.CreateJunction(str(target), str(path))
    else:
        try:
            path.symlink_to(target, target_is_directory=True)
        except OSError:
            pytest.skip("host does not allow symbolic links")


@pytest.mark.parametrize("component", ["area", "own", "tmp", "state_root"])
@pytest.mark.parametrize("replacement", ["file", "symlink", "junction"])
@pytest.mark.asyncio
async def test_folder_creation_repairs_disposable_entries_without_following_links(
    tmp_path: Path, component: str, replacement: str
) -> None:
    tree = Tree(tmp_path / "data")
    address = tree.session("root")
    owner = tree.owner()
    folder = await owner.folder_for(address)
    outside = tmp_path / "outside"
    outside.mkdir()
    valuable = outside / "keep.txt"
    valuable.write_text("keep", encoding="utf-8")
    path = owner.state_root if component == "state_root" else getattr(folder, component)
    shutil.rmtree(path)
    if replacement == "file":
        path.write_text("replace this disposable entry", encoding="utf-8")
    else:
        _directory_link(path, outside, replacement)

    repaired = await owner.folder_for(address)

    assert repaired.tmp.is_dir()
    assert repaired.incarnation != folder.incarnation
    assert valuable.read_text(encoding="utf-8") == "keep"
    assert sorted(item.name for item in outside.iterdir()) == ["keep.txt"]


@pytest.mark.parametrize("damage", ["missing", "text", "binary", "directory", "link"])
@pytest.mark.asyncio
async def test_external_incarnation_state_can_be_rebuilt_without_losing_work(
    tmp_path: Path, damage: str
) -> None:
    tree = Tree(tmp_path)
    address = tree.session("root")
    owner = tree.owner()
    folder = await owner.folder_for(address)
    work = folder.own / "keep.txt"
    work.write_text("work", encoding="utf-8")
    marker = owner.state_root / folder.area.name / f"{folder.own.name}.incarnation"
    marker.unlink()
    if damage == "text":
        marker.write_text("arbitrary damaged state", encoding="ascii")
    elif damage == "binary":
        marker.write_bytes(b"\xff\x00")
    elif damage == "directory":
        marker.mkdir()
    elif damage == "link":
        try:
            marker.symlink_to(work)
        except OSError:
            pytest.skip("host does not allow symbolic links")

    recreated = await tree.owner().folder_for(address)

    assert recreated.incarnation != folder.incarnation
    assert recreated.recreated
    assert work.read_text(encoding="utf-8") == "work"
    assert (await tree.owner().folder_for(address)).incarnation == recreated.incarnation


@pytest.mark.asyncio
async def test_concurrent_root_repairs_preserve_new_pending_retirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = Tree(tmp_path)
    first_address = tree.session("first")
    second_address = tree.session("second")
    owner = tree.owner()
    owner.root.mkdir(parents=True)
    owner.state_root.write_text("damaged shared metadata root", encoding="utf-8")
    pending = owner.state_root / tree.generations[first_address] / ".retire"
    entered = asyncio.Event()
    second_attempted = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_remove = scratch_module._remove_disposable
    original_ensure = owner._ensure_state_root
    first_worker: int | None = None
    removals = 0

    def remove(path: Path, *, within: Path) -> None:
        nonlocal removals, first_worker
        if path == owner.state_root:
            removals += 1
            first_worker = threading.get_ident()
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(2)
            original_remove(path, within=within)
            # Another root can immediately persist archive intent in the repaired
            # directory; a delayed repair must not remove this newly written state.
            pending.parent.mkdir(parents=True)
            pending.touch()
            return
        original_remove(path, within=within)

    def ensure() -> None:
        if first_worker is not None and threading.get_ident() != first_worker:
            loop.call_soon_threadsafe(second_attempted.set)
        original_ensure()

    monkeypatch.setattr(scratch_module, "_remove_disposable", remove)
    monkeypatch.setattr(owner, "_ensure_state_root", ensure)
    # Use a third root for the first caller so its folder lookup does not itself
    # consume the deliberately created first root's pending archive intent.
    first_call = asyncio.create_task(owner.folder_for(tree.session("third")))
    second_call = None
    try:
        await entered.wait()
        second_call = asyncio.create_task(owner.folder_for(second_address))
        await second_attempted.wait()
    finally:
        release.set()
        first = await first_call
        if second_call is not None:
            second = await second_call

    assert removals == 1
    assert pending.is_file()
    assert (owner.state_root / first.area.name).is_dir()
    assert (owner.state_root / second.area.name).is_dir()


@pytest.mark.parametrize("operation", ["folder", "sweep"])
@pytest.mark.asyncio
async def test_cancelled_setup_does_not_expose_a_late_filesystem_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    operation: str,
) -> None:
    tree = Tree(tmp_path)
    address = tree.session("root")
    owner = tree.owner()
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def late_failure(*_arguments: object) -> None:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(2)
        raise PermissionError("private-filename-that-must-not-be-logged")

    call = None
    if operation == "folder":
        monkeypatch.setattr(owner, "_folder_for", late_failure)
        call = asyncio.create_task(owner.folder_for(address))
    else:
        monkeypatch.setattr(owner, "_sweep", late_failure)
        owner.start()
    try:
        await entered.wait()
        if call is not None:
            call.cancel()
        else:
            owner.stop()
    finally:
        release.set()
    if call is not None:
        with pytest.raises(asyncio.CancelledError):
            await call
    else:
        await owner.aclose()

    assert caplog.records
    assert "private-filename-that-must-not-be-logged" not in caplog.text


@pytest.mark.parametrize("contents", ["binary", "directory", "link"])
@pytest.mark.asyncio
async def test_legacy_markers_are_inert_agent_files(tmp_path: Path, contents: str) -> None:
    tree = Tree(tmp_path)
    address = tree.session("root")
    owner = tree.owner()
    folder = await owner.folder_for(address)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    for marker in (folder.own / ".incarnation", folder.area / ".activity"):
        if contents == "binary":
            marker.write_bytes(b"\xff damaged old metadata")
        elif contents == "directory":
            marker.mkdir()
        else:
            try:
                marker.symlink_to(outside)
            except OSError:
                pytest.skip("host does not allow symbolic links")

    assert (await owner.folder_for(address)).incarnation == folder.incarnation
    assert await owner.sweep() == 0
    assert outside.read_text(encoding="utf-8") == "keep"


@pytest.mark.asyncio
async def test_agent_can_empty_own_folder_and_recreate_it_before_next_call(tmp_path: Path) -> None:
    tree = Tree(tmp_path)
    address = tree.session("root")
    owner = tree.owner()
    first = await owner.folder_for(address)
    # Emptying in place leaves the directory's filesystem identity intact.
    shutil.rmtree(first.tmp)
    emptied = await owner.folder_for(address)
    assert [entry.name for entry in emptied.own.iterdir()] == ["tmp"]
    assert emptied.incarnation != first.incarnation
    # An Agent can also replace the complete directory before the next lookup.
    shutil.rmtree(emptied.own)
    emptied.own.mkdir()
    emptied.tmp.mkdir()
    replaced = await tree.owner().folder_for(address)
    assert replaced.incarnation != emptied.incarnation
    assert replaced.recreated


@pytest.mark.parametrize("unsafe", ["../outside", "absolute/path", "nul", ".."])
@pytest.mark.asyncio
async def test_rejects_unsafe_ids_before_creating_files(tmp_path: Path, unsafe: str) -> None:
    tree = Tree(tmp_path)
    address = tree.session("root")
    tree.generations[address] = unsafe
    owner = tree.owner()
    with pytest.raises(ScratchUnavailableError):
        await owner.folder_for(address)
    assert not owner.root.exists()


@pytest.mark.asyncio
async def test_unreadable_folder_is_not_treated_as_missing(
    tmp_path: Path,
    deny_access: Callable[[Path], None],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tree = Tree(tmp_path)
    address = tree.session("root")
    owner = tree.owner()
    first = await owner.folder_for(address)
    deny_access(first.own)
    for _ in range(2):
        with pytest.raises(ScratchUnavailableError):
            await owner.folder_for(address)
    assert len(caplog.records) == 1
    assert str(first.own) not in caplog.text
    monkeypatch.undo()
    assert (await owner.folder_for(address)).incarnation == first.incarnation


@pytest.mark.asyncio
async def test_failed_retired_removal_warns_once_then_retries_next_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    tree = Tree(tmp_path)
    address = tree.session("root")
    owner = tree.owner()
    folder = await owner.folder_for(address)
    tree.now += SCRATCH_RETENTION.total_seconds() + 1
    original = owner._discard

    def held_open(path: Path) -> None:
        raise PermissionError(13, "private-internal-filename")

    monkeypatch.setattr(owner, "_discard", held_open)
    assert await owner.sweep() == 0
    assert len(caplog.records) == 1
    assert "private-internal-filename" not in caplog.text
    assert not folder.area.exists()
    assert len(list(owner.root.iterdir())) == 1
    monkeypatch.setattr(owner, "_discard", original)
    assert await owner.sweep() == 1
    assert list(owner.root.iterdir()) == []


@pytest.mark.parametrize("resume_via", ["sweep", "restored_session"])
@pytest.mark.parametrize("marker_layout", ["current", "legacy"])
@pytest.mark.asyncio
async def test_archive_rename_failure_survives_restart_and_never_reuses_old_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    resume_via: str,
    marker_layout: str,
) -> None:
    tree = Tree(tmp_path)
    address = tree.session("root")
    owner = tree.owner()
    folder = await owner.folder_for(address)
    (folder.own / "old-result.txt").write_text("old", encoding="utf-8")
    original_rename = Path.rename

    def held_open(path: Path, target: str | os.PathLike[str]) -> Path:
        if path == folder.area:
            raise PermissionError(13, "private-internal-filename")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", held_open)
    await owner.remove_area_of([address])
    assert folder.own.is_dir()
    assert len(caplog.records) == 1
    assert "private-internal-filename" not in caplog.text
    if marker_layout == "legacy":
        # Older versions stored retirement intent beside the announced area.
        # Upgrade must preserve it even without any external metadata yet.
        (owner.root / f".retire-{folder.area.name}").touch()
        shutil.rmtree(owner.state_root)
    restarted = tree.owner()
    with pytest.raises(ScratchUnavailableError):
        await restarted.folder_for(address)

    monkeypatch.setattr(Path, "rename", original_rename)
    # Archive removal remains pending even for a fresh marker or a restored Run.
    if marker_layout == "current":
        assert (owner.state_root / folder.area.name / ".activity").stat().st_mtime == tree.now
    tree.busy.add(address)
    if resume_via == "sweep":
        assert await restarted.sweep() == 1
        assert list(restarted.root.iterdir()) == []
    fresh = await restarted.folder_for(address)

    assert fresh.recreated
    assert fresh.incarnation != folder.incarnation
    assert not (fresh.own / "old-result.txt").exists()
    assert list(restarted.root.iterdir()) == [fresh.area]
    assert await restarted.sweep() == 0
    assert (await restarted.folder_for(address)).incarnation == fresh.incarnation


@pytest.mark.asyncio
async def test_retirement_releases_area_lock_before_deleting_and_cannot_remove_new_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = Tree(tmp_path)
    address = tree.session("root")
    owner = tree.owner()
    folder = await owner.folder_for(address)
    tree.now += SCRATCH_RETENTION.total_seconds() + 1
    retired = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = owner._discard

    def paused_discard(path: Path) -> None:
        loop.call_soon_threadsafe(retired.set)
        assert release.wait(2)
        original(path)

    monkeypatch.setattr(owner, "_discard", paused_discard)
    sweep = asyncio.create_task(owner.sweep())
    try:
        await retired.wait()
        replacement = await owner.folder_for(address)
        assert replacement.recreated
        assert replacement.incarnation != folder.incarnation
    finally:
        release.set()
        await sweep
    assert replacement.own.is_dir()


@pytest.mark.asyncio
async def test_lifecycle_runs_initial_sweep_off_loop_and_close_settles_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = Tree(tmp_path)
    owner = tree.owner()
    loop_thread = threading.get_ident()
    started = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    loop = asyncio.get_running_loop()

    def paused_sweep(
        busy: tuple[SessionAddress, ...], stopping: threading.Event | None, activity_epoch: int
    ) -> int:
        assert threading.get_ident() != loop_thread
        loop.call_soon_threadsafe(started.set)
        assert release.wait(2)
        finished.set()
        return 0

    monkeypatch.setattr(owner, "_sweep", paused_sweep)
    owner.start()
    try:
        await started.wait()
        owner.stop()
        closing = asyncio.create_task(owner.aclose())
        await asyncio.sleep(0)
        assert not closing.done()
    finally:
        release.set()
        await owner.aclose()
    await closing
    assert finished.is_set()


@pytest.fixture
def sessions(tmp_path: Path, current_session_store_template: Path) -> Iterator[ChatSessionManager]:
    for name in ("data-store.json", "sessions.db"):
        shutil.copy2(current_session_store_template / name, tmp_path)
    manager = ChatSessionManager(tmp_path)
    yield manager
    manager.close()


@pytest.mark.parametrize("session_id", ["main", "MixedCase", "NUL"])
@pytest.mark.asyncio
async def test_real_session_generation_keeps_folder_when_moved_to_another_agent(
    tmp_path: Path, sessions: ChatSessionManager, session_id: str
) -> None:
    source = SessionAddress(None, "first", session_id)
    target = SessionAddress(None, "second", session_id)
    sessions.create(source.agent_id, source.session_id)
    owner = ScratchAreas(
        tmp_path,
        identity=lambda address: sessions.get(address).generation_id,
        ancestors=lambda address: ancestors(sessions, address),
        busy_sessions=lambda: (),
    )
    initial = await owner.folder_for(source)

    await sessions.move(source, target)

    moved = await owner.folder_for(target)
    assert moved.own == initial.own
    assert moved.incarnation == initial.incarnation
