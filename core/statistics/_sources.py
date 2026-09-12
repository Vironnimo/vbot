"""Statistics source contracts and canonical Session/Run activity selection."""

from __future__ import annotations

import builtins
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol

from core.chat.messages import ChatMessage
from core.sessions import (
    FORK_SOURCE_META_KEY,
    ChatSession,
    OwnedRunRecord,
    RunStartBoundary,
    SessionAddress,
)
from core.statistics._measurements import (
    _distinct_run_models,
    _duration_ms,
    _read_usage,
    _timing_field,
)
from core.statistics.index import (
    StatisticsScope,
)
from core.statistics.report import (
    JsonObject,
    RunActivity,
)
from core.statistics.timestamps import parse_timestamp


class _AgentLike(Protocol):
    @property
    def id(self) -> str: ...


class AgentDirectory(Protocol):
    """The agent-id source for the scan (satisfied by ``AgentStore``)."""

    def list(self) -> list[_AgentLike]: ...


class _ProjectLike(Protocol):
    @property
    def project_id(self) -> str: ...


class ProjectDirectory(Protocol):
    """The project-scope source for the scan (satisfied by ``ProjectStore``).

    Beyond listing Projects it enumerates, per Project, the Agents that own at
    least one canonical Session — the single discovery point for Project-scoped
    Sessions, so Statistics never knows the storage layout.
    """

    def list(self) -> builtins.list[_ProjectLike]: ...

    def session_owning_agents(self, project_id: str) -> builtins.list[str]: ...


class SessionSource(Protocol):
    """Path-free session access (satisfied by ``ChatSessionManager``).

    ``project_id`` is the Session scope: ``None`` addresses an Identity Agent;
    a set value addresses a Project Agent. Statistics passes the address through
    unchanged, so Project and identity Sessions remain distinct even when their
    Agent and Session ids match.
    """

    data_dir: Path

    def list_summaries(
        self,
        agent_id: str,
        project_id: str | None = None,
        *,
        metadata_keys: Sequence[str] = (),
    ) -> list[JsonObject]: ...

    def get(self, address: SessionAddress) -> ChatSession: ...

    def history_version(self, address: SessionAddress) -> tuple[str, int]: ...

    def list_history_versions(
        self, addresses: Sequence[SessionAddress]
    ) -> dict[SessionAddress, tuple[str, int]]: ...

    def owned_runs(
        self,
        *,
        owner_name: str,
        group_id: str,
        participant_id: str | None = None,
        after: int = 0,
        limit: int = 100,
    ) -> list[OwnedRunRecord]: ...

    def run_start_boundaries(
        self, addresses: Sequence[SessionAddress]
    ) -> Sequence[RunStartBoundary]: ...


def _indexed_activity_summary(summary: JsonObject) -> JsonObject:
    """Remove fork slicing metadata after the read boundary omitted that prefix."""
    projected = dict(summary)
    projected.pop(FORK_SOURCE_META_KEY, None)
    return projected


def _run_activity_record(
    agent_id: str,
    session_id: str,
    session_title: str | None,
    group: list[ChatMessage],
    summary: ChatMessage,
) -> RunActivity:
    measured_input = 0
    measured_output = 0
    estimated_input = 0
    estimated_output = 0
    for message in group:
        if message.role != "assistant":
            continue
        facts = _read_usage(message.usage)
        if facts.input_estimated:
            estimated_input += facts.input_tokens
        else:
            measured_input += facts.input_tokens
        if facts.output_estimated:
            estimated_output += facts.output_tokens
        else:
            measured_output += facts.output_tokens

    started_at = _timing_field(summary.timing, "started_at") or summary.timestamp
    completed_at = _timing_field(summary.timing, "completed_at") or summary.timestamp
    return RunActivity(
        agent_id=agent_id,
        session_id=session_id,
        session_title=session_title,
        run_id=summary.run_id or "",
        status=summary.status or "completed",
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=_duration_ms(summary.timing) or 0,
        models=sorted(_distinct_run_models(group)),
        tool_calls=sum(1 for message in group if message.role == "tool"),
        measured_input_tokens=measured_input,
        measured_output_tokens=measured_output,
        estimated_input_tokens=estimated_input,
        estimated_output_tokens=estimated_output,
    )


def _run_overlaps(activity: RunActivity, *, since: datetime, until: datetime) -> bool:
    started_at = parse_timestamp(activity.started_at)
    completed_at = parse_timestamp(activity.completed_at)
    if started_at is None or completed_at is None:
        return False
    return completed_at >= since and started_at <= until


def _session_activity_messages(
    messages: list[ChatMessage], summary: JsonObject
) -> list[ChatMessage]:
    """Exclude the copied transcript prefix from a fork's activity aggregates.

    ``ChatSessionManager.fork`` records the exact number of copied complete
    messages in ``fork_source.message_count``. Those records remain part of the
    fork's visible history, but their Runs, usage, errors, and Tool calls already
    belong to the source Session. Invalid hand-edited metadata fails open and
    leaves the transcript unchanged rather than silently hiding activity.
    """
    fork_source = summary.get(FORK_SOURCE_META_KEY)
    if not isinstance(fork_source, dict):
        return messages
    copied_message_count = fork_source.get("message_count")
    if (
        isinstance(copied_message_count, bool)
        or not isinstance(copied_message_count, int)
        or copied_message_count < 0
        or copied_message_count > len(messages)
    ):
        return messages
    return messages[copied_message_count:]


def _owner_scopes(records: Sequence[OwnedRunRecord]) -> tuple[StatisticsScope, ...]:
    grouped: dict[tuple[str | None, str], list[JsonObject]] = {}
    seen: set[SessionAddress] = set()
    for record in records:
        if record.address in seen:
            continue
        seen.add(record.address)
        grouped.setdefault((record.address.project_id, record.address.agent_id), []).append(
            {"id": record.address.session_id}
        )
    return tuple(
        StatisticsScope(
            project_id=project_id,
            agent_id=agent_id,
            display_key=agent_id,
            summaries=tuple(summaries),
        )
        for (project_id, agent_id), summaries in grouped.items()
    )


def _owned_run_messages(
    messages: Sequence[ChatMessage], record: OwnedRunRecord, boundaries: Sequence[RunStartBoundary]
) -> list[ChatMessage]:
    """Select one owner Run without treating a reused Session as wholly owned."""
    start = record.start_sequence
    end = record.terminal_sequence
    if end is None:
        generation_starts = [
            boundary for boundary in boundaries if boundary.generation_id == record.generation_id
        ]
        current = next(
            (
                index
                for index, boundary in enumerate(generation_starts)
                if boundary.run_id == record.run_id
            ),
            None,
        )
        if current is None:
            return []
        # A Run with no output can share its sequence with its successor.
        # Canonical admission order, not a strict sequence comparison, separates them.
        if current + 1 < len(generation_starts):
            end = generation_starts[current + 1].start_sequence - 1
    selected: list[ChatMessage] = []
    for message in messages:
        ordinal = _statistics_message_ordinal(message)
        if ordinal is None or ordinal < start or (end is not None and ordinal > end):
            continue
        selected.append(message)
    return selected


def _statistics_message_ordinal(message: ChatMessage) -> int | None:
    prefix = "statistics-"
    if not message.id.startswith(prefix):
        return None
    try:
        value = int(message.id.removeprefix(prefix))
    except ValueError:
        return None
    return value if value >= 0 else None
