"""Portable merge locks and protected repair-window keeper lifecycle.

A repair window's keeper process holds the merge lock for one task until its
deadline. A merge of that task holds the window's lease while it lands; the keeper
then holds the merge lock on past the deadline until the lease is released, so the
window cannot end under the merge.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import random
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

KIND_MERGE = "merge"


KIND_REPAIR = "repair"


DEFAULT_MERGE_WAIT_TIMEOUT_SECONDS = 30 * 60


DEFAULT_REPAIR_WINDOW_SECONDS = 15 * 60


MERGE_LOCK_POLL_MIN_SECONDS = 0.4


MERGE_LOCK_POLL_MAX_SECONDS = 1.2


KEEPER_POLL_SECONDS = 1.0


HOLDER_FRESHNESS_SECONDS = 15.0


RELEASE_SHUTDOWN_TIMEOUT_SECONDS = 10.0


# A keeper confirms a merge's lease within one poll; a keeper started before leases
# existed never does.
LEASE_CONFIRMATION_TIMEOUT_SECONDS = 10.0


LEASE_SUFFIX = ".lease"


class MergeLockBusyError(Exception):
    """Raised when the merge lock stayed busy longer than the wait timeout."""


def _acquire_file_lock(lock_file) -> bool:
    """Try to take the exclusive advisory lock without blocking."""
    lock_file.seek(0, os.SEEK_END)
    if lock_file.tell() == 0:
        lock_file.write(b"0")
        lock_file.flush()
    lock_file.seek(0)
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl = importlib.import_module("fcntl")

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _release_file_lock(lock_file) -> None:
    """Release the advisory lock taken by `_acquire_file_lock`."""
    lock_file.seek(0)
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl = importlib.import_module("fcntl")

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def _probe_lock_is_busy(lock_path: Path) -> bool:
    """Return whether another process currently holds the merge lock."""
    with lock_path.open("a+b") as lock_file:
        busy = not _acquire_file_lock(lock_file)
        if not busy:
            _release_file_lock(lock_file)
    return busy


def _lease_path(lock_path: Path) -> Path:
    """Return the lease a merge holds to keep its task's repair window open until it is done."""
    return lock_path.with_name(lock_path.name + LEASE_SUFFIX)


def _acquire_within(lock_file, timeout_seconds: float) -> bool:
    """Try to take an advisory lock until the timeout passes."""
    deadline = time.monotonic() + timeout_seconds
    while not _acquire_file_lock(lock_file):
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.05, timeout_seconds))
    return True


@contextmanager
def _held_file_lock(lock_path: Path) -> Iterator[None]:
    """Hold an OS file lock, waiting for it as long as it takes."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        while not _acquire_file_lock(lock_file):
            time.sleep(random.uniform(MERGE_LOCK_POLL_MIN_SECONDS, MERGE_LOCK_POLL_MAX_SECONDS))
        try:
            yield
        finally:
            _release_file_lock(lock_file)


def _write_holder_record(holder_path: Path, record: dict[str, object]) -> None:
    """Publish the current lock holder's identity and heartbeat.

    The record is replaced in one step, so a reader never sees a partial one.
    Raises OSError when it cannot be replaced, as on Windows while a reader has it open.
    """
    holder_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = holder_path.with_name(
        f"{holder_path.name}.{os.getpid()}-{threading.get_ident()}.tmp"
    )
    try:
        temporary.write_text(json.dumps(record) + "\n", encoding="utf-8")
        os.replace(temporary, holder_path)
    except OSError:
        with suppress(OSError):
            temporary.unlink()
        raise


def _read_holder_record(holder_path: Path) -> dict[str, object] | None:
    """Read a lock holder record, tolerating absence or corruption."""
    try:
        data = json.loads(holder_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _holder_record_is_current(record: dict[str, object] | None) -> bool:
    """Return whether a holder record has a recent heartbeat."""
    if record is None:
        return False
    heartbeat = record.get("heartbeat")
    return isinstance(heartbeat, (int, float)) and (
        time.time() - heartbeat <= HOLDER_FRESHNESS_SECONDS
    )


def _own_repair_window_is_active(holder_path: Path, task: str) -> bool:
    """Return whether a fresh repair window is held for this exact task."""
    record = _read_holder_record(holder_path)
    if record is None or not _holder_record_is_current(record):
        return False
    return bool(record.get("kind") == KIND_REPAIR and record.get("task") == task)


def _repair_window_stays_open(holder_path: Path, task: str) -> bool:
    """Return whether this task's repair window is open and its deadline still ahead.

    A window past its deadline stays open only while a merge of the task holds its
    lease, so it ends when that merge is done.
    """
    if not _own_repair_window_is_active(holder_path, task):
        return False
    record = _read_holder_record(holder_path) or {}
    deadline = record.get("deadline")
    return not isinstance(deadline, (int, float)) or deadline > time.time()


def _repair_window_is_extended(lock_path: Path, holder_path: Path, task: str) -> bool:
    """Return whether this task's keeper still holds the merge lock for the merge's lease."""
    record = _read_holder_record(holder_path)
    return bool(
        _own_repair_window_is_active(holder_path, task)
        and record is not None
        and record.get("extended") is True
        and _probe_lock_is_busy(lock_path)
    )


def _await_lease_confirmation(lock_path: Path, holder_path: Path, task: str, since: float) -> bool:
    """Wait until the task's keeper records that it holds the lock for the lease.

    False once the lock is free or held for something else, or after the timeout.
    """
    deadline = time.monotonic() + LEASE_CONFIRMATION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not _probe_lock_is_busy(lock_path):
            return False
        record = _read_holder_record(holder_path)
        if record is not None:
            if record.get("kind") != KIND_REPAIR or record.get("task") != task:
                return False
            heartbeat = record.get("heartbeat")
            if (
                record.get("extended") is True
                and isinstance(heartbeat, (int, float))
                and heartbeat >= since
            ):
                return True
        time.sleep(min(KEEPER_POLL_SECONDS, 0.2))
    return False


@contextmanager
def _extended_repair_window(lock_path: Path, holder_path: Path, task: str) -> Iterator[bool]:
    """Hold this task's repair-window lease; yield whether its keeper confirmed it.

    While the lease is held, the keeper holds the merge lock past the window's
    deadline. False when the window ended before the keeper confirmed, when its keeper
    predates leases, or when another merge of the task holds the lease.
    """
    lease = _lease_path(lock_path)
    lease.parent.mkdir(parents=True, exist_ok=True)
    with lease.open("a+b") as lease_file:
        since = time.time()
        # The keeper takes the lease for an instant each time it probes it.
        if not _acquire_within(lease_file, 2 * KEEPER_POLL_SECONDS + 0.5):
            yield False
            return
        try:
            yield _await_lease_confirmation(lock_path, holder_path, task, since)
        finally:
            _release_file_lock(lease_file)


@contextmanager
def _merge_exclusive_lock(
    *,
    task: str,
    kind: str,
    timeout_seconds: float,
    lock_path: Path,
    holder_path: Path,
) -> Iterator[None]:
    """Hold the cross-process merge lock, waiting up to the timeout."""
    deadline = time.monotonic() + timeout_seconds
    lock_file = lock_path.open("a+b")
    while not _acquire_file_lock(lock_file):
        if time.monotonic() >= deadline:
            lock_file.close()
            raise MergeLockBusyError(
                f"merge lock stayed busy for {int(timeout_seconds)}s "
                "(another merge or protected repair window is running)"
            )
        time.sleep(random.uniform(MERGE_LOCK_POLL_MIN_SECONDS, MERGE_LOCK_POLL_MAX_SECONDS))

    # The record only names the holder to others; the lock is what counts.
    with suppress(OSError):
        _write_holder_record(
            holder_path,
            {
                "task": task,
                "kind": kind,
                "pid": os.getpid(),
                "started_at": time.time(),
                "heartbeat": time.time(),
                "deadline": None,
            },
        )
    try:
        yield
    finally:
        with suppress(OSError):
            holder_path.unlink()
        _release_file_lock(lock_file)
        lock_file.close()


def _request_window_release(release_path: Path, holder_path: Path, lock_path: Path) -> bool:
    """Signal the repair keeper to exit and wait until the lock is free."""
    release_path.parent.mkdir(parents=True, exist_ok=True)
    release_path.write_text("release\n", encoding="utf-8")
    deadline = time.monotonic() + RELEASE_SHUTDOWN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        record = _read_holder_record(holder_path)
        if not _holder_record_is_current(record) or not _probe_lock_is_busy(lock_path):
            with suppress(OSError):
                holder_path.unlink()
            return True
        time.sleep(0.2)
    if not _probe_lock_is_busy(lock_path):
        with suppress(OSError):
            holder_path.unlink()
        return True
    return False


def cmd_keeper_hold(args: argparse.Namespace) -> int:
    """Internal keeper process holding the lock for a protected repair window."""
    lock_path = Path(args.lock_path)
    holder_path = Path(args.holder_path)
    release_path = Path(args.release_path)
    deadline = float(args.deadline)

    record: dict[str, object] = {
        "task": args.task,
        "kind": KIND_REPAIR,
        "pid": os.getpid(),
        "started_at": time.time(),
        "deadline": deadline,
    }
    lock_file = lock_path.open("a+b")
    try:
        while not _acquire_file_lock(lock_file):
            if time.time() >= deadline:
                return 1
            time.sleep(random.uniform(MERGE_LOCK_POLL_MIN_SECONDS, MERGE_LOCK_POLL_MAX_SECONDS))
        lease = _lease_path(lock_path)
        try:
            while not release_path.exists():
                # A merge of this task that holds the lease keeps the window open.
                extended = _probe_lock_is_busy(lease)
                now = time.time()
                if now >= deadline and not extended:
                    break
                record["heartbeat"] = now
                record["extended"] = extended
                # A reader holding the record open delays the heartbeat by one poll.
                with suppress(OSError):
                    _write_holder_record(holder_path, record)
                time.sleep(KEEPER_POLL_SECONDS)
        finally:
            with suppress(OSError):
                holder_path.unlink()
    finally:
        _release_file_lock(lock_file)
        lock_file.close()

    with suppress(OSError):
        release_path.unlink()
    return 0
