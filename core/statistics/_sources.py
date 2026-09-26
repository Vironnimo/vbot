"""Statistics source contracts and owner-managed Session scopes."""

from __future__ import annotations

import builtins
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from core.sessions import (
    ChatSession,
    OwnedRunRecord,
    OwnedSessionSummary,
    SessionAddress,
)
from core.statistics.index import (
    StatisticsScope,
)
from core.statistics.report import JsonObject

if TYPE_CHECKING:
    from core.database import Database


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

    @property
    def database(self) -> Database: ...

    def usage_history(
        self, after_entry_key: int = 0, *, limit: int = 1000
    ) -> tuple[JsonObject, ...]: ...

    def list_summaries(
        self,
        agent_id: str,
        project_id: str | None = None,
        *,
        metadata_keys: Sequence[str] = (),
    ) -> list[JsonObject]: ...

    def get(self, address: SessionAddress) -> ChatSession: ...

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

    def list_owned_session_summaries(
        self,
        *,
        owner_name: str | None = None,
        group_id: str | None = None,
        metadata_keys: Sequence[str] = (),
    ) -> list[OwnedSessionSummary]: ...


def extension_session_summary(owned: OwnedSessionSummary) -> JsonObject:
    """Label an owner-managed Session by its group title and participant name.

    Participant Sessions carry no meaningful title of their own; report rows
    show which group and participant produced the activity instead.
    """
    summary = dict(owned.summary)
    label = " · ".join(part for part in (owned.group_title, owned.participant_name) if part)
    if label:
        summary["title"] = label
    return summary


def _owner_scopes(
    records: Sequence[OwnedRunRecord],
    summaries: Mapping[SessionAddress, JsonObject],
) -> tuple[StatisticsScope, ...]:
    """Group owned Run Sessions into index scopes.

    ``summaries`` supplies the same labelled summaries the full report indexes,
    so a group read never rewrites a shared index row with a thinner summary.
    """
    grouped: dict[tuple[str | None, str], list[JsonObject]] = {}
    seen: set[SessionAddress] = set()
    for record in records:
        if record.address in seen:
            continue
        seen.add(record.address)
        grouped.setdefault((record.address.project_id, record.address.agent_id), []).append(
            summaries.get(record.address, {"id": record.address.session_id})
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
