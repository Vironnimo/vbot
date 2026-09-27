"""Canonical fixed values shared by chat message tests."""

from datetime import UTC, datetime

FIXED_TIMESTAMP = datetime(2026, 5, 3, 14, 30, tzinfo=UTC)
FIXED_TIMING = {
    "started_at": "2026-05-03T14:30:01+00:00",
    "completed_at": "2026-05-03T14:30:02+00:00",
    "duration_ms": 1234,
}
