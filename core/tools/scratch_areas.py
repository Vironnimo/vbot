"""Temporary working trees shared by a Session and its Sub-Agent descendants."""

from __future__ import annotations

import asyncio
import errno
import os
import re
import stat
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from threading import Event, Lock, RLock

from core.sessions import SessionAddress
from core.storage.layout import DataDirectoryLayout
from core.utils.errors import VBotError
from core.utils.file_status import is_link_status, stat_or_none
from core.utils.ids import is_safe_id
from core.utils.logging import get_logger
from core.utils.tree_move import remove_tree
from core.utils.workers import BoundedWorkerPool

SCRATCH_RETENTION = timedelta(days=7)
SCRATCH_SWEEP_INTERVAL_SECONDS = 3600.0
_WORKERS = BoundedWorkerPool(name="scratch-areas", max_workers=4)
_LOGGER = get_logger("tools.scratch_areas")
_TRASH = re.compile(r"\.trash-[0-9a-f]{32}\Z")
_INCARNATION = re.compile(r"[0-9a-f]{32}\Z")
_RETIRE_PREFIX = ".retire-"


@dataclass(frozen=True, slots=True)
class ScratchFolder:
    """One participant's current folder and the shared area around it."""

    own: Path
    area: Path
    tmp: Path
    recreated: bool
    incarnation: str


class ScratchAreas:
    """Own Scratch placement, activity, retention and archive cleanup.

    ``identity`` returns the live Session's globally unique generation id, or
    ``None`` for a missing Session. Public Session ids are not globally unique.
    Generations survive Agent rename and takeover; reusing an address starts a
    different generation. Ancestors are ordered nearest first. A missing Parent
    ends the live tree, so its surviving descendants start a fresh area.

    Busy addresses are snapshotted by the async caller on the Event Loop. Session
    lookups and every filesystem operation run on workers. Root resolution is
    intentionally uncached: archives, restores and address reuse change it.
    """

    def __init__(
        self,
        data_dir: str | Path,
        *,
        identity: Callable[[SessionAddress], str | None],
        ancestors: Callable[[SessionAddress], Sequence[SessionAddress]],
        busy_sessions: Callable[[], Iterable[SessionAddress]],
        clock: Callable[[], float] = time.time,
        sweep_interval_seconds: float = SCRATCH_SWEEP_INTERVAL_SECONDS,
    ) -> None:
        if sweep_interval_seconds <= 0:
            raise ValueError("Scratch sweep interval must be positive")
        self._layout = DataDirectoryLayout(Path(data_dir).absolute())
        self.root = self._layout.scratch_temporary
        self._identity = identity
        self._ancestors = ancestors
        self._busy_sessions = busy_sessions
        self._clock = clock
        self._sweep_interval_seconds = sweep_interval_seconds
        self._locks: dict[str, RLock] = {}
        self._locks_guard = Lock()
        self._sweep_lock = Lock()
        self._activity_lock = Lock()
        self._activity_epoch = 0
        self._stopping = Event()
        self._sweeper_task: asyncio.Task[None] | None = None

    async def folder_for(self, address: SessionAddress) -> ScratchFolder:
        """Ensure this Session's own folder and process temp child, recording activity."""
        return await _WORKERS.run(self._folder_for, address)

    async def roots_for_archive(self, addresses: Iterable[SessionAddress]) -> tuple[str, ...]:
        """Capture live tree roots before archive commits, without touching files.

        Pass the returned immutable generation ids to ``remove_areas`` only after
        the archive succeeded, when live Session lookup is no longer possible.
        """
        return await _WORKERS.run(self._roots_for_archive, tuple(addresses))

    async def remove_areas(self, root_ids: Iterable[str]) -> None:
        """Retire captured roots immediately; failed removals retry at the next sweep."""
        roots = tuple(dict.fromkeys(root_ids))
        for root_id in roots:
            _validate_id(root_id)
        await _WORKERS.run(self._remove_areas, roots)

    async def remove_area_of(self, addresses: Iterable[SessionAddress]) -> None:
        """Remove only the areas of live tree roots among ``addresses``."""
        await self.remove_areas(await self.roots_for_archive(addresses))

    async def sweep(self) -> int:
        """Remove expired areas and crash leftovers; return the number removed."""
        busy, epoch = self._activity_snapshot()
        return await _WORKERS.run(self._sweep, busy, None, epoch)

    def record_run_start(self) -> None:
        """Invalidate outstanding sweep decisions before an admitted Run can work.

        Runtime calls this synchronously from the Run manager's start callback.
        The counter lock never covers filesystem work or Session lookups.
        """
        with self._activity_lock:
            self._activity_epoch += 1

    def _activity_snapshot(self) -> tuple[tuple[SessionAddress, ...], int]:
        # Called on the Event Loop without yielding, so admission cannot occur
        # between the busy snapshot and its epoch.
        busy = tuple(self._busy_sessions())
        with self._activity_lock:
            return busy, self._activity_epoch

    def start(self) -> None:
        """Schedule an immediate sweep and then hourly sweeps."""
        if self._sweeper_task is not None and not self._sweeper_task.done():
            return
        self._stopping.clear()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            busy, epoch = self._activity_snapshot()
            self._sweep(busy, None, epoch)
            return
        self._sweeper_task = loop.create_task(self._sweep_loop(), name="scratch-areas-sweep")

    def stop(self) -> None:
        """Request cleanup to stop; ``aclose`` also waits for any worker to finish."""
        self._stopping.set()
        if self._sweeper_task is not None:
            self._sweeper_task.cancel()

    async def aclose(self) -> None:
        """Stop and settle periodic cleanup, including an already-started worker."""
        self.stop()
        if self._sweeper_task is not None:
            await asyncio.gather(self._sweeper_task, return_exceptions=True)
            self._sweeper_task = None

    def _resolve(self, address: SessionAddress) -> tuple[str, str] | None:
        own_id = self._identity(address)
        if own_id is None:
            return None
        _validate_id(own_id)
        root_id = own_id
        for ancestor in self._ancestors(address):
            ancestor_id = self._identity(ancestor)
            if ancestor_id is None:
                break
            _validate_id(ancestor_id)
            root_id = ancestor_id
        return own_id, root_id

    def _area_lock(self, root_id: str) -> RLock:
        with self._locks_guard:
            return self._locks.setdefault(root_id, RLock())

    def _root_directories(self) -> tuple[Path, ...]:
        return (self._layout.artifacts, self._layout.temporary, self.root)

    def _ensure_root(self) -> None:
        # The configured data root may itself be a user-selected link. Everything
        # below that trusted root must remain a real directory, never an Agent's link.
        self._layout.root.mkdir(parents=True, exist_ok=True)
        for directory in self._root_directories():
            _ensure_directory(directory)

    def _has_root(self) -> bool:
        for directory in self._root_directories():
            status = stat_or_none(directory, follow_symlinks=False)
            if status is None:
                return False
            _require_directory(directory, status)
        return True

    def _folder_for(self, address: SessionAddress) -> ScratchFolder:
        resolved = self._resolve(address)
        if resolved is None:
            raise ValueError("Scratch folder requires a live Session")
        own_id, root_id = resolved
        trash = None
        with self._area_lock(root_id):
            self._ensure_root()
            area = self.root / root_id
            # A restored Session must never adopt files whose archive removal
            # Windows delayed because a process still had one of them open.
            if self._pending_retirement(area):
                trash = self._retire_archived(area)
            _ensure_directory(area)
            own = area / own_id
            recreated = _ensure_directory(own)
            incarnation, new_incarnation = _incarnation_for(own)
            tmp = own / "tmp"
            _ensure_directory(tmp)
            self._touch(area)
            folder = ScratchFolder(own, area, tmp, recreated or new_incarnation, incarnation)
        if trash is not None:
            try:
                self._discard(trash)
            except OSError as error:
                _warn(root_id, error)
        return folder

    def _touch(self, area: Path) -> None:
        marker = area / ".activity"
        _ensure_regular_file(marker)
        now = self._clock()
        if os.utime in os.supports_follow_symlinks:
            os.utime(marker, (now, now), follow_symlinks=False)
        else:
            # Windows has neither descriptor nor no-follow utime support. The
            # marker was just verified as a regular file under this area's lock.
            os.utime(marker, (now, now))

    def _roots_for_archive(self, addresses: tuple[SessionAddress, ...]) -> tuple[str, ...]:
        roots: set[str] = set()
        for address in addresses:
            resolved = self._resolve(address)
            if resolved is not None and resolved[0] == resolved[1]:
                roots.add(resolved[0])
        return tuple(sorted(roots))

    def _retire(self, area: Path, *, activity_epoch: int | None = None) -> Path | None:
        if activity_epoch is not None:
            with self._activity_lock:
                if activity_epoch != self._activity_epoch:
                    return None
        if stat_or_none(area, follow_symlinks=False) is None:
            return None
        trash = self.root / f".trash-{uuid.uuid4().hex}"
        area.rename(trash)
        if activity_epoch is not None:
            with self._activity_lock:
                unchanged = activity_epoch == self._activity_epoch
            if not unchanged:
                # The caller still holds this area's lock: folder_for cannot
                # create a replacement before the original is back in place.
                # Admission never waits for either rename on the Event Loop.
                trash.rename(area)
                return None
        return trash

    def _discard(self, path: Path) -> None:
        # Shared tree removal handles symbolic links, junctions and read-only
        # files, and verifies the resolved target stays inside this Scratch root.
        remove_tree(path, within=self.root)

    def _pending_retirement(self, area: Path) -> bool:
        marker = self.root / f"{_RETIRE_PREFIX}{area.name}"
        status = stat_or_none(marker, follow_symlinks=False)
        if status is None:
            return False
        _require_regular(marker, status)
        return True

    def _retire_archived(self, area: Path) -> Path | None:
        # This marker survives a failed rename as well as a process restart. It
        # is outside the area because an open descendant can block its rename.
        # Clear it before allowing any replacement area to be created.
        marker = self.root / f"{_RETIRE_PREFIX}{area.name}"
        _ensure_regular_file(marker)
        trash = self._retire(area)
        marker.unlink()
        return trash

    def _remove_areas(self, roots: tuple[str, ...]) -> None:
        for root_id in roots:
            try:
                with self._area_lock(root_id):
                    if not self._has_root():
                        continue
                    trash = self._retire_archived(self.root / root_id)
                if trash is not None:
                    self._discard(trash)
                    _LOGGER.debug("Scratch area removed (root=%s reason=archive)", root_id)
            except OSError as error:
                _warn(root_id, error)

    def _sweep(
        self, busy: tuple[SessionAddress, ...], stopping: Event | None, activity_epoch: int
    ) -> int:
        with self._sweep_lock:
            return self._sweep_locked(busy, stopping, activity_epoch)

    def _sweep_locked(
        self, busy: tuple[SessionAddress, ...], stopping: Event | None, activity_epoch: int
    ) -> int:
        # Resolve the entire snapshot before any deletion. A Session-store failure
        # must never turn an unknown busy tree into an apparently inactive one.
        busy_roots: set[str] = set()
        for address in busy:
            resolved = self._resolve(address)
            if resolved is not None:
                busy_roots.add(resolved[1])
        try:
            if not self._has_root():
                return 0
            with os.scandir(self.root) as listing:
                names = [entry.name for entry in listing]
        except OSError as error:
            _warn(None, error)
            return 0
        removed = 0
        visited: set[str] = set()
        cutoff = self._clock() - SCRATCH_RETENTION.total_seconds()
        for name in names:
            if stopping is not None and stopping.is_set():
                break
            try:
                candidate = self.root / name
                if _TRASH.fullmatch(name):
                    self._discard(candidate)
                    removed += 1
                    continue
                root_id = name.removeprefix(_RETIRE_PREFIX)
                if not is_safe_id(root_id) or root_id in visited:
                    continue
                visited.add(root_id)
                candidate = self.root / root_id
                with self._area_lock(root_id):
                    if self._pending_retirement(candidate):
                        trash = self._retire_archived(candidate)
                    else:
                        status = stat_or_none(candidate, follow_symlinks=False)
                        if status is None:
                            continue
                        if root_id in busy_roots:
                            _require_directory(candidate, status)
                            self._touch(candidate)
                            continue
                        # Links are entries, never a place to inspect a marker through.
                        if stat.S_ISDIR(status.st_mode) and not is_link_status(status):
                            marker = stat_or_none(candidate / ".activity", follow_symlinks=False)
                            if marker is not None:
                                _require_regular(candidate / ".activity", marker)
                                status = marker
                        if status.st_mtime >= cutoff:
                            continue
                        trash = self._retire(candidate, activity_epoch=activity_epoch)
                if trash is not None:
                    self._discard(trash)
                    removed += 1
                    _LOGGER.debug("Scratch area removed (root=%s reason=cleanup)", root_id)
            except OSError as error:
                root_id = name.removeprefix(_RETIRE_PREFIX)
                _warn(root_id if is_safe_id(root_id) else None, error)
        if removed:
            _LOGGER.info("Scratch areas removed (count=%d)", removed)
        return removed

    async def _sweep_loop(self) -> None:
        try:
            while not self._stopping.is_set():
                try:
                    busy, epoch = self._activity_snapshot()
                    await _WORKERS.run(self._sweep, busy, self._stopping, epoch)
                except OSError as error:
                    _warn(None, error)
                except VBotError as error:
                    _LOGGER.warning("Scratch sweep failed (error=%s)", type(error).__name__)
                except Exception as error:
                    # Keep periodic cleanup alive after a transient lookup failure.
                    # Its exception text/traceback can expose filenames inside areas.
                    _LOGGER.error("Scratch sweep failed (error=%s)", type(error).__name__)
                await asyncio.sleep(self._sweep_interval_seconds)
        except asyncio.CancelledError:
            return


def _validate_id(identifier: str) -> None:
    if not is_safe_id(identifier):
        raise ValueError("Scratch Session identity must be a safe filesystem basename")


def _require_directory(path: Path, status: os.stat_result) -> None:
    if is_link_status(status) or not stat.S_ISDIR(status.st_mode):
        raise OSError(errno.ENOTDIR, "Scratch path is not a real directory", str(path))


def _require_regular(path: Path, status: os.stat_result) -> None:
    if is_link_status(status) or not stat.S_ISREG(status.st_mode):
        raise OSError(errno.EINVAL, "Scratch marker is not a regular file", str(path))


def _ensure_directory(path: Path) -> bool:
    status = stat_or_none(path, follow_symlinks=False)
    created = status is None
    if created:
        try:
            path.mkdir()
        except FileExistsError:
            created = False
        status = path.lstat()
    assert status is not None
    _require_directory(path, status)
    return created


def _ensure_regular_file(path: Path) -> None:
    try:
        with path.open("xb"):
            pass
    except FileExistsError:
        pass
    _require_regular(path, path.lstat())


def _incarnation_for(own: Path) -> tuple[str, bool]:
    marker = own / ".incarnation"
    token = uuid.uuid4().hex
    try:
        with marker.open("x", encoding="ascii", newline="\n") as stream:
            stream.write(token + "\n")
        return token, True
    except FileExistsError:
        _require_regular(marker, marker.lstat())
        token = marker.read_text(encoding="ascii").strip()
        if _INCARNATION.fullmatch(token) is None:
            raise OSError(
                errno.EINVAL, "Scratch incarnation marker is invalid", str(marker)
            ) from None
        return token, False


def _warn(root_id: str | None, error: OSError) -> None:
    # Exception text can contain an Agent-created filename; log only safe facts.
    _LOGGER.warning(
        "Scratch cleanup failed (root=%s error=%s errno=%s)",
        root_id or "unknown",
        type(error).__name__,
        error.errno,
    )


__all__ = ["SCRATCH_RETENTION", "SCRATCH_SWEEP_INTERVAL_SECONDS", "ScratchAreas", "ScratchFolder"]
