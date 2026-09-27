"""The per-session read-before-write guard, path locks, and atomic file replacement."""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

import pytest

import core.tools.file_state as file_state_module
from core.tools.file_state import (
    FileReadState,
    ReadOnlyFileError,
    StaleReason,
    atomic_write_bytes,
    os_error_reason,
)


def test_a_session_may_write_only_what_it_read_since_the_last_change(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("abc", encoding="utf-8")
    registry = FileReadState()
    assert registry.check_stale("session-1", target) is StaleReason.NEVER_READ

    registry.record_read("session-1", target)
    assert registry.check_stale("session-1", target) is None
    # Another session has its own read history.
    assert registry.check_stale("session-2", target) is StaleReason.NEVER_READ

    target.write_text("abcdef", encoding="utf-8")
    assert registry.check_stale("session-1", target) is StaleReason.MODIFIED

    registry.record_read("session-1", target)
    assert registry.check_stale("session-1", target) is None

    # Same byte length, only the modification time moves forward.
    info = target.stat()
    os.utime(target, (info.st_atime, info.st_mtime + 5))
    assert registry.check_stale("session-1", target) is StaleReason.MODIFIED


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


def test_path_locks_serialize_one_path_but_not_different_paths(tmp_path: Path) -> None:
    registry = FileReadState()
    other_entered = threading.Event()
    same_started = threading.Event()
    same_entered = threading.Event()

    def enter(path: Path, started: threading.Event | None, entered: threading.Event) -> None:
        if started is not None:
            started.set()
        with registry.lock_path(path):
            entered.set()

    other = threading.Thread(target=enter, args=(tmp_path / "b.txt", None, other_entered))
    same = threading.Thread(target=enter, args=(tmp_path / "a.txt", same_started, same_entered))
    with registry.lock_path(tmp_path / "a.txt"):
        other.start()
        assert other_entered.wait(timeout=1)
        same.start()
        assert same_started.wait(timeout=1)
        assert same_entered.wait(timeout=0.05) is False

    assert same_entered.wait(timeout=1)
    for thread in (other, same):
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


# A move destination receives the source's (here read-only) mode, so its
# temporary copy is read-only too and must still be removed.
@pytest.mark.parametrize(("existing", "mode"), [(True, None), (False, stat.S_IREAD)])
def test_failed_replacement_keeps_the_target_and_removes_the_temporary_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool, mode: int | None
) -> None:
    target = tmp_path / "a.txt"
    if existing:
        target.write_bytes(b"before")

    def fail_replace(_source: Path, _target: Path) -> None:
        raise PermissionError("replace denied")

    monkeypatch.setattr(file_state_module.os, "replace", fail_replace)

    with pytest.raises(PermissionError, match="replace denied"):
        atomic_write_bytes(target, b"after", mode=mode)

    assert list(tmp_path.iterdir()) == ([target] if existing else [])
    if existing:
        assert target.read_bytes() == b"before"


@pytest.mark.parametrize(
    ("code", "precondition", "attempts", "reason"),
    [
        (5, True, 7, "permission denied"),
        (32, True, 7, "another program is using it"),
        (33, True, 7, "another program has locked part of it"),
        # Other failures, or a caller without a precondition check, fail at once.
        (28, True, 1, "permission denied"),
        (32, False, 1, "another program is using it"),
    ],
)
def test_replace_retries_only_windows_sharing_failures_behind_a_precondition_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    code: int,
    precondition: bool,
    attempts: int,
    reason: str,
) -> None:
    target = tmp_path / "a.txt"
    target.write_bytes(b"before")
    replaces: list[object] = []
    checks: list[object] = []
    sleeps: list[float] = []

    def busy_replace(_source: Path, destination: Path) -> None:
        replaces.append(destination)
        error = PermissionError(13, "Der Prozess kann nicht zugreifen", str(destination))
        error.winerror = code  # type: ignore[attr-defined]
        raise error

    monkeypatch.setattr(file_state_module.os, "replace", busy_replace)
    monkeypatch.setattr(file_state_module.time, "sleep", sleeps.append)
    check = (lambda: checks.append(target.read_bytes())) if precondition else None

    with pytest.raises(OSError) as raised:
        atomic_write_bytes(target, b"after", before_replace=check)

    assert len(replaces) == attempts
    assert len(checks) == (attempts if precondition else 0)
    assert len(sleeps) == attempts - 1 and sum(sleeps) < 2
    assert getattr(raised.value, "attempts_made", None) == (attempts if attempts > 1 else None)
    if attempts > 1 and os.name != "nt":
        # Only Windows keeps a Windows error code on a wrapped error.
        reason = "permission denied"
    assert os_error_reason(raised.value) == reason
    assert target.read_bytes() == b"before"
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
        with pytest.raises(ReadOnlyFileError) as raised:
            atomic_write_bytes(target, b"after", before_replace=lambda: None)

        assert not hasattr(raised.value, "attempts_made")
        assert os_error_reason(raised.value) == "the file is read-only"
        assert target.read_bytes() == b"before"
        assert list(tmp_path.iterdir()) == [target]
    finally:
        target.chmod(stat.S_IREAD | stat.S_IWRITE)


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


def _windows_error(code: int) -> OSError:
    error = OSError(13, "Der Prozess kann nicht zugreifen", "C:/abs/a.txt")
    error.winerror = code  # type: ignore[attr-defined]
    return error


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        # Windows reports its messages in the system language and names the absolute path.
        (PermissionError(13, "Zugriff verweigert", "C:/abs/a.txt"), "permission denied"),
        (OSError(28, "Nicht genug Speicher", "C:/abs/a.txt"), "the disk is full"),
        (_windows_error(32), "another program is using it"),
        (_windows_error(123), "the path contains characters that are not allowed in file names"),
        (OSError("the call was cancelled"), "the call was cancelled"),
    ],
)
def test_os_error_reason_is_english_and_names_no_absolute_path(error: OSError, reason: str) -> None:
    assert os_error_reason(error) == reason
