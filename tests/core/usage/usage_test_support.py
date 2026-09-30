"""Reading the whole Usage ledger for assertions."""

from __future__ import annotations

from core.usage import UsageRecord, UsageRecorder


def read_ledger(recorder: UsageRecorder, revision: int = 0) -> tuple[int, tuple[UsageRecord, ...]]:
    """The final watermark and every call version ``read_since(revision)`` delivers."""
    latest, records = 0, list[UsageRecord]()
    for page in recorder.read_since(revision):
        latest = page.revision
        records.extend(page.records)
    return latest, tuple(records)
