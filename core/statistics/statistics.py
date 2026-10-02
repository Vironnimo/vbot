"""Statistics service: report access and owner-scoped usage over the derived index."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from core.models.pricing import TokenPricing
from core.projects.address import format_agent_address
from core.sessions import OwnedRunRecord, SessionAddress
from core.statistics._extensions import ExtensionSliceKey, extension_actor_key
from core.statistics._group_usage import OwnedRun, group_usage
from core.statistics._run_activity import load_run_activity
from core.statistics._sections import (
    SECTION_NAMES,
    LiveSession,
    ReportContext,
    ReportWindow,
    build_sections,
)
from core.statistics._sources import (
    AgentDirectory,
    ProjectDirectory,
    SessionSource,
    _owner_scopes,
    extension_session_summary,
)
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
    WindowInfo,
)
from core.statistics.skills import (
    SkillInventorySource,
    SkillUse,
    _ResolvedInventory,
    counts_as_skill_use,
    empty_inventory,
    load_skill_use,
    offered_skill_names,
    resolve_inventory,
)
from core.utils.timestamps import format_canonical_timestamp

if TYPE_CHECKING:
    from core.usage import UsageRecorder


@dataclass(frozen=True)
class _ExtensionSession:
    """One owner-managed participant Session: its index scope and report slice."""

    scope: StatisticsScope
    key: ExtensionSliceKey


MAX_RUN_ACTIVITY = 200

# The sections of ``StatisticsService.report``, in report order.
REPORT_SECTIONS = SECTION_NAMES


def _utc_now() -> datetime:
    return datetime.now(UTC)


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
    ``clock`` supplies the report time (the open end of a window and the
    previous window's length); it defaults to the wall clock.
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
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._sessions = chat_sessions
        self._agents = agents
        self._projects = projects
        self._skill_inventory = skill_inventory
        self._pricing_lookup = pricing_lookup
        self._index = index if index is not None else StatisticsIndex(Path(chat_sessions.data_dir))
        self._usage_recorder = usage_recorder
        self._clock = clock

    def warm_index(self) -> None:
        """Reconcile the disposable index without building a report."""
        self._import_session_usage()
        scopes = _index_scopes(self._statistics_scopes(), self._extension_sessions())
        self._index.read(
            self._sessions,
            scopes,
            lambda _view: None,
            usage_recorder=self._usage_recorder,
            pricing_lookup=self._pricing_lookup,
        )

    async def warm_index_async(self) -> None:
        """``warm_index`` on the index database's worker pool."""
        await self._index.run_async(self.warm_index)

    async def report_async(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        timezone: str = "UTC",
        sections: Iterable[str] | None = None,
    ) -> JsonObject:
        """``report`` on the index database's worker pool."""
        return await self._index.run_async(
            self.report, since=since, until=until, timezone=timezone, sections=sections
        )

    async def run_activity_async(self, *, since: datetime, until: datetime) -> RunActivityReport:
        """``run_activity`` on the index database's worker pool."""
        return await self._index.run_async(self.run_activity, since=since, until=until)

    def report(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        timezone: str = "UTC",
        sections: Iterable[str] | None = None,
    ) -> JsonObject:
        """Reconcile all Session scopes once and return the requested report sections.

        ``since`` is floored and ``until`` ceiled to whole UTC hours; day
        series use calendar days of the IANA ``timezone``. ``sections`` names
        sections from ``REPORT_SECTIONS`` (default: all). The result is
        ``{generated_at, window: {since, until, timezone}, <section>: {...}}``.
        Unknown sections, an unknown zone or ``since`` after ``until`` raise
        ``ValueError``.
        """
        names = report_sections(sections)
        now = self._clock()
        window = ReportWindow.create(since=since, until=until, timezone=timezone, now=now)
        self._import_session_usage()
        scopes = self._statistics_scopes()
        extension_sessions = self._extension_sessions()
        inventory = self._resolve_skills(scopes, extension_sessions) if "skills" in names else None

        def consume(view: IndexView) -> JsonObject:
            context = ReportContext(
                view.connection,
                window,
                _live_sessions(view, scopes, extension_sessions),
                skill_inventory=inventory,
            )
            return build_sections(context, names)

        built = self._index.read(
            self._sessions,
            _index_scopes(scopes, extension_sessions),
            consume,
            usage_recorder=self._usage_recorder,
            pricing_lookup=self._pricing_lookup,
        )
        return {
            "generated_at": format_canonical_timestamp(now),
            "window": window.echo(),
            **built,
        }

    def _resolve_skills(
        self,
        scopes: tuple[StatisticsScope, ...],
        extension_sessions: tuple[_ExtensionSession, ...],
    ) -> _ResolvedInventory:
        # Extension participant Agents own no private Skills, but their
        # Project contributes its Skills.
        if self._skill_inventory is None:
            return empty_inventory()
        return resolve_inventory(
            self._skill_inventory,
            agent_ids=frozenset(scope.agent_id for scope in scopes),
            project_ids=frozenset(
                project_id
                for project_id in (
                    *(scope.project_id for scope in scopes),
                    *(entry.scope.project_id for entry in extension_sessions),
                )
                if project_id is not None
            ),
        )

    def run_activity(
        self,
        *,
        since: datetime,
        until: datetime,
    ) -> RunActivityReport:
        """Return persisted Runs whose execution overlaps the selected interval."""
        self._import_session_usage()
        scopes = self._statistics_scopes()
        extension_sessions = self._extension_sessions()

        def consume(view: IndexView) -> tuple[int, list[RunActivity]]:
            sessions = {
                session.session_key: session
                for session in _live_sessions(view, scopes, extension_sessions)
            }
            return load_run_activity(
                view.connection, sessions, since=since, until=until, limit=MAX_RUN_ACTIVITY
            )

        total_runs, runs = self._index.read(
            self._sessions,
            _index_scopes(scopes, extension_sessions),
            consume,
            usage_recorder=self._usage_recorder,
            pricing_lookup=self._pricing_lookup,
        )
        return RunActivityReport(
            generated_at=datetime.now(UTC).isoformat(),
            window=WindowInfo(since=since.isoformat(), until=until.isoformat()),
            total_runs=total_runs,
            truncated=total_runs > MAX_RUN_ACTIVITY,
            runs=runs,
        )

    def skill_usage(self) -> dict[tuple[str, str], SkillUse]:
        """Return each Agent's Skill use by ``(agent id, Skill name)``.

        Reconciles the index, then reads every surviving identity and Project
        Session of the roster Agents that counts as use (background Sessions do
        not; see ``core.statistics.skills.counts_as_skill_use``). A Project
        Session counts for its bare Agent id. Extension participant Sessions
        belong to no roster Agent and are left out. No time window applies.
        """
        self._import_session_usage()
        scopes = self._statistics_scopes()

        def consume(view: IndexView) -> dict[tuple[str, str], SkillUse]:
            owners = {
                indexed.session_key: scope.agent_id
                for scope in scopes
                for indexed, _session_id in _surviving(view, scope)
                if counts_as_skill_use(indexed.summary)
            }
            return load_skill_use(view.connection, owners)

        # Every scope is passed so the read never prunes Extension Sessions.
        return self._index.read(
            self._sessions,
            _index_scopes(scopes, self._extension_sessions()),
            consume,
            usage_recorder=self._usage_recorder,
            pricing_lookup=self._pricing_lookup,
        )

    async def skill_usage_async(self) -> dict[tuple[str, str], SkillUse]:
        """``skill_usage`` on the index database's worker pool."""
        return await self._index.run_async(self.skill_usage)

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
        activity, participants = self._group_report(owner_name, group_id, records)
        return {
            "group_id": group_id,
            "participant_id": participant_id,
            "participant_count": len({record.owner.participant_id for record in records}),
            "owned_run_count": len(records),
            "activity": activity,
            "participants": [
                {"participant_id": peer_id, "activity": peer_activity}
                for peer_id, peer_activity in participants.items()
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
    ) -> tuple[JsonObject, dict[str, JsonObject]]:
        summaries = {
            owned.address: extension_session_summary(owned)
            for owned in self._sessions.list_owned_session_summaries(
                owner_name=owner_name, group_id=group_id, metadata_keys=("seen_skills",)
            )
        }
        by_address: dict[SessionAddress, list[OwnedRunRecord]] = {}
        for record in records:
            by_address.setdefault(record.address, []).append(record)

        def consume(view: IndexView) -> tuple[JsonObject, dict[str, JsonObject]]:
            # Only an owned Run of the Session generation it was recorded in
            # counts: a reused Session is never treated as wholly owned.
            owned: list[OwnedRun] = []
            for address, address_records in by_address.items():
                indexed = view.session(address.project_id, address.agent_id, address.session_id)
                if indexed is None:
                    continue
                owned.extend(
                    OwnedRun(indexed.session_key, record.run_id, record.owner.participant_id)
                    for record in address_records
                    if indexed.generation_id == record.generation_id
                )
            return group_usage(view.connection, owned)

        return self._index.read(
            self._sessions,
            _owner_scopes(records, owner_name, summaries, self._sessions.summary),
            consume,
            prune=False,
            usage_recorder=self._usage_recorder,
            pricing_lookup=self._pricing_lookup,
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
            for agent in self._agents.list_with_builtins()
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
                        owner_name=owned.owner_name,
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


def _live_sessions(
    view: IndexView,
    scopes: tuple[StatisticsScope, ...],
    extension_sessions: tuple[_ExtensionSession, ...],
) -> list[LiveSession]:
    """The listed Sessions that survived reconciliation, in report listing order.

    Extension-owned Sessions count once per owner under its reserved actor
    key, never under their synthetic participant Agent ids.
    """
    entries: list[tuple[StatisticsScope, ExtensionSliceKey | None]] = [
        (scope, None) for scope in scopes
    ]
    entries.extend((entry.scope, entry.key) for entry in extension_sessions)
    sessions: list[LiveSession] = []
    for scope, extension in entries:
        for indexed, session_id in _surviving(view, scope):
            created_at = indexed.summary.get("created_at")
            sessions.append(
                LiveSession(
                    session_key=indexed.session_key,
                    display_key=scope.display_key,
                    address=SessionAddress(scope.project_id, scope.agent_id, session_id),
                    title=_title(indexed.summary),
                    created_at=created_at if isinstance(created_at, str) else None,
                    offered_skills=tuple(offered_skill_names(indexed.summary)),
                    skill_use=counts_as_skill_use(indexed.summary),
                    extension=extension,
                )
            )
    return sessions


def report_sections(sections: Iterable[str] | None) -> tuple[str, ...]:
    """Validate requested section names; ``None`` selects every section.

    Unknown names and an empty selection raise ``ValueError``.
    """
    if sections is None:
        return REPORT_SECTIONS
    requested = list(sections)
    unknown = sorted({str(name) for name in requested} - set(REPORT_SECTIONS))
    if unknown:
        raise ValueError(f"unknown statistics sections: {', '.join(unknown)}")
    if not requested:
        raise ValueError("at least one statistics section is required")
    return tuple(name for name in REPORT_SECTIONS if name in requested)


def _title(summary: JsonObject) -> str | None:
    title = summary.get("title")
    return title if isinstance(title, str) else None
