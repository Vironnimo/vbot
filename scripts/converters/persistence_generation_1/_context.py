"""Shared state handed to every Generation 1 conversion area."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any


class ConversionError(Exception):
    """Raised when a source cannot be converted at all."""


@dataclass(frozen=True, slots=True)
class SkippedItem:
    """One source item an area dropped or approximated.

    ``changes_history`` marks an item that can change which entries a Session's
    current history shows, such as a dropped history record. Only such items
    explain a history difference in the Session check.
    """

    area: str
    item: str
    reason: str
    changes_history: bool = False


@dataclass(slots=True)
class ConversionReport:
    """Counts and skipped items, collected across all areas."""

    counts: dict[str, dict[str, int]] = field(default_factory=dict)
    skipped: list[SkippedItem] = field(default_factory=list)

    def count(self, area: str, key: str, amount: int = 1) -> None:
        area_counts = self.counts.setdefault(area, {})
        area_counts[key] = area_counts.get(key, 0) + amount

    def skip(self, area: str, item: str, reason: str, *, changes_history: bool = False) -> None:
        self.skipped.append(
            SkippedItem(area=area, item=item, reason=reason, changes_history=changes_history)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "counts": {area: dict(values) for area, values in self.counts.items()},
            "skipped": [
                {
                    "area": item.area,
                    "item": item.item,
                    "reason": item.reason,
                    "changes_history": item.changes_history,
                }
                for item in self.skipped
            ],
        }


@dataclass(slots=True)
class ConversionContext:
    """Source data directory, staging root and report for one conversion."""

    source: Path
    staging: Path
    report: ConversionReport = field(default_factory=ConversionReport)
    retired: list[PurePosixPath] = field(default_factory=list)

    def source_path(self, relative: str | PurePosixPath) -> Path:
        return self.source / _relative(relative)

    def staged(self, relative: str | PurePosixPath) -> Path:
        """Return the staging path for *relative*, creating its parent directory."""
        path = self.staging / _relative(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def retire(self, relative: str | PurePosixPath) -> None:
        """Mark a source file that the installed result no longer uses."""
        path = _relative(relative)
        if path not in self.retired:
            self.retired.append(path)


def _relative(relative: str | PurePosixPath) -> PurePosixPath:
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ConversionError(
            f"conversion paths must be relative to the data directory: {relative}"
        )
    return path
