"""Tests for the per-session read-before-write / stale-file guard registry."""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

import pytest

import core.tools.file_state as file_state_module
from core.tools.file_state import (
    FileReadState,
    StaleReason,
    atomic_write_bytes,
)


def test_unread_existing_file_is_never_read(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    registry = FileReadState()

    assert registry.check_stale("session-1", target) is StaleReason.NEVER_READ


def test_recorded_read_is_not_stale(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    registry = FileReadState()

    registry.record_read("session-1", target)

    assert registry.check_stale("session-1", target) is None


def test_size_change_flags_modified(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("short", encoding="utf-8")
    registry = FileReadState()
    registry.record_read("session-1", target)

    target.write_text("a much longer body", encoding="utf-8")

    assert registry.check_stale("session-1", target) is StaleReason.MODIFIED


def test_mtime_change_with_same_size_flags_modified(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("abc", encoding="utf-8")
    registry = FileReadState()
    registry.record_read("session-1", target)

    # Same byte length, only the modification time moves forward.
    info = target.stat()
    os.utime(target, (info.st_atime, info.st_mtime + 5))

    assert registry.check_stale("session-1", target) is StaleReason.MODIFIED


def test_scope_is_per_session(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    registry = FileReadState()

    registry.record_read("session-1", target)

    # A different session has its own read history.
    assert registry.check_stale("session-2", target) is StaleReason.NEVER_READ


def test_record_read_restamps_after_a_change(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("abc", encoding="utf-8")
    registry = FileReadState()
    registry.record_read("session-1", target)

    target.write_text("abcdef", encoding="utf-8")
    assert registry.check_stale("session-1", target) is StaleReason.MODIFIED

    registry.record_read("session-1", target)
    assert registry.check_stale("session-1", target) is None


def test_disabled_guard_never_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(file_state_module, "FILE_STATE_GUARD_ENABLED", False)
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    registry = FileReadState()

    # No recording happens, and the never-read check returns clean.
    registry.record_read("session-1", target)
    assert registry.check_stale("session-1", target) is None


def test_eviction_caps_tracked_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(file_state_module, "_MAX_TRACKED_FILES", 2)
    registry = FileReadState()
    files = [tmp_path / f"f{index}.txt" for index in range(3)]
    for target in files:
        target.write_text("x", encoding="utf-8")
        registry.record_read("session-1", target)

    # The oldest insertion is evicted; the two most recent survive.
    assert registry.check_stale("session-1", files[0]) is StaleReason.NEVER_READ
    assert registry.check_stale("session-1", files[1]) is None
    assert registry.check_stale("session-1", files[2]) is None


def test_path_lock_serializes_same_path(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    registry = FileReadState()
    waiter_started = threading.Event()
    waiter_entered = threading.Event()

    def wait_for_path() -> None:
        waiter_started.set()
        with registry.lock_path(target):
            waiter_entered.set()

    with registry.lock_path(target):
        thread = threading.Thread(target=wait_for_path)
        thread.start()
        assert waiter_started.wait(timeout=1)
        assert waiter_entered.wait(timeout=0.05) is False

    assert waiter_entered.wait(timeout=1)
    thread.join(timeout=1)
    assert thread.is_alive() is False


def test_path_locks_for_different_files_do_not_block_each_other(tmp_path: Path) -> None:
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    registry = FileReadState()
    second_entered = threading.Event()

    def lock_second() -> None:
        with registry.lock_path(second):
            second_entered.set()

    with registry.lock_path(first):
        thread = threading.Thread(target=lock_second)
        thread.start()
        assert second_entered.wait(timeout=1)

    thread.join(timeout=1)
    assert thread.is_alive() is False


def test_atomic_write_replaces_target_and_preserves_mode(tmp_path: Path) -> None:
    file_root = tmp_path / "files"
    file_root.mkdir()
    target = file_root / "a.txt"
    target.write_bytes(b"before")
    target.chmod(0o640)
    original_mode = stat.S_IMODE(target.stat().st_mode)

    atomic_write_bytes(target, b"after")

    assert target.read_bytes() == b"after"
    assert stat.S_IMODE(target.stat().st_mode) == original_mode
    assert list(file_root.iterdir()) == [target]


def test_atomic_write_failure_keeps_original_and_removes_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_root = tmp_path / "files"
    file_root.mkdir()
    target = file_root / "a.txt"
    target.write_bytes(b"before")

    def fail_replace(_source: Path, _target: Path) -> None:
        raise PermissionError("replace denied")

    monkeypatch.setattr(file_state_module.os, "replace", fail_replace)

    with pytest.raises(PermissionError, match="replace denied"):
        atomic_write_bytes(target, b"after")

    assert target.read_bytes() == b"before"
    assert list(file_root.iterdir()) == [target]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
@pytest.mark.parametrize(("umask", "expected"), [(0o022, 0o644), (0o077, 0o600)])
def test_atomic_write_gives_new_files_the_umask_derived_mode(
    tmp_path: Path, umask: int, expected: int
) -> None:
    target = tmp_path / "new.txt"
    previous = os.umask(umask)
    try:
        atomic_write_bytes(target, b"x\n")
    finally:
        os.umask(previous)

    assert stat.S_IMODE(target.stat().st_mode) == expected
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.skipif(os.name != "nt", reason="Windows read-only attribute")
def test_read_only_target_fails_immediately_without_temporary_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "read-only.txt"
    target.write_bytes(b"before")
    target.chmod(stat.S_IREAD)
    monkeypatch.setattr(
        file_state_module.time, "sleep", lambda _delay: pytest.fail("read-only is not transient")
    )
    try:
        with pytest.raises(file_state_module.ReadOnlyFileError) as raised:
            atomic_write_bytes(target, b"after", before_replace=lambda: None)

        assert not hasattr(raised.value, "attempts_made")
        assert target.read_bytes() == b"before"
        assert list(tmp_path.iterdir()) == [target]
    finally:
        target.chmod(stat.S_IREAD | stat.S_IWRITE)


def test_failed_replacement_removes_a_read_only_temporary_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_replace(_source: Path, _target: Path) -> None:
        raise PermissionError("replace denied")

    monkeypatch.setattr(file_state_module.os, "replace", fail_replace)

    with pytest.raises(PermissionError, match="replace denied"):
        # A move destination receives the source's (here read-only) mode.
        atomic_write_bytes(tmp_path / "moved.txt", b"after", mode=stat.S_IREAD)

    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing handles")
def test_atomic_replace_recovers_from_a_real_reader_without_delete_sharing(tmp_path, monkeypatch):
    import ctypes
    from ctypes import wintypes

    target = tmp_path / "file.txt"
    target.write_bytes(b"before")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    # A watcher may allow other reads/writes while denying rename/delete.
    handle = create(str(target), 0x80000000, 3, None, 3, 0x80, None)
    assert handle != ctypes.c_void_p(-1).value
    releases = []
    checks = []

    def release_reader(delay):
        nonlocal handle
        releases.append(delay)
        assert close(handle)
        handle = None

    def check_before_replace():
        checks.append(target.read_bytes())
        assert checks[-1] == b"before"

    monkeypatch.setattr(file_state_module.time, "sleep", release_reader)
    try:
        atomic_write_bytes(target, b"after", before_replace=check_before_replace)
    finally:
        if handle is not None:
            close(handle)
    assert len(releases) == 1 and checks == [b"before", b"before"]
    assert target.read_bytes() == b"after"
    assert list(tmp_path.iterdir()) == [target]
