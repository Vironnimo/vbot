"""Statistics RPC handler.

``statistics.report`` returns a full read-only :class:`StatisticsReport` computed
on demand from persisted Sessions (see ``.vorch/domain-maps/statistics.md``). It accepts
an optional ``{since, until}`` ISO-8601 UTC window and contains no opaque provider
metadata by construction (no raw tool arguments, no reasoning data).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from core.skills import project_skill_origin, scan_skill_names
from core.statistics import StatisticsService
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.validation import _reject_unsupported

JsonObject = dict[str, Any]

_SUPPORTED_FIELDS = {"since", "until"}


def _statistics_report(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, _SUPPORTED_FIELDS, "statistics.report")

    since = _optional_utc_timestamp(params, "since")
    until = _optional_utc_timestamp(params, "until")
    if since is not None and until is not None and since > until:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.since must not be after params.until")

    report = _statistics_service(state).report(since=since, until=until)
    return report.to_dict()


def _optional_utc_timestamp(params: JsonObject, key: str) -> datetime | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.{key} must be an ISO 8601 timestamp string",
        )
    parsed = _parse_iso_utc(value)
    if parsed is None:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.{key} must be an ISO 8601 timestamp string",
        )
    return parsed


def _parse_iso_utc(value: str) -> datetime | None:
    try:
        normalized = value.removesuffix("Z") + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class _RuntimeSkillInventory:
    """Thin ``SkillInventorySource`` adapter over the runtime for the skills join.

    Wiring only: it reads the current skill inventory live from the runtime so
    "never used" is authoritative against the same registry every accessor sees.
    Global (bundled+global) skills carry the runtime-tagged origin; a project's
    own skills are tagged ``project:<display-name>`` here (the runtime scan
    returns them untagged); an agent's private home yields its bare skill names.
    """

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    def global_skills(self) -> list[tuple[str, str | None]]:
        return [
            (skill.name, skill.origin) for skill in self._runtime.skills_for(None, None).list_all()
        ]

    def agent_skill_names(self, agent_id: str) -> frozenset[str]:
        skills_dir = self._runtime.agent_skills_dir(agent_id)
        if not skills_dir.is_dir():
            return frozenset()
        return scan_skill_names(skills_dir)

    def project_skills(self, project_id: str) -> list[tuple[str, str | None]]:
        origin = project_skill_origin(self._runtime.projects.get(project_id).display_name)
        return [(skill.name, origin) for skill in self._runtime.project_own_skills(project_id)]


def _statistics_service(state: Any) -> StatisticsService:
    service = getattr(state, "statistics_service", None)
    if service is not None:
        return cast(StatisticsService, service)
    service = StatisticsService(
        state.runtime.chat_sessions,
        state.runtime.agents,
        state.runtime.projects,
        _RuntimeSkillInventory(state.runtime),
    )
    state.statistics_service = service
    return service


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return the statistics RPC handlers."""

    return {"statistics.report": _statistics_report}
