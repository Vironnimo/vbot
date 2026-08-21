"""Tests for the session-scoped file-content change tracker (git-style stats)."""

from __future__ import annotations

from pathlib import Path

from core.tools.change_tracker import ChangeTracker


def test_repeated_edit_of_same_line_counts_once(tmp_path: Path) -> None:
    tracker = ChangeTracker()
    target = tmp_path / "a.txt"
    tracker.record_read("session-1", target, "line1\nline2\nline3\n")

    tracker.record_write("session-1", target, "line1\nline2\nline3\n", "line1\nline2b\nline3\n")
    tracker.record_write("session-1", target, "line1\nline2b\nline3\n", "line1\nline2c\nline3\n")

    stats = tracker.take_run_stats("session-1")
    assert stats is not None
    assert stats["files"] == 1
    assert stats["added"] == 1
    assert stats["removed"] == 1
    assert stats["paths"] == [str(target)]


def test_full_rewrite_diffs_against_read_baseline(tmp_path: Path) -> None:
    tracker = ChangeTracker()
    target = tmp_path / "a.txt"
    tracker.record_read("session-1", target, "keep\nold\nkeep\n")

    tracker.record_write("session-1", target, "keep\nold\nkeep\n", "keep\nnew\nkeep\n")

    stats = tracker.take_run_stats("session-1")
    assert stats is not None
    assert stats["added"] == 1
    assert stats["removed"] == 1


def test_new_file_counts_whole_content_as_added(tmp_path: Path) -> None:
    tracker = ChangeTracker()
    target = tmp_path / "new.txt"

    tracker.record_write("session-1", target, "", "x\ny\n")

    stats = tracker.take_run_stats("session-1")
    assert stats is not None
    assert stats["files"] == 1
    assert stats["added"] == 2
    assert stats["removed"] == 0


def test_unchanged_write_produces_no_stats(tmp_path: Path) -> None:
    tracker = ChangeTracker()
    target = tmp_path / "a.txt"
    tracker.record_read("session-1", target, "same\n")

    tracker.record_write("session-1", target, "same\n", "same\n")

    assert tracker.take_run_stats("session-1") is None


def test_stats_are_consumed_per_run(tmp_path: Path) -> None:
    tracker = ChangeTracker()
    target = tmp_path / "a.txt"
    tracker.record_read("session-1", target, "a\n")
    tracker.record_write("session-1", target, "a\n", "b\n")

    assert tracker.take_run_stats("session-1") is not None
    assert tracker.take_run_stats("session-1") is None


def test_sessions_are_isolated(tmp_path: Path) -> None:
    tracker = ChangeTracker()
    target = tmp_path / "a.txt"
    tracker.record_read("session-1", target, "a\n")
    tracker.record_write("session-1", target, "a\n", "b\n")

    assert tracker.take_run_stats("session-2") is None
    assert tracker.take_run_stats("session-1") is not None


def test_multiple_files_are_aggregated_and_sorted(tmp_path: Path) -> None:
    tracker = ChangeTracker()
    first = tmp_path / "b.txt"
    second = tmp_path / "a.txt"
    tracker.record_read("session-1", first, "a\n")
    tracker.record_read("session-1", second, "a\n")
    tracker.record_write("session-1", first, "a\n", "b\n")
    tracker.record_write("session-1", second, "a\n", "c\n")

    stats = tracker.take_run_stats("session-1")
    assert stats is not None
    assert stats["files"] == 2
    assert stats["added"] == 2
    assert stats["paths"] == [str(second), str(first)]
