"""Tests for shared atomic-write staging."""

import errno
import os
import stat
from pathlib import Path

import pytest

from core.storage.layout import DataDirectoryLayout
from core.utils import atomic
from core.utils.atomic import (
    atomic_write_bytes,
    atomic_write_stream,
    atomic_write_text,
    temporary_path,
    write_new_bytes,
)


class _WindowsPermissionError(PermissionError):
    """A ``PermissionError`` that reports a Windows error code, as only Windows does."""

    def __init__(self, winerror: int) -> None:
        super().__init__(errno.EACCES, "The process cannot access the file")
        self.winerror = winerror


def test_temporary_path_uses_canonical_atomic_directory(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"

    temporary = temporary_path(tmp_path, target)

    assert temporary.parent == DataDirectoryLayout(tmp_path).atomic_temporary


def test_atomic_write_cleans_staging_file_after_success(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    layout = DataDirectoryLayout(tmp_path)

    atomic_write_text(target, "{}\n", data_dir=tmp_path)

    assert target.read_text(encoding="utf-8") == "{}\n"
    assert list(layout.atomic_temporary.iterdir()) == []


@pytest.mark.durable
@pytest.mark.parametrize("write_text", [False, True])
def test_atomic_write_fsyncs_data_before_replace_and_directories_after(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    write_text: bool,
) -> None:
    target = tmp_path / "settings.json"
    events: list[str] = []
    real_replace = os.replace

    def record_fsync(descriptor: int) -> None:
        mode = os.fstat(descriptor).st_mode
        events.append("directory_fsync" if stat.S_ISDIR(mode) else "file_fsync")

    def record_replace(source: Path, destination: Path) -> None:
        events.append("replace")
        real_replace(source, destination)

    monkeypatch.setattr("core.utils.atomic.os.fsync", record_fsync)
    monkeypatch.setattr("core.utils.atomic.os.replace", record_replace)

    if write_text:
        atomic_write_text(target, "new\n", data_dir=tmp_path)
    else:
        atomic_write_bytes(target, b"new\n", data_dir=tmp_path)

    assert events[:2] == ["file_fsync", "replace"]
    assert all(event == "directory_fsync" for event in events[2:])
    assert len(events[2:]) == (2 if os.name == "posix" else 0)


@pytest.mark.durable
def test_write_new_bytes_fsyncs_the_file_then_its_directory_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "blob.bin"
    events: list[str] = []

    def record_fsync(descriptor: int) -> None:
        mode = os.fstat(descriptor).st_mode
        events.append("directory_fsync" if stat.S_ISDIR(mode) else "file_fsync")

    monkeypatch.setattr("core.utils.atomic.os.fsync", record_fsync)

    write_new_bytes(target, b"new")

    assert target.read_bytes() == b"new"
    assert events == ["file_fsync", *(["directory_fsync"] if os.name == "posix" else [])]


def test_write_new_bytes_never_replaces_and_removes_the_file_it_failed_to_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = tmp_path / "taken.bin"
    existing.write_bytes(b"old")

    with pytest.raises(FileExistsError):
        write_new_bytes(existing, b"new")

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("core.utils.atomic.os.fsync", fail_fsync)
    with pytest.raises(OSError, match="disk full"):
        write_new_bytes(tmp_path / "new.bin", b"new")

    assert existing.read_bytes() == b"old"
    assert [path.name for path in tmp_path.iterdir()] == ["taken.bin"]


def test_atomic_write_cleans_staging_file_after_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "settings.json"
    target.write_text("old\n", encoding="utf-8")
    layout = DataDirectoryLayout(tmp_path)

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("core.utils.atomic.os.replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        atomic_write_text(target, "new\n", data_dir=tmp_path)

    assert target.read_text(encoding="utf-8") == "old\n"
    assert list(layout.atomic_temporary.iterdir()) == []


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_atomic_write_outlasts_a_transient_windows_sharing_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, winerror: int
) -> None:
    target = tmp_path / "settings.json"
    target.write_text("old\n", encoding="utf-8")
    layout = DataDirectoryLayout(tmp_path)
    real_replace = os.replace
    sleeps: list[float] = []
    failures = [_WindowsPermissionError(winerror) for _ in range(3)]

    def hold_then_release(source: Path, destination: Path) -> None:
        if failures:
            raise failures.pop()
        real_replace(source, destination)

    monkeypatch.setattr("core.utils.atomic.os.replace", hold_then_release)
    monkeypatch.setattr(atomic, "_sleep", sleeps.append)

    atomic_write_text(target, "new\n", data_dir=tmp_path)

    assert target.read_text(encoding="utf-8") == "new\n"
    assert sleeps == list(atomic._REPLACE_RETRY_DELAYS[:3])
    assert list(layout.atomic_temporary.iterdir()) == []


def test_atomic_write_gives_up_after_its_short_retry_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "settings.json"
    target.write_text("old\n", encoding="utf-8")
    layout = DataDirectoryLayout(tmp_path)
    attempts: list[int] = []
    sleeps: list[float] = []

    def always_held(_source: Path, _destination: Path) -> None:
        attempts.append(1)
        raise _WindowsPermissionError(32)

    monkeypatch.setattr("core.utils.atomic.os.replace", always_held)
    monkeypatch.setattr(atomic, "_sleep", sleeps.append)

    with pytest.raises(_WindowsPermissionError):
        atomic_write_text(target, "new\n", data_dir=tmp_path)

    assert len(attempts) == len(atomic._REPLACE_RETRY_DELAYS) + 1
    assert sum(sleeps) < 1
    assert target.read_text(encoding="utf-8") == "old\n"
    assert list(layout.atomic_temporary.iterdir()) == []


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(PermissionError(errno.EACCES, "denied"), id="not-windows"),
        pytest.param(_WindowsPermissionError(3), id="other-windows-code"),
    ],
)
def test_atomic_write_does_not_retry_errors_that_are_not_sharing_violations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: PermissionError
) -> None:
    target = tmp_path / "settings.json"

    def refuse(_source: Path, _destination: Path) -> None:
        raise error

    monkeypatch.setattr("core.utils.atomic.os.replace", refuse)
    monkeypatch.setattr(atomic, "_sleep", lambda _delay: pytest.fail("must not wait"))

    with pytest.raises(PermissionError):
        atomic_write_text(target, "new\n")

    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="POSIX replaces files that other programs hold open")
def test_atomic_write_waits_for_a_reader_holding_the_target_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "settings.json"
    target.write_text("old\n", encoding="utf-8")
    reader = target.open("rb")  # no delete sharing, like a scanner or indexer
    waits: list[float] = []

    def release_reader(delay: float) -> None:
        waits.append(delay)
        reader.close()

    monkeypatch.setattr(atomic, "_sleep", release_reader)
    try:
        atomic_write_text(target, "new\n")
    finally:
        reader.close()

    assert target.read_text(encoding="utf-8") == "new\n"
    assert len(waits) == 1


@pytest.mark.skipif(os.name != "nt", reason="read-only attribute semantics of Windows")
def test_atomic_write_does_not_wait_for_a_read_only_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "settings.json"
    target.write_text("old\n", encoding="utf-8")
    target.chmod(stat.S_IREAD)
    monkeypatch.setattr(atomic, "_sleep", lambda _delay: pytest.fail("must not wait"))
    try:
        with pytest.raises(PermissionError):
            atomic_write_text(target, "new\n")
    finally:
        target.chmod(stat.S_IWRITE | stat.S_IREAD)

    assert target.read_text(encoding="utf-8") == "old\n"


@pytest.mark.parametrize("write_bytes", [False, True])
def test_atomic_write_applies_requested_mode_to_target(
    tmp_path: Path,
    write_bytes: bool,
) -> None:
    target = tmp_path / "control-record.json"

    if write_bytes:
        atomic_write_bytes(target, b"{}", mode=0o600)
    else:
        atomic_write_text(target, "{}", mode=0o600)

    assert target.read_bytes() == b"{}"
    if os.name == "posix":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_atomic_write_stream_replaces_target_with_streamed_chunks(tmp_path: Path) -> None:
    target = tmp_path / "trace.json"
    target.write_bytes(b"old")

    def write(handle) -> None:
        for chunk in (b"[", b"1,", b"2", b"]"):
            handle.write(chunk)

    atomic_write_stream(target, write)

    assert target.read_bytes() == b"[1,2]"
    assert [path.name for path in tmp_path.iterdir()] == ["trace.json"]


def test_atomic_write_stream_removes_staging_file_when_writer_fails(tmp_path: Path) -> None:
    target = tmp_path / "trace.json"
    target.write_bytes(b"old")

    def write(handle) -> None:
        handle.write(b"partial")
        raise TypeError("not serializable")

    with pytest.raises(TypeError, match="not serializable"):
        atomic_write_stream(target, write)

    assert target.read_bytes() == b"old"
    assert [path.name for path in tmp_path.iterdir()] == ["trace.json"]
