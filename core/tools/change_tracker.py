"""Run-scoped file-content tracking for git-style change statistics.

Tracks, per Run, one real content delta per mutated file so the chat loop
can compute git-style before/after line diffs — streamed live after each
dispatched Tool round via ``peek_run_stats`` and consumed once at Run end via
``take_run_stats``. Every mutation is recorded against the file's actual
on-disk content immediately before the mutation, so external changes between
tool calls (formatters, shell commands, other sessions) stay outside the run's
delta instead of being attributed to it. Repeated mutations of one file within
a Run count once against the first mutation's pre-state, matching how
``git diff --stat`` reports a working-tree delta. No git repository or
external process is involved.

The tracker is a single runtime-owned instance injected into the apply_patch
tools and the chat loop (constructor injection, like ``FileReadState``) — not a
module singleton. It is deliberately best-effort: files that cannot be compared
as UTF-8 text (too large, undecodable) simply fall back to the client-side
per-tool-call counts.
"""

from __future__ import annotations

import difflib
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from core.tools.arguments import split_text_lines

if TYPE_CHECKING:
    from core.sessions import SessionAddress

# Cap on tracked ``(Run, path)`` content entries so a long-lived server
# process does not grow the map without bound; oldest insertions are evicted
# first. A rarely evicted entry only costs a harmless fallback to the
# per-tool-call counts.
_MAX_TRACKED_FILES = 4096

# Cap on retained content per file so huge files do not bloat memory. Files
# whose before- or after-content exceeds this are not tracked and fall back to
# per-tool-call counts.
MAX_TRACKED_BYTES = 512 * 1024

# Cap on the number of changed files reported per run so a pathological run
# cannot produce an unbounded payload.
_MAX_REPORTED_PATHS = 200


class ChangeTracker:
    """Process-wide registry of per-Run deltas."""

    def __init__(self) -> None:
        # Insertion-ordered ``(Run, path)`` entries, so the oldest is evicted first.
        self._run_changes: dict[tuple[tuple[SessionAddress, str], str], tuple[str, str]] = {}
        # Runs that lost an entry report no statistics until their statistics are taken,
        # so the accessor falls back to per-call counts instead of an undercount.
        self._incomplete_runs: set[tuple[SessionAddress, str]] = set()
        self._run_changes_lock = threading.Lock()

    def record_write(
        self, run_key: tuple[SessionAddress, str], resolved: Path, before: str, after: str
    ) -> None:
        """Record one file mutation for the current run's change statistics.

        ``before`` is the file's actual on-disk content immediately before the
        mutation; ``after`` is the new content. The delta is stored per
        ``(Run, path)`` so repeated edits of the same file in one run diff
        against the run's first pre-mutation state rather than summing
        per-call counts.
        """
        if (
            max(
                len(before.encode("utf-8", errors="replace")),
                len(after.encode("utf-8", errors="replace")),
            )
            > MAX_TRACKED_BYTES
        ):
            return
        key = (run_key, str(resolved))
        with self._run_changes_lock:
            existing = self._run_changes.get(key)
            if existing is not None:
                self._run_changes[key] = (existing[0], after)
                return
            self._run_changes[key] = (before, after)
            while len(self._run_changes) > _MAX_TRACKED_FILES:
                evicted_run, _path = next(iter(self._run_changes))
                del self._run_changes[evicted_run, _path]
                self._incomplete_runs.add(evicted_run)

    def peek_run_stats(self, run_key: tuple[SessionAddress, str]) -> dict[str, object] | None:
        """Return current git-style change statistics WITHOUT consuming them.

        Same computation as :meth:`take_run_stats`, but the per-run deltas stay
        stored so the chat loop can stream live totals while the Run is still
        executing and consume them once at Run end. An empty tracker returns
        ``None``; recorded files whose diffs all net to zero return an explicit
        all-zero object so a reverted change can retire an earlier nonzero
        total instead of leaving it stale.
        """
        with self._run_changes_lock:
            if run_key in self._incomplete_runs:
                return None
            snapshot = self._changes_for_run(run_key)
        if not snapshot:
            return None
        return _stats_from_changes(snapshot)

    def take_run_stats(self, run_key: tuple[SessionAddress, str]) -> dict[str, object] | None:
        """Return git-style change statistics for the Run.

        Computes one real line diff per changed file against the run's first
        pre-mutation state, sums the added/removed lines, and returns
        ``{files, added, removed, paths}`` — or ``None`` when the run changed
        no tracked files. Reverted deltas return explicit zero totals. Entries
        detach under the lock before the expensive diff, even if it fails.
        """
        with self._run_changes_lock:
            run_changes = self._changes_for_run(run_key)
            for path in run_changes:
                del self._run_changes[run_key, path]
            if run_key in self._incomplete_runs:
                self._incomplete_runs.discard(run_key)
                return None
        if not run_changes:
            return None
        return _stats_from_changes(run_changes)

    def _changes_for_run(self, run_key: tuple[SessionAddress, str]) -> dict[str, tuple[str, str]]:
        """Return one Run's deltas by path; the caller holds the lock."""
        return {
            path: change for (owner, path), change in self._run_changes.items() if owner == run_key
        }


def _stats_from_changes(run_changes: dict[str, tuple[str, str]]) -> dict[str, object]:
    """Aggregate one real line diff per changed file into run statistics."""
    paths: list[str] = []
    added = 0
    removed = 0
    for path, (before, after) in run_changes.items():
        diff_added, diff_removed = _line_diff_counts(before, after)
        if diff_added == 0 and diff_removed == 0:
            continue
        paths.append(path)
        added += diff_added
        removed += diff_removed

    paths.sort()
    return {
        "files": len(paths),
        "added": added,
        "removed": removed,
        "paths": paths[:_MAX_REPORTED_PATHS],
    }


def _line_diff_counts(before: str, after: str) -> tuple[int, int]:
    """Return ``(added, removed)`` line counts of a real before/after diff.

    Auto-junking stays off: with the default heuristic, SequenceMatcher treats
    frequently repeated lines in long files as junk and reports inflated
    replace blocks where git reports a minimal diff.
    """
    before_lines = split_text_lines(before)
    after_lines = split_text_lines(after)
    matcher = difflib.SequenceMatcher(None, before_lines, after_lines, autojunk=False)
    added = 0
    removed = 0
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag == "insert":
            added += j2 - j1
        elif tag == "delete":
            removed += _i2 - _i1
        elif tag == "replace":
            removed += _i2 - _i1
            added += j2 - j1
    return added, removed


__all__ = [
    "MAX_TRACKED_BYTES",
    "_MAX_REPORTED_PATHS",
    "_MAX_TRACKED_FILES",
]
