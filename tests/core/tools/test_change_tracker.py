"""Tests for the session-scoped file-content change tracker (git-style stats)."""

from __future__ import annotations

from pathlib import Path

from core.tools.change_tracker import MAX_TRACKED_BYTES, ChangeTracker


def test_repeated_edit_of_same_line_counts_once(tmp_path: Path) -> None:
    tracker = ChangeTracker()
    target = tmp_path / "a.txt"
    start = "line1\nline2\nline3\n"

    tracker.record_write("session-1", target, start, "line1\nline2b\nline3\n")
    tracker.record_write("session-1", target, "line1\nline2b\nline3\n", "line1\nline2c\nline3\n")

    stats = tracker.take_run_stats("session-1")
    assert stats is not None
    assert stats["files"] == 1
    assert stats["added"] == 1
    assert stats["removed"] == 1
    assert stats["paths"] == [str(target)]


def test_full_rewrite_counts_only_real_delta() -> None:
    tracker = ChangeTracker()

    tracker.record_write("session-1", Path("a.txt"), "keep\nold\nkeep\n", "keep\nnew\nkeep\n")

    stats = tracker.take_run_stats("session-1")
    assert stats is not None
    assert stats["added"] == 1
    assert stats["removed"] == 1


def test_external_intermediate_changes_are_not_attributed_to_the_run() -> None:
    """A formatter/shell rewrite between two mutations must not inflate stats.

    The first mutation stores its own pre-state; later mutations of the same
    path deduplicate against it, so content that changed out of band between
    them does not show up as run delta.
    """
    tracker = ChangeTracker()
    target = Path("a.txt")

    # Run 1: agent edits one line; afterwards an external formatter rewrites more.
    tracker.record_write("session-1", target, "a\nb\nc\n", "a\nB\nc\n")
    assert tracker.take_run_stats("session-1") == {
        "files": 1,
        "added": 1,
        "removed": 1,
        "paths": [str(target)],
    }

    # Run 2 starts from whatever is on disk now (formatter output included):
    # only the agent's own delta may be reported, not the formatter churn.
    disk_after_formatter = "A\nB\nc\n"  # external churn: first line reformatted
    tracker.record_write("session-2", target, disk_after_formatter, "A\nB2\nc\n")
    assert tracker.take_run_stats("session-2") == {
        "files": 1,
        "added": 1,
        "removed": 1,
        "paths": [str(target)],
    }


def test_new_file_counts_whole_content_as_added() -> None:
    tracker = ChangeTracker()
    target = Path("new.txt")

    tracker.record_write("session-1", target, "", "x\ny\n")

    stats = tracker.take_run_stats("session-1")
    assert stats is not None
    assert stats["files"] == 1
    assert stats["added"] == 2
    assert stats["removed"] == 0


def test_unchanged_write_produces_no_stats() -> None:
    tracker = ChangeTracker()
    target = Path("a.txt")

    tracker.record_write("session-1", target, "same\n", "same\n")

    assert tracker.take_run_stats("session-1") is None


def test_stats_are_consumed_per_run() -> None:
    tracker = ChangeTracker()
    target = Path("a.txt")
    tracker.record_write("session-1", target, "a\n", "b\n")

    assert tracker.take_run_stats("session-1") is not None
    assert tracker.take_run_stats("session-1") is None


def test_peek_returns_totals_without_consuming_them() -> None:
    tracker = ChangeTracker()
    target = Path("a.txt")
    tracker.record_write("session-1", target, "a\n", "b\nc\n")

    first = tracker.peek_run_stats("session-1")
    second = tracker.peek_run_stats("session-1")

    assert first == second
    assert first == {"files": 1, "added": 2, "removed": 1, "paths": [str(target)]}

    stats = tracker.take_run_stats("session-1")
    assert stats == {"files": 1, "added": 2, "removed": 1, "paths": [str(target)]}
    assert tracker.peek_run_stats("session-1") is None


def test_peek_reports_explicit_zero_when_changes_revert_to_baseline() -> None:
    tracker = ChangeTracker()
    target = Path("a.txt")
    tracker.record_write("session-1", target, "a\n", "b\n")
    tracker.record_write("session-1", target, "b\n", "a\n")

    peeked = tracker.peek_run_stats("session-1")
    assert peeked == {"files": 0, "added": 0, "removed": 0, "paths": []}

    # The terminal contract stays unchanged: an all-zero outcome is no stats.
    assert tracker.take_run_stats("session-1") is None


def test_peek_returns_none_for_unknown_session() -> None:
    tracker = ChangeTracker()

    assert tracker.peek_run_stats("missing-session") is None


def test_sessions_are_isolated() -> None:
    tracker = ChangeTracker()
    target = Path("a.txt")
    tracker.record_write("session-1", target, "a\n", "b\n")

    assert tracker.take_run_stats("session-2") is None
    assert tracker.take_run_stats("session-1") is not None


def test_multiple_files_are_aggregated_and_sorted() -> None:
    tracker = ChangeTracker()
    first = Path("b.txt")
    second = Path("a.txt")
    tracker.record_write("session-1", first, "a\n", "b\n")
    tracker.record_write("session-1", second, "a\n", "c\n")

    stats = tracker.take_run_stats("session-1")
    assert stats is not None
    assert stats["files"] == 2
    assert stats["added"] == 2
    assert stats["paths"] == [str(second), str(first)]


def test_oversized_content_is_not_tracked() -> None:
    tracker = ChangeTracker()
    target = Path("big.txt")
    huge = "x\n" * (MAX_TRACKED_BYTES // 2 + 8)

    tracker.record_write("session-1", target, huge, huge + "extra\n")

    assert tracker.peek_run_stats("session-1") is None


def test_large_file_with_repeated_lines_diffs_like_git() -> None:
    """Auto-junking must stay off: repeated filler lines are real diff units.

    With SequenceMatcher's default heuristic, popular lines in sequences over
    200 items are treated as junk and a one-line change inflates into large
    replace blocks. Git reports exactly one added and one removed line here.
    """
    tracker = ChangeTracker()
    target = Path("long.txt")
    before = "\n".join(f"filler {index % 5}" for index in range(400)) + "\nunique line\n"
    after = before.replace("unique line\n", "changed line\n")

    tracker.record_write("session-1", target, before, after)

    stats = tracker.take_run_stats("session-1")
    assert stats is not None
    assert stats["added"] == 1
    assert stats["removed"] == 1
