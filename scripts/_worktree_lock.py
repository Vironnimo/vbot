"""Portable merge locks and protected repair-window keeper lifecycle."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import random
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
        if os.name == "nt":
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
        if os.name == "nt":
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


def _write_holder_record(holder_path: Path, record: dict[str, object]) -> None:
    """Publish the current lock holder's identity and heartbeat."""
    holder_path.parent.mkdir(parents=True, exist_ok=True)
    holder_path.write_text(json.dumps(record) + "\n", encoding="utf-8")


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
        try:
            while time.time() < deadline:
                if release_path.exists():
                    break
                record["heartbeat"] = time.time()
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
