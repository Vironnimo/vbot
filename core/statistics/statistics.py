"""Statistics service: canonical Session reconciliation, report access and owner-scoped usage."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from core.chat.messages import ChatMessage
from core.projects.address import format_agent_address
from core.sessions import (
    OwnedRunRecord,
    SessionAddress,
    SessionNotFoundError,
)
from core.statistics._aggregation import (
    _Aggregator,
)
from core.statistics._sources import (
    AgentDirectory,
    ProjectDirectory,
    SessionSource,
    _indexed_activity_summary,
    _owned_run_messages,
    _owner_scopes,
    _run_activity_record,
    _run_overlaps,
    _session_activity_messages,
)
from core.statistics.index import (
    IndexedStatisticsSession,
    StatisticsIndex,
    StatisticsIndexError,
    StatisticsScope,
    statistics_session_key,
)
from core.statistics.report import (
    JsonObject,
    RunActivity,
    RunActivityReport,
    StatisticsReport,
    WindowInfo,
)
from core.statistics.skills import (
    SkillInventorySource,
)
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool

_LOGGER = get_logger("statistics")


_GROUP_USAGE_WORKERS = BoundedWorkerPool(name="statistics-group-usage", max_workers=2)


MAX_RUN_ACTIVITY = 200


class StatisticsService:
    """Compute a full :class:`StatisticsReport` from a disposable Session index.

    The service reconciles canonical Sessions into a compact SQLite projection
    before every read. Unchanged transcripts are never loaded; append-only growth
    validates and ingests only the new tail. A failed or incompatible projection
    is discarded and rebuilt once, with the canonical live scan retained as a
    final availability fallback.

    Project sessions feed the same report as identity sessions; a project agent
    appears under its outer address form ``agent@project`` so it stays distinct
    from the bare identity id and from the same agent in another project. Without
    any projects (or with an absent project directory) the report is identical to
    the identity-only scan — project scopes live under a different anchor path,
    so the same session id under both scopes is two different files, never a
    double count.

    The optional ``skill_inventory`` joins observed skill usage against the
    current skill set (see ``core/statistics/skills.py``). When omitted, the
    skills section still builds — against an empty inventory, so every observed
    usage drops and all counts are zero — keeping existing constructions valid.
    """

    def __init__(
        self,
        chat_sessions: SessionSource,
        agents: AgentDirectory,
        projects: ProjectDirectory | None = None,
        skill_inventory: SkillInventorySource | None = None,
    ) -> None:
        self._sessions = chat_sessions
        self._agents = agents
        self._projects = projects
        self._skill_inventory = skill_inventory
        self._index = StatisticsIndex(Path(chat_sessions.data_dir))

    def warm_index(self) -> None:
        """Reconcile the disposable index without building a report."""
        self._indexed_snapshot(self._statistics_scopes())

    def report(
        self, *, since: datetime | None = None, until: datetime | None = None
    ) -> StatisticsReport:
        """Reconcile all Session scopes and return the aggregated report."""
        aggregator = _Aggregator(since=since, until=until)
        scopes = self._statistics_scopes()
        snapshot = self._indexed_snapshot(scopes)
        for scope in scopes:
            summaries: list[JsonObject] = []
            aggregator.register_scope(agent_id=scope.agent_id, project_id=scope.project_id)
            for summary, messages in self._scope_sessions(scope, snapshot):
                aggregator.process_session(scope.display_key, str(summary["id"]), messages, summary)
                summaries.append(summary)
            aggregator.register_agent(scope.display_key, summaries)
        return aggregator.build(self._skill_inventory)

    def run_activity(
        self,
        *,
        since: datetime,
        until: datetime,
    ) -> RunActivityReport:
        """Return persisted Runs whose execution overlaps the selected interval."""

        runs: list[RunActivity] = []
        scopes = self._statistics_scopes()
        snapshot = self._indexed_snapshot(scopes)
        for scope in scopes:
            for summary, messages in self._scope_sessions(scope, snapshot):
                session_id = str(summary["id"])
                title = summary.get("title")
                session_title = title if isinstance(title, str) and title else None
                group: list[ChatMessage] = []
                for message in messages:
                    if message.role != "run_summary":
                        group.append(message)
                        continue
                    activity = _run_activity_record(
                        scope.display_key, session_id, session_title, group, message
                    )
                    if _run_overlaps(activity, since=since, until=until):
                        runs.append(activity)
                    group = []

        runs.sort(key=lambda run: run.started_at, reverse=True)
        total_runs = len(runs)
        return RunActivityReport(
            generated_at=datetime.now(UTC).isoformat(),
            window=WindowInfo(since=since.isoformat(), until=until.isoformat()),
            total_runs=total_runs,
            truncated=total_runs > MAX_RUN_ACTIVITY,
            runs=runs[:MAX_RUN_ACTIVITY],
        )

    async def group_usage(
        self,
        *,
        owner_name: str,
        group_id: str,
        query: JsonObject | None = None,
    ) -> JsonObject:
        """Return a bounded, owner-exact usage projection for an Extension group."""
        return await _GROUP_USAGE_WORKERS.run(
            self._group_usage, owner_name, group_id, dict(query or {})
        )

    def _group_usage(self, owner_name: str, group_id: str, query: JsonObject) -> JsonObject:
        if set(query) - {"participant_id"}:
            raise ValueError("invalid group usage query")
        if (
            not isinstance(owner_name, str)
            or not owner_name
            or not isinstance(group_id, str)
            or not group_id
        ):
            raise ValueError("owner_name and group_id are required")
        participant_id = query.get("participant_id")
        if participant_id is not None and (
            not isinstance(participant_id, str) or not participant_id
        ):
            raise ValueError("participant_id must be a non-empty string")
        records = self._owned_run_page(owner_name, group_id, participant_id)
        report = self._group_report(records)
        participants = {record.owner.participant_id for record in records}
        return {
            "group_id": group_id,
            "participant_id": participant_id,
            "participant_count": len(participants),
            "owned_run_count": len(records),
            "usage": asdict(report.usage),
            "tools": asdict(report.tools),
            "compactions": asdict(report.compactions),
            "runs": asdict(report.runs),
        }

    def _owned_run_page(
        self, owner_name: str, group_id: str, participant_id: str | None
    ) -> list[OwnedRunRecord]:
        result: list[OwnedRunRecord] = []
        after = 0
        while True:
            page = self._sessions.owned_runs(
                owner_name=owner_name,
                group_id=group_id,
                participant_id=participant_id,
                after=after,
                limit=1000,
            )
            result.extend(page)
            if len(page) < 1000:
                return result
            after = page[-1].record_key

    def _group_report(self, records: list[OwnedRunRecord]) -> StatisticsReport:
        scopes = _owner_scopes(records)
        snapshot = self._index.snapshot(self._sessions, scopes, prune=False)
        by_address: dict[SessionAddress, list[OwnedRunRecord]] = {}
        for record in records:
            by_address.setdefault(record.address, []).append(record)
        aggregator = _Aggregator(since=None, until=None)
        addresses = tuple(by_address)
        boundaries = [
            boundary
            for offset in range(0, len(addresses), 100)
            for boundary in self._sessions.run_start_boundaries(addresses[offset : offset + 100])
        ]
        for address, address_records in by_address.items():
            key = statistics_session_key(address.project_id, address.agent_id, address.session_id)
            indexed = snapshot.get(key)
            if indexed is None:
                continue
            messages = list(indexed.messages)
            address_boundaries = [
                boundary
                for boundary in boundaries
                if boundary.address.project_id == address.project_id
                and boundary.address.agent_id == address.agent_id
                and boundary.address.session_id == address.session_id
            ]
            for record in address_records:
                if indexed.generation_id != record.generation_id:
                    continue
                sliced = _owned_run_messages(messages, record, address_boundaries)
                if not sliced:
                    continue
                display_key = record.owner.participant_id
                aggregator.register_agent(display_key, [{"id": address.session_id}])
                aggregator.register_scope(agent_id=address.agent_id, project_id=address.project_id)
                aggregator.process_session(
                    display_key, address.session_id, sliced, {"id": address.session_id}
                )
        return aggregator.build(None)

    def _project_scopes(self) -> list[tuple[str, str]]:
        """Return ``(project_id, agent_id)`` for every session-owning project agent."""
        if self._projects is None:
            return []
        scopes: list[tuple[str, str]] = []
        for project in self._projects.list():
            project_id = project.project_id
            for agent_id in self._projects.session_owning_agents(project_id):
                scopes.append((project_id, agent_id))
        return scopes

    def _statistics_scopes(self) -> tuple[StatisticsScope, ...]:
        scopes = [
            StatisticsScope(
                project_id=None,
                agent_id=agent.id,
                display_key=agent.id,
                summaries=tuple(
                    self._sessions.list_summaries(
                        agent.id,
                        None,
                        metadata_keys=("seen_skills",),
                    )
                ),
            )
            for agent in self._agents.list()
        ]
        for project_id, agent_id in self._project_scopes():
            scopes.append(
                StatisticsScope(
                    project_id=project_id,
                    agent_id=agent_id,
                    display_key=format_agent_address(agent_id, project_id),
                    summaries=tuple(
                        self._sessions.list_summaries(
                            agent_id,
                            project_id,
                            metadata_keys=("seen_skills",),
                        )
                    ),
                )
            )
        return tuple(scopes)

    def _indexed_snapshot(
        self,
        scopes: tuple[StatisticsScope, ...],
    ) -> dict[tuple[str, str, str], IndexedStatisticsSession] | None:
        for attempt in range(2):
            try:
                return self._index.snapshot(self._sessions, scopes)
            except (OSError, sqlite3.DatabaseError, StatisticsIndexError) as error:
                if attempt == 0:
                    _LOGGER.warning(
                        "Statistics index failed; rebuilding once: %s",
                        error,
                    )
                    try:
                        self._index.discard()
                    except OSError as discard_error:
                        _LOGGER.warning(
                            "Could not discard failed Statistics index: %s",
                            discard_error,
                        )
                else:
                    _LOGGER.warning(
                        "Statistics index rebuild failed; using canonical Session scan: %s",
                        error,
                    )
        return None

    def _scope_sessions(
        self,
        scope: StatisticsScope,
        snapshot: dict[tuple[str, str, str], IndexedStatisticsSession] | None,
    ) -> Iterator[tuple[JsonObject, list[ChatMessage]]]:
        """Resolve surviving Sessions with fork prefixes excluded exactly once.

        Report consumers share this read boundary regardless of whether the
        disposable index is available. A Session removed during reconciliation
        or canonical loading contributes neither structure nor activity.
        """
        for summary in scope.summaries:
            session_id = str(summary["id"])
            if snapshot is not None:
                indexed = snapshot.get(
                    statistics_session_key(scope.project_id, scope.agent_id, session_id)
                )
                if indexed is None:
                    continue
                summary = indexed.summary
                messages = list(indexed.messages)
            else:
                address = SessionAddress(
                    project_id=scope.project_id, agent_id=scope.agent_id, session_id=session_id
                )
                try:
                    messages = self._sessions.get(address).load()
                except SessionNotFoundError:
                    continue
                messages = _session_activity_messages(messages, summary)
            yield _indexed_activity_summary(summary), messages
