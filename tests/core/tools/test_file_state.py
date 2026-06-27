"""Tests for the per-session read-before-write / stale-file guard registry."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import core.tools.file_state as file_state_module
from core.tools.file_state import FileReadState, StaleReason, stale_failure_text


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


def test_stale_failure_text_maps_reason_to_code() -> None:
    never_code, never_message = stale_failure_text(StaleReason.NEVER_READ, Path("a.txt"))
    assert never_code == "file_not_read"
    assert "read it first" in never_message.lower()

    modified_code, modified_message = stale_failure_text(StaleReason.MODIFIED, Path("a.txt"))
    assert modified_code == "file_modified_since_read"
    assert "read it again" in modified_message.lower()
