"""Session-scoped file-content tracking for git-style change statistics.

Tracks, per session, the last known text content of every file the session has
read or written, so the chat loop can compute real before/after line diffs —
streamed live after each dispatched Tool round via ``peek_run_stats`` and
consumed once at Run end via ``take_run_stats``. The diff uses
``difflib.SequenceMatcher`` — the same Myers line-diff algorithm git uses for
``git diff --stat`` — so repeated edits of the same line count once and a
full-file rewrite shows only the lines that actually changed. No git repository
or external process is involved.

The tracker is a single runtime-owned instance injected into the read/write/edit
tools and the chat loop (constructor injection, like ``FileReadState``) — not a
module singleton. It is deliberately best-effort: a missing baseline (file never
read in this session, server restart, non-UTF-8 content) simply means the run
falls back to the client-side per-tool-call counts.
"""

from __future__ import annotations

import difflib
import threading
from pathlib import Path

# Cap on tracked ``(session, path)`` content entries so a long-lived server
# process does not grow the map without bound; oldest insertions are evicted
# first. A rarely evicted entry only costs a harmless fallback to the
# per-tool-call counts.
_MAX_TRACKED_FILES = 4096

# Cap on retained content per file so huge files do not bloat memory. Files
# larger than this are not tracked and fall back to per-tool-call counts.
_MAX_TRACKED_BYTES = 512 * 1024

# Cap on the number of changed files reported per run so a pathological run
# cannot produce an unbounded payload.
_MAX_REPORTED_PATHS = 200


class ChangeTracker:
    """Process-wide registry of per-session file baselines and run deltas."""

    def __init__(self) -> None:
        self._baselines: dict[tuple[str, str], str] = {}
        self._baselines_lock = threading.Lock()
        self._run_changes: dict[str, dict[str, tuple[str, str]]] = {}
        self._run_changes_lock = threading.Lock()

    def record_read(self, session_id: str, resolved: Path, content: str) -> None:
        """Remember the current text content of a file for a session.

        Called by ``read`` for text files. The baseline is only a fallback for
        later writes; a write without a baseline still records its own delta.
        """
        if not content or len(content.encode("utf-8", errors="replace")) > _MAX_TRACKED_BYTES:
            return
        key = (session_id, str(resolved))
        with self._baselines_lock:
            self._baselines.pop(key, None)
            self._baselines[key] = content
            while len(self._baselines) > _MAX_TRACKED_FILES:
                del self._baselines[next(iter(self._baselines))]

    def baseline_for(self, session_id: str, resolved: Path) -> str | None:
        """Return the session's last known text content of a file, if any."""
        with self._baselines_lock:
            return self._baselines.get((session_id, str(resolved)))

    def record_write(self, session_id: str, resolved: Path, before: str, after: str) -> None:
        """Record one file mutation for the current run's change statistics.

        ``before`` is the content the tool replaced (the session's last known
        content, or the on-disk content for a full-file write); ``after`` is the
        new content. The delta is stored per ``(session, path)`` so repeated
        edits of the same file in one run diff against the run's first baseline
        rather than summing per-call counts.
        """
        if len(after.encode("utf-8", errors="replace")) > _MAX_TRACKED_BYTES:
            return
        path = str(resolved)
        with self._run_changes_lock:
            run_changes = self._run_changes.setdefault(session_id, {})
            if path not in run_changes:
                run_changes[path] = (before, after)
            else:
                _before, _after = run_changes[path]
                run_changes[path] = (_before, after)

    def peek_run_stats(self, session_id: str) -> dict[str, object] | None:
        """Return current git-style change statistics WITHOUT consuming them.

        Same computation as :meth:`take_run_stats`, but the per-run deltas stay
        stored so the chat loop can stream live totals while the Run is still
        executing and consume them once at Run end. An empty tracker returns
        ``None``; recorded files whose diffs all net to zero return an explicit
        all-zero object so a reverted change can retire an earlier nonzero
        total instead of leaving it stale.
        """
        with self._run_changes_lock:
            snapshot = dict(self._run_changes.get(session_id, {}))
        if not snapshot:
            return None
        stats = _stats_from_changes(snapshot)
        if stats is not None:
            return stats
        return {"files": 0, "added": 0, "removed": 0, "paths": []}

    def take_run_stats(self, session_id: str) -> dict[str, object] | None:
        """Return git-style change statistics for the session's current run.

        Computes one real line diff per changed file against the run's first
        baseline, sums the added/removed lines, and returns
        ``{files, added, removed, paths}`` — or ``None`` when the run changed
        no tracked files. The per-run deltas are consumed and cleared.
        """
        with self._run_changes_lock:
            run_changes = self._run_changes.pop(session_id, None)
        if not run_changes:
            return None
        return _stats_from_changes(run_changes)


def _stats_from_changes(run_changes: dict[str, tuple[str, str]]) -> dict[str, object] | None:
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

    if not paths:
        return None
    paths.sort()
    return {
        "files": len(paths),
        "added": added,
        "removed": removed,
        "paths": paths[:_MAX_REPORTED_PATHS],
    }


def _line_diff_counts(before: str, after: str) -> tuple[int, int]:
    """Return ``(added, removed)`` line counts of a real before/after diff."""
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    matcher = difflib.SequenceMatcher(None, before_lines, after_lines)
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
    "ChangeTracker",
    "_MAX_REPORTED_PATHS",
    "_MAX_TRACKED_BYTES",
    "_MAX_TRACKED_FILES",
]
