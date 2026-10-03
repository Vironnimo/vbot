"""Run change statistics: the lines each Run changed through its own file writes."""

from __future__ import annotations

from pathlib import Path

import pytest

import core.tools.change_tracker as change_tracker_module
from core.sessions import SessionAddress
from core.tools.change_tracker import MAX_RETAINED_FILE_CHARS, ChangeTracker


def _key(session_id: str, run_id: str = "run-one") -> tuple[SessionAddress, str]:
    return SessionAddress(None, "agent", session_id), run_id


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


@pytest.mark.parametrize(
    ("writes", "expected"),
    [
        # Consecutive writes of one file count once, as one net diff.
        (
            [
                ("a.txt", "line1\nline2\nline3\n", "line1\nline2b\nline3\n"),
                ("a.txt", "line1\nline2b\nline3\n", "line1\nline2c\nline3\n"),
            ],
            _stats(("a.txt", 1, 1)),
        ),
        ([("new.txt", "", "x\ny\n")], _stats(("new.txt", 2, 0))),
        ([("gone.txt", "x\ny\n", "")], _stats(("gone.txt", 0, 2))),
        ([("a.txt", "a\n", "b\nc\n")], _stats(("a.txt", 2, 1))),
        # Line endings are content, like in git.
        ([("a.txt", "a\nb", "a\nb\n")], _stats(("a.txt", 1, 1))),
        # Several files aggregate, each with its own counts, in path order.
        (
            [("b.txt", "a\n", "b\nc\n"), ("a.txt", "a\n", "c\n")],
            _stats(("a.txt", 1, 1), ("b.txt", 2, 1)),
        ),
        # Unchanged or reverted writes report explicit zeros, not None.
        ([("a.txt", "same\n", "same\n")], _stats()),
        ([("a.txt", "a\n", "b\n"), ("a.txt", "b\n", "a\n")], _stats()),
        # The file changed between the Run's writes: the change in between is not
        # the Run's, so each segment counts against the content it started from.
        (
            [
                ("a.txt", "a\nb\nc\n", "a\nB\nc\n"),
                ("a.txt", "A\nB\nc\n", "A\nB\nC\n"),
            ],
            _stats(("a.txt", 2, 2)),
        ),
    ],
)
def test_stats_are_the_runs_own_line_diffs(
    writes: list[tuple[str, str, str]], expected: dict[str, object]
) -> None:
    tracker = ChangeTracker()
    for path, before, after in writes:
        tracker.record_write(_key("session-1"), Path(path), before, after)

    # Peeking streams totals without consuming them; taking consumes them once.
    assert tracker.peek_run_stats(_key("session-1")) == expected
    assert tracker.peek_run_stats(_key("session-1")) == expected
    assert tracker.take_run_stats(_key("session-1")) == expected
    assert tracker.peek_run_stats(_key("session-1")) is None
    assert tracker.take_run_stats(_key("session-1")) is None


def test_concurrent_runs_editing_one_file_count_only_their_own_lines() -> None:
    tracker = ChangeTracker()
    first, second = _key("session-a"), _key("session-b")
    target = Path("shared.txt")

    tracker.record_write(first, target, "1\n2\n3\n4\n", "1 first\n2\n3\n4\n")
    tracker.record_write(second, target, "1 first\n2\n3\n4\n", "1 first\n2\n3\n4 second\n")
    tracker.record_write(
        first, target, "1 first\n2\n3\n4 second\n", "1 first\n2 first\n3\n4 second\n"
    )

    assert tracker.take_run_stats(first) == _stats(("shared.txt", 2, 2))
    assert tracker.take_run_stats(second) == _stats(("shared.txt", 1, 1))


def test_large_files_and_spent_budgets_still_count_every_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contents that cannot be retained are counted per write instead of netted."""
    tracker = ChangeTracker()
    huge = "x\n" * (MAX_RETAINED_FILE_CHARS // 2 + 8)
    tracker.record_write(_key("session-1"), Path("big.txt"), huge, huge + "one\n")
    tracker.record_write(_key("session-1"), Path("big.txt"), huge + "one\n", huge + "two\n")

    assert tracker.take_run_stats(_key("session-1")) == _stats(("big.txt", 2, 1))

    monkeypatch.setattr(change_tracker_module, "MAX_RETAINED_CHARS", 0)
    tracker.record_write(_key("session-1"), Path("a.txt"), "a\n", "b\n")
    tracker.record_write(_key("session-2"), Path("b.txt"), "", "x\n")
    tracker.record_write(_key("session-1"), Path("a.txt"), "b\n", "a\n")

    assert tracker.take_run_stats(_key("session-1")) == _stats(("a.txt", 2, 2))
    assert tracker.take_run_stats(_key("session-2")) == _stats(("b.txt", 1, 0))


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


def test_reported_paths_are_capped_but_totals_count_every_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(change_tracker_module, "MAX_REPORTED_PATHS", 2)
    tracker = ChangeTracker()
    for name in ("c.txt", "a.txt", "b.txt"):
        tracker.record_write(_key("session-1"), Path(name), "", "x\n")

    stats = tracker.take_run_stats(_key("session-1"))

    assert stats is not None
    assert (stats["files"], stats["added"], stats["paths"]) == (3, 3, ["a.txt", "b.txt"])


def test_final_count_failure_still_detaches_owned_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = ChangeTracker()
    key = _key("session-one")
    tracker.record_write(key, Path("file.txt"), "before", "after")

    def fail_counts(_before: str | None, _after: str | None) -> tuple[int, int]:
        raise RuntimeError("diff unavailable")

    monkeypatch.setattr(change_tracker_module, "_counts", fail_counts)
    with pytest.raises(RuntimeError):
        tracker.take_run_stats(key)
    assert tracker.peek_run_stats(key) is None
