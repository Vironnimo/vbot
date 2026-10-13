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
from core.tools.scratch_areas import SCRATCH_RETENTION, ScratchAreas


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
    assert (await tree.owner().folder_for(address)).incarnation == first.incarnation
    shutil.rmtree(first.tmp)
    only_tmp = await owner.folder_for(address)
    assert only_tmp.tmp.is_dir()
    assert not only_tmp.recreated
    assert only_tmp.incarnation == first.incarnation
    shutil.rmtree(first.own)

    recreated_by_process = await owner.folder_for(address)
    next_run = await tree.owner().folder_for(address)

    assert recreated_by_process.recreated
    assert recreated_by_process.incarnation != first.incarnation
    assert next_run.incarnation == recreated_by_process.incarnation
    assert not next_run.recreated
    (next_run.own / ".incarnation").unlink()
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
    assert (active.area / ".activity").stat().st_mtime == tree.now
    tree.busy.clear()
    tree.now += SCRATCH_RETENTION.total_seconds() + 1
    assert await restarted.sweep() == 2


@pytest.mark.asyncio
async def test_missing_activity_marker_uses_area_time(tmp_path: Path) -> None:
    tree = Tree(tmp_path)
    owner = tree.owner()
    folder = await owner.folder_for(tree.session("root"))
    (folder.area / ".activity").unlink()
    os.utime(folder.area, (tree.now, tree.now))
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


@pytest.mark.parametrize("component", ["area", "own", "tmp", ".activity", ".incarnation"])
@pytest.mark.asyncio
async def test_folder_creation_refuses_links_in_owned_paths(tmp_path: Path, component: str) -> None:
    tree = Tree(tmp_path / "data")
    address = tree.session("root")
    owner = tree.owner()
    folder = await owner.folder_for(address)
    outside = tmp_path / "outside"
    outside.mkdir()
    valuable = outside / "keep.txt"
    valuable.write_text("keep", encoding="utf-8")
    if component.startswith("."):
        container = folder.area if component == ".activity" else folder.own
        path = container / component
        path.unlink()
        try:
            path.symlink_to(valuable)
        except OSError:
            pytest.skip("host does not allow symbolic links")
    else:
        path = getattr(folder, component)
        shutil.rmtree(path)
        _directory_link(path, outside, "junction" if sys.platform == "win32" else "symlink")

    with pytest.raises(OSError):
        await owner.folder_for(address)

    assert valuable.read_text(encoding="utf-8") == "keep"
    assert sorted(item.name for item in outside.iterdir()) == ["keep.txt"]


@pytest.mark.parametrize("unsafe", ["../outside", "absolute/path", "nul", ".."])
@pytest.mark.asyncio
async def test_rejects_unsafe_ids_before_creating_files(tmp_path: Path, unsafe: str) -> None:
    tree = Tree(tmp_path)
    address = tree.session("root")
    tree.generations[address] = unsafe
    owner = tree.owner()
    with pytest.raises(ValueError):
        await owner.folder_for(address)
    assert not owner.root.exists()


@pytest.mark.asyncio
async def test_unreadable_folder_is_not_treated_as_missing(
    tmp_path: Path, deny_access: Callable[[Path], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = Tree(tmp_path)
    address = tree.session("root")
    owner = tree.owner()
    first = await owner.folder_for(address)
    deny_access(first.own)
    with pytest.raises(PermissionError):
        await owner.folder_for(address)
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
@pytest.mark.asyncio
async def test_archive_rename_failure_survives_restart_and_never_reuses_old_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    resume_via: str,
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
    restarted = tree.owner()
    with pytest.raises(PermissionError):
        await restarted.folder_for(address)

    monkeypatch.setattr(Path, "rename", original_rename)
    # Archive removal remains pending even for a fresh marker or a restored Run.
    assert (folder.area / ".activity").stat().st_mtime == tree.now
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
