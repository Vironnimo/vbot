"""Immutable records of ``statistics.run_activity``, with their JSON form.

``statistics.report`` sections and Extension group usage are plain JSON built in
``core/statistics/_sections`` and ``core/statistics/_group_usage.py``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class WindowInfo:
    """Echo of the optional time window applied to time-derived aggregates."""

    since: str | None
    until: str | None


@dataclass(frozen=True)
class RunActivity:
    """One persisted Run overlapping a selected Provider-limit interval."""

    agent_id: str
    session_id: str
    session_title: str | None
    run_id: str
    status: str
    started_at: str
    completed_at: str | None
    duration_ms: int | None
    models: list[str]
    tool_calls: int
    measured_input_tokens: int
    measured_output_tokens: int
    estimated_input_tokens: int
    estimated_output_tokens: int


@dataclass(frozen=True)
class RunActivityReport:
    """Bounded Run details for one inspected historical limit interval."""

    generated_at: str
    window: WindowInfo
    total_runs: int
    truncated: bool
    runs: list[RunActivity]

    def to_dict(self) -> JsonObject:
        return asdict(self)
