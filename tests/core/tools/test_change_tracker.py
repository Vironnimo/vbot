"""Run-scoped file-content change tracker (git-style stats)."""

from __future__ import annotations

from pathlib import Path

import pytest

import core.tools.change_tracker as change_tracker_module
from core.sessions import SessionAddress
from core.tools.change_tracker import MAX_TRACKED_BYTES, ChangeTracker


def _key(session_id: str) -> tuple[SessionAddress, str]:
    return SessionAddress(None, "agent", session_id), "run-one"


def _stats(*file_stats: tuple[str, int, int]) -> dict[str, object]:
    """Expected Run statistics from ``(path, added, removed)`` per changed file."""
    return {
        "files": len(file_stats),
        "added": sum(added for _path, added, _removed in file_stats),
        "removed": sum(removed for _path, _added, removed in file_stats),
        "paths": [path for path, _added, _removed in file_stats],
        "file_stats": [
            {"path": path, "added": added, "removed": removed}
            for path, added, removed in file_stats
        ],
    }


# Repeated filler lines are real diff units: SequenceMatcher's auto-junk heuristic
# would inflate this one-line change into large replace blocks; git reports 1/1.
_LONG = "\n".join(f"filler {index % 5}" for index in range(400)) + "\nunique line\n"


@pytest.mark.parametrize(
    ("writes", "expected"),
    [
        # Repeated edits of one line count once against the Run's first pre-state.
        (
            [
                ("a.txt", "line1\nline2\nline3\n", "line1\nline2b\nline3\n"),
                ("a.txt", "line1\nline2b\nline3\n", "line1\nline2c\nline3\n"),
            ],
            _stats(("a.txt", 1, 1)),
        ),
        ([("a.txt", "keep\nold\nkeep\n", "keep\nnew\nkeep\n")], _stats(("a.txt", 1, 1))),
        ([("new.txt", "", "x\ny\n")], _stats(("new.txt", 2, 0))),
        ([("a.txt", "a\n", "b\nc\n")], _stats(("a.txt", 2, 1))),
        ([("long.txt", _LONG, _LONG.replace("unique", "changed"))], _stats(("long.txt", 1, 1))),
        # Several files aggregate, each keeping its own counts, in path order.
        (
            [("b.txt", "a\n", "b\nc\n"), ("a.txt", "a\n", "c\n")],
            _stats(("a.txt", 1, 1), ("b.txt", 2, 1)),
        ),
        # Unchanged or reverted writes report an explicit zero, not None.
        ([("a.txt", "same\n", "same\n")], _stats()),
        ([("a.txt", "a\n", "b\n"), ("a.txt", "b\n", "a\n")], _stats()),
    ],
)
def test_stats_are_the_net_line_diff_of_each_file(
    writes: list[tuple[str, str, str]], expected: dict[str, object]
) -> None:
    tracker = ChangeTracker()
    for path, before, after in writes:
        tracker.record_write(_key("session-1"), Path(path), before, after)

    # Peeking streams live totals without consuming them; taking consumes them once.
    assert tracker.peek_run_stats(_key("session-1")) == expected
    assert tracker.peek_run_stats(_key("session-1")) == expected
    assert tracker.take_run_stats(_key("session-1")) == expected
    assert tracker.peek_run_stats(_key("session-1")) is None
    assert tracker.take_run_stats(_key("session-1")) is None


def test_external_intermediate_changes_are_not_attributed_to_the_run() -> None:
    """A formatter or shell rewrite between two Runs must not inflate the second Run's stats."""
    tracker = ChangeTracker()
    target = Path("a.txt")

    tracker.record_write(_key("session-1"), target, "a\nb\nc\n", "a\nB\nc\n")
    assert tracker.take_run_stats(_key("session-1")) == _stats(("a.txt", 1, 1))

    # The next Run starts from the formatter's output on disk: only its own delta counts.
    tracker.record_write(_key("session-2"), target, "A\nB\nc\n", "A\nB2\nc\n")
    assert tracker.take_run_stats(_key("session-2")) == _stats(("a.txt", 1, 1))


def test_oversized_content_is_not_tracked() -> None:
    tracker = ChangeTracker()
    huge = "x\n" * (MAX_TRACKED_BYTES // 2 + 8)

    tracker.record_write(_key("session-1"), Path("big.txt"), huge, huge + "extra\n")

    assert tracker.peek_run_stats(_key("session-1")) is None


def test_tracked_file_cap_evicts_oldest_and_falls_back_for_that_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(change_tracker_module, "_MAX_TRACKED_FILES", 2)
    tracker = ChangeTracker()

    tracker.record_write(_key("session-a"), Path("a1.txt"), "", "one\n")
    tracker.record_write(_key("session-a"), Path("a1.txt"), "one\n", "two\n")  # same entry
    tracker.record_write(_key("session-b"), Path("b1.txt"), "", "one\n")
    tracker.record_write(_key("session-b"), Path("b2.txt"), "", "one\n")  # evicts a1

    # A Session that lost an entry reports nothing rather than an undercount.
    assert tracker.peek_run_stats(_key("session-a")) is None
    assert tracker.take_run_stats(_key("session-a")) is None
    b_stats = tracker.take_run_stats(_key("session-b"))
    assert b_stats is not None and b_stats["files"] == 2

    # The next Run of the evicted Session is tracked normally again.
    tracker.record_write(_key("session-a"), Path("a2.txt"), "", "one\n")
    a_stats = tracker.take_run_stats(_key("session-a"))
    assert a_stats is not None and a_stats["files"] == 1


def test_tracked_file_cap_bounds_retained_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(change_tracker_module, "_MAX_TRACKED_FILES", 3)
    tracker = ChangeTracker()

    for index in range(10):
        tracker.record_write(_key(f"session-{index}"), Path("a.txt"), "", "x\n")

    retained = [index for index in range(10) if tracker.peek_run_stats(_key(f"session-{index}"))]
    assert retained == [7, 8, 9]


@pytest.mark.parametrize(
    ("other_address", "other_run"),
    [
        (SessionAddress("project", "agent", "same-session"), "same-run"),
        (SessionAddress(None, "other-agent", "same-session"), "same-run"),
        (SessionAddress(None, "agent", "other-session"), "same-run"),
        (SessionAddress(None, "agent", "same-session"), "next-run"),
    ],
)
def test_scoped_runs_never_share_or_consume_each_others_changes(
    other_address: SessionAddress, other_run: str
) -> None:
    tracker = ChangeTracker()
    first = (SessionAddress(None, "agent", "same-session"), "same-run")
    second = (other_address, other_run)
    tracker.record_write(first, Path("first.txt"), "", "one\n")
    tracker.record_write(second, Path("second.txt"), "", "two\nthree\n")

    assert tracker.take_run_stats(first) == _stats(("first.txt", 1, 0))
    assert tracker.take_run_stats(second) == _stats(("second.txt", 2, 0))


def test_final_diff_failure_still_detaches_owned_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = ChangeTracker()
    key = _key("session-one")
    tracker.record_write(key, Path("file.txt"), "before", "after")

    def fail_diff(_before: str, _after: str) -> tuple[int, int]:
        raise RuntimeError("diff unavailable")

    monkeypatch.setattr(change_tracker_module, "_line_diff_counts", fail_diff)
    with pytest.raises(RuntimeError):
        tracker.take_run_stats(key)
    assert tracker.peek_run_stats(key) is None
