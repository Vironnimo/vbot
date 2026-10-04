"""Run change statistics: the lines each Run changed through its file edits.

The chat loop reads a Run's statistics after every Tool round (``peek_run_stats``)
to stream and persist them, and consumes them once at Run end
(``take_run_stats``). The file edit Tools report each committed text write with
the file's actual content right before and after it (``record_write``).

A Run's statistics cover only its own writes. Consecutive writes of one file
form a segment that counts once, as the minimal line diff from the content
before the first write to the content after the last (repeated edits of one
line count once, a reverted edit counts zero). When the file changed between
two of the Run's writes (another Session, a formatter, a shell command), the
segment closes and the next write starts a new one from the changed content:
the change in between is never attributed to the Run. A file's counts are the
sum of its segments.

Every write counts. Retaining contents is what lets a segment net repeated
edits; a file too large to retain, or contents beyond the shared memory budget,
are counted right away and their contents dropped, so their next write starts a
new segment. No git repository or external process is involved.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from core.tools._line_diff import line_change_counts
from core.tools.arguments import split_text_lines

if TYPE_CHECKING:
    from core.sessions import SessionAddress
    from core.tools.contracts import JsonObject

type RunKey = tuple[SessionAddress, str]

# Contents a segment may retain per side (characters). A larger file is counted
# at once and not retained.
MAX_RETAINED_FILE_CHARS = 512 * 1024

# Contents all segments may retain together (characters). Beyond it the oldest
# segments are counted and drop their contents.
MAX_RETAINED_CHARS = 64 * 1024 * 1024

# Changed files listed per Run payload; ``files``, ``added`` and ``removed``
# still count every changed file.
MAX_REPORTED_PATHS = 200


@dataclass(eq=False, slots=True)
class _Segment:
    """Consecutive writes of one file by one Run: its counts, or the contents to count."""

    base: str | None
    current: str | None
    counts: tuple[int, int] | None = None

    @property
    def retained(self) -> int:
        return len(self.base or "") + len(self.current or "")


class ChangeTracker:
    """Runtime-owned registry of the change statistics of running Runs."""

    def __init__(self) -> None:
        # Per running Run, each written file's segments in write order.
        self._runs: dict[RunKey, dict[str, list[_Segment]]] = {}
        self._retained = 0
        self._lock = threading.Lock()

    def record_write(self, run_key: RunKey, resolved: Path, before: str, after: str) -> None:
        """Record one committed text write of ``resolved`` by the Run.

        ``before`` is the file's actual content immediately before the write
        (empty for a new file), ``after`` the content it wrote (empty for a
        deletion).
        """
        with self._lock:
            files = self._runs.setdefault(run_key, {})
            segments = files.setdefault(str(resolved), [])
            last = segments[-1] if segments else None
            if last is not None and last.current is not None and last.current == before:
                self._retained -= last.retained
                last.current = after
                last.counts = None
                segment = last
            else:
                segment = _Segment(base=before, current=after)
                segments.append(segment)
            self._retained += segment.retained
            # A closed segment needs its contents only until it is counted.
            due = [item for item in segments[:-1] if item.retained]
            if max(len(segment.base or ""), len(segment.current or "")) > MAX_RETAINED_FILE_CHARS:
                due.append(segment)
            due.extend(self._over_budget(exclude=due))
            pending = [(item, item.base, item.current, item.counts) for item in due]
        # Count outside the lock; a segment written or taken meanwhile is left as it is.
        counted = [
            (item, base, current, known if known is not None else _counts(base, current))
            for item, base, current, known in pending
        ]
        with self._lock:
            for item, base, current, counts in counted:
                if item.base is base and item.current is current:
                    self._retained -= item.retained
                    item.base = item.current = None
                    item.counts = counts

    def peek_run_stats(self, run_key: RunKey) -> JsonObject | None:
        """Return the Run's current statistics without consuming them.

        ``None`` means the Run recorded no write; a Run whose writes net to
        nothing reports explicit zeros, so a reverted change retires an
        earlier total.
        """
        with self._lock:
            run = self._runs.get(run_key)
            if run is None:
                return None
            files = _snapshot(run)
        counted = _count_files(files)
        with self._lock:
            for path, segments in files.items():
                for (item, base, current, _known), counts in zip(
                    segments, counted[path], strict=True
                ):
                    if item.base is base and item.current is current:
                        item.counts = counts
        return _stats(counted)

    def take_run_stats(self, run_key: RunKey) -> JsonObject | None:
        """Return the Run's final statistics and forget the Run, like ``peek_run_stats``."""
        with self._lock:
            run = self._runs.pop(run_key, None)
            if run is None:
                return None
            files = _snapshot(run)
            for segments in run.values():
                for item in segments:
                    self._retained -= item.retained
                    item.base = item.current = None
        return _stats(_count_files(files))

    def _over_budget(self, exclude: list[_Segment]) -> list[_Segment]:
        """Return the oldest retaining segments to count so the rest fit the budget."""
        excess = self._retained - MAX_RETAINED_CHARS - sum(item.retained for item in exclude)
        chosen: list[_Segment] = []
        if excess <= 0:
            return chosen
        skipped = {id(item) for item in exclude}
        for run in self._runs.values():
            for segments in run.values():
                for item in segments:
                    if item.retained and id(item) not in skipped:
                        chosen.append(item)
                        excess -= item.retained
                        if excess <= 0:
                            return chosen
        return chosen


def _counts(before: str | None, after: str | None) -> tuple[int, int]:
    return line_change_counts(
        split_text_lines(before or "", keepends=True), split_text_lines(after or "", keepends=True)
    )


type _SegmentState = tuple[_Segment, str | None, str | None, tuple[int, int] | None]


def _snapshot(run: dict[str, list[_Segment]]) -> dict[str, list[_SegmentState]]:
    """Return each file's segments with their contents and counts as they are now."""
    return {
        path: [(item, item.base, item.current, item.counts) for item in segments]
        for path, segments in run.items()
    }


def _count_files(files: dict[str, list[_SegmentState]]) -> dict[str, list[tuple[int, int]]]:
    return {
        path: [
            known if known is not None else _counts(base, current)
            for _item, base, current, known in segments
        ]
        for path, segments in files.items()
    }


def _stats(counted: dict[str, list[tuple[int, int]]]) -> JsonObject:
    """Aggregate per-file segment counts into Run statistics.

    ``paths`` lists the changed files in path order and ``file_stats`` the same
    files with their own counts; a file whose counts are zero is in neither.
    """
    file_stats: list[JsonObject] = []
    added = removed = 0
    for path in sorted(counted):
        file_added = sum(counts[0] for counts in counted[path])
        file_removed = sum(counts[1] for counts in counted[path])
        if file_added or file_removed:
            file_stats.append({"path": path, "added": file_added, "removed": file_removed})
            added += file_added
            removed += file_removed
    reported = file_stats[:MAX_REPORTED_PATHS]
    return {
        "files": len(file_stats),
        "added": added,
        "removed": removed,
        "paths": [entry["path"] for entry in reported],
        "file_stats": reported,
    }
