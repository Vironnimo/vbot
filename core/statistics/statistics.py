"""Statistics service: report access and owner-scoped usage over the derived index."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from core.models.pricing import TokenPricing
from core.projects.address import format_agent_address
from core.sessions import OwnedRunRecord, SessionAddress
from core.statistics._aggregation import ReportBuilder
from core.statistics._call_scan import account_run_activity
from core.statistics._extensions import ExtensionSliceKey, extension_actor_key
from core.statistics._runs import load_run_activity
from core.statistics._sources import (
    AgentDirectory,
    ProjectDirectory,
    SessionSource,
    _owner_scopes,
    extension_session_summary,
)
from core.statistics._units import ReportUnit, materialize_run_slices
from core.statistics.index import (
    IndexedSession,
    IndexView,
    StatisticsIndex,
    StatisticsScope,
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
    offered_skill_names,
)

if TYPE_CHECKING:
    from core.usage import UsageRecorder


@dataclass(frozen=True)
class _ExtensionSession:
    """One owner-managed participant Session: its index scope and report slice."""

    scope: StatisticsScope
    key: ExtensionSliceKey


MAX_RUN_ACTIVITY = 200


class StatisticsService:
    """Compute Statistics reports from the disposable Session index.

    Every read reconciles canonical Sessions into the shared
    :class:`StatisticsIndex` and aggregates there in SQL; unchanged
    transcripts are never loaded and no projection is kept in memory between
    reads. Pass the runtime's shared ``index`` so every Statistics reader uses
    one owner of the index file; without it the service owns a private one.

    Project sessions feed the same report as identity sessions; a project agent
    appears under its outer address form ``agent@project`` so it stays distinct
    from the bare identity id and from the same agent in another project.
    Project and identity scopes are distinct Session addresses, so the same
    Session id under both is two Sessions, never a double count.

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
        *,
        pricing_lookup: Callable[[str], TokenPricing | None] | None = None,
        index: StatisticsIndex | None = None,
        usage_recorder: UsageRecorder | None = None,
    ) -> None:
        self._sessions = chat_sessions
        self._agents = agents
        self._projects = projects
        self._skill_inventory = skill_inventory
        self._pricing_lookup = pricing_lookup
        self._index = index if index is not None else StatisticsIndex(Path(chat_sessions.data_dir))
        self._usage_recorder = usage_recorder

    def warm_index(self) -> None:
        """Reconcile the disposable index without building a report."""
        self._import_session_usage()
        scopes = _index_scopes(self._statistics_scopes(), self._extension_sessions())
        self._index.read(
            self._sessions, scopes, lambda _view: None, usage_recorder=self._usage_recorder
        )

    async def warm_index_async(self) -> None:
        """``warm_index`` on the index database's worker pool."""
        await self._index.run_async(self.warm_index)

    async def report_async(
        self, *, since: datetime | None = None, until: datetime | None = None
    ) -> StatisticsReport:
        """``report`` on the index database's worker pool."""
        return await self._index.run_async(self.report, since=since, until=until)

    async def run_activity_async(self, *, since: datetime, until: datetime) -> RunActivityReport:
        """``run_activity`` on the index database's worker pool."""
        return await self._index.run_async(self.run_activity, since=since, until=until)

    def report(
        self, *, since: datetime | None = None, until: datetime | None = None
    ) -> StatisticsReport:
        """Reconcile all Session scopes and return the aggregated report."""
        self._import_session_usage()
        scopes = self._statistics_scopes()
        extension_sessions = self._extension_sessions()

        def consume(view: IndexView) -> ReportBuilder:
            builder = ReportBuilder(since=since, until=until, pricing_lookup=self._pricing_lookup)
            for scope in scopes:
                builder.register_scope(agent_id=scope.agent_id, project_id=scope.project_id)
                surviving: list[JsonObject] = []
                for indexed, session_id in _surviving(view, scope):
                    _add_unit(builder, scope, session_id, indexed)
                    surviving.append(indexed.summary)
                builder.register_agent(scope.display_key, surviving)
            # Extension-owned Sessions count once per owner under its reserved
            # actor key, never under their synthetic participant Agent ids.
            owner_summaries: dict[str, list[JsonObject]] = {}
            for entry in extension_sessions:
                builder.register_scope(agent_id=None, project_id=entry.scope.project_id)
                surviving = owner_summaries.setdefault(entry.key.owner_name, [])
                for indexed, session_id in _surviving(view, entry.scope):
                    _add_unit(builder, entry.scope, session_id, indexed, extension=entry.key)
                    surviving.append(indexed.summary)
            for owner_name, summaries in owner_summaries.items():
                if summaries:
                    builder.register_agent(extension_actor_key(owner_name), summaries)
            builder.aggregate(view.connection, durable_usage=self._usage_recorder is not None)
            return builder

        builder = self._index.read(
            self._sessions,
            _index_scopes(scopes, extension_sessions),
            consume,
            usage_recorder=self._usage_recorder,
        )
        return builder.build(self._skill_inventory)

    def run_activity(
        self,
        *,
        since: datetime,
        until: datetime,
    ) -> RunActivityReport:
        """Return persisted Runs whose execution overlaps the selected interval."""
        self._import_session_usage()
        scopes = _index_scopes(self._statistics_scopes(), self._extension_sessions())

        def consume(view: IndexView) -> tuple[int, list[RunActivity]]:
            units = [
                ReportUnit(
                    display_key=scope.display_key,
                    session_key=indexed.session_key,
                    session_id=session_id,
                    title=_title(indexed.summary),
                    address=SessionAddress(scope.project_id, scope.agent_id, session_id),
                )
                for scope in scopes
                for indexed, session_id in _surviving(view, scope)
            ]
            total, runs = load_run_activity(
                view.connection, units, since=since, until=until, limit=MAX_RUN_ACTIVITY
            )
            if self._usage_recorder is not None:
                runs = account_run_activity(view.connection, runs, units)
            return total, runs

        total_runs, runs = self._index.read(
            self._sessions, scopes, consume, usage_recorder=self._usage_recorder
        )
        return RunActivityReport(
            generated_at=datetime.now(UTC).isoformat(),
            window=WindowInfo(since=since.isoformat(), until=until.isoformat()),
            total_runs=total_runs,
            truncated=total_runs > MAX_RUN_ACTIVITY,
            runs=runs,
        )

    async def group_usage(
        self,
        *,
        owner_name: str,
        group_id: str,
        query: JsonObject | None = None,
    ) -> JsonObject:
        """Return a bounded, owner-exact usage projection for an Extension group."""
        return await self._index.run_async(
            self._group_usage, owner_name, group_id, dict(query or {})
        )

    def _group_usage(self, owner_name: str, group_id: str, query: JsonObject) -> JsonObject:
        self._import_session_usage()
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
        report, participant_reports = self._group_report(owner_name, group_id, records)
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
            "participants": [
                {
                    "participant_id": peer_id,
                    "usage": asdict(peer_report.usage),
                    "tools": asdict(peer_report.tools),
                    "compactions": asdict(peer_report.compactions),
                    "runs": asdict(peer_report.runs),
                }
                for peer_id, peer_report in participant_reports.items()
            ],
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

    def _group_report(
        self, owner_name: str, group_id: str, records: list[OwnedRunRecord]
    ) -> tuple[StatisticsReport, dict[str, StatisticsReport]]:
        summaries = {
            owned.address: extension_session_summary(owned)
            for owned in self._sessions.list_owned_session_summaries(
                owner_name=owner_name, group_id=group_id, metadata_keys=("seen_skills",)
            )
        }
        by_address: dict[SessionAddress, list[OwnedRunRecord]] = {}
        for record in records:
            by_address.setdefault(record.address, []).append(record)

        def consume(view: IndexView) -> tuple[StatisticsReport, dict[str, StatisticsReport]]:
            # Only an owned Run of the Session generation it was recorded in
            # counts, and only its own records: a reused Session is never
            # treated as wholly owned.
            owned: list[OwnedRunRecord] = []
            slices: list[tuple[int, str]] = []
            for address, address_records in by_address.items():
                indexed = view.session(address.project_id, address.agent_id, address.session_id)
                if indexed is None:
                    continue
                for record in address_records:
                    if indexed.generation_id == record.generation_id:
                        owned.append(record)
                        slices.append((indexed.session_key, record.run_id))
            present = materialize_run_slices(view.connection, slices)
            overall = _group_builder()
            participants: dict[str, ReportBuilder] = {}
            for position, record in enumerate(owned):
                if position not in present and self._usage_recorder is None:
                    continue
                display_key = record.owner.participant_id
                unit = ReportUnit(
                    display_key=display_key,
                    session_key=position,
                    session_id=record.address.session_id,
                    address=record.address,
                    run_id=record.run_id,
                )
                peer = participants.setdefault(display_key, _group_builder())
                for target in (overall, peer):
                    target.register_agent(display_key, [{"id": record.address.session_id}])
                    target.add_unit(unit)
            overall.aggregate(
                view.connection, durable_usage=self._usage_recorder is not None, group_usage=True
            )
            for peer in participants.values():
                peer.aggregate(
                    view.connection,
                    durable_usage=self._usage_recorder is not None,
                    group_usage=True,
                )
            return overall.build(None), {
                peer_id: peer.build(None) for peer_id, peer in participants.items()
            }

        return self._index.read(
            self._sessions,
            _owner_scopes(records, summaries),
            consume,
            prune=False,
            usage_recorder=self._usage_recorder,
        )

    def _import_session_usage(self) -> None:
        if self._usage_recorder is not None:
            self._usage_recorder.import_session_history(self._sessions)

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

    def _extension_sessions(self) -> tuple[_ExtensionSession, ...]:
        """Discover every live Extension-owned participant Session in one read."""
        entries: list[_ExtensionSession] = []
        for owned in self._sessions.list_owned_session_summaries(metadata_keys=("seen_skills",)):
            summary = extension_session_summary(owned)
            created_at = summary.get("created_at")
            entries.append(
                _ExtensionSession(
                    scope=StatisticsScope(
                        project_id=owned.address.project_id,
                        agent_id=owned.address.agent_id,
                        display_key=extension_actor_key(owned.owner_name),
                        summaries=(summary,),
                    ),
                    key=ExtensionSliceKey(
                        owner_name=owned.owner_name,
                        group_id=owned.group_id,
                        group_title=owned.group_title,
                        participant_id=owned.participant_id,
                        participant_name=owned.participant_name,
                        model=owned.model,
                        session_id=owned.address.session_id,
                        created_at=created_at if isinstance(created_at, str) else None,
                    ),
                )
            )
        return tuple(entries)


def _index_scopes(
    scopes: tuple[StatisticsScope, ...], extension_sessions: tuple[_ExtensionSession, ...]
) -> tuple[StatisticsScope, ...]:
    return (*scopes, *(entry.scope for entry in extension_sessions))


def _surviving(view: IndexView, scope: StatisticsScope) -> list[tuple[IndexedSession, str]]:
    """Return the scope's listed Sessions that survived reconciliation, in listing order."""
    surviving: list[tuple[IndexedSession, str]] = []
    for summary in scope.summaries:
        session_id = str(summary["id"])
        indexed = view.session(scope.project_id, scope.agent_id, session_id)
        if indexed is not None:
            surviving.append((indexed, session_id))
    return surviving


def _add_unit(
    builder: ReportBuilder,
    scope: StatisticsScope,
    session_id: str,
    indexed: IndexedSession,
    *,
    extension: ExtensionSliceKey | None = None,
) -> None:
    created_at = indexed.summary.get("created_at")
    builder.add_unit(
        ReportUnit(
            display_key=scope.display_key,
            session_key=indexed.session_key,
            session_id=session_id,
            title=_title(indexed.summary),
            extension=extension,
            address=SessionAddress(scope.project_id, scope.agent_id, session_id),
        ),
        created_at=created_at if isinstance(created_at, str) else None,
        offered_skills=offered_skill_names(indexed.summary),
    )


def _title(summary: JsonObject) -> str | None:
    title = summary.get("title")
    return title if isinstance(title, str) else None


def _group_builder() -> ReportBuilder:
    # Group usage exposes usage, Tools, Compactions and Runs only.
    return ReportBuilder(since=None, until=None, include_costs=False, include_skills=False)
