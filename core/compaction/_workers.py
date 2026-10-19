"""The Compaction workers: blocking Compaction work runs here, off the Event Loop."""

from __future__ import annotations

from core.utils.workers import BoundedWorkerPool

COMPACTION_WORKER_LIMIT = 4

COMPACTION_WORKERS = BoundedWorkerPool(
    name="compaction",
    max_workers=COMPACTION_WORKER_LIMIT,
)
