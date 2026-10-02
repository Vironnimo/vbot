"""Statistics RPC handler.

``statistics.report`` returns the requested report sections (default: all of
``REPORT_SECTIONS``) for an optional hour-aligned window, with day series in an
IANA timezone (default: the Settings timezone). ``statistics.run_activity``
returns the bounded Run projection overlapping a required time window for
Provider-limit correlation. Both run on the Statistics index database's worker
pool and expose no raw Tool arguments or Reasoning.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from core.settings.settings import SettingsValidationError, validate_timezone_name
from core.skills import project_skill_origin, scan_skill_names
from core.statistics import REPORT_SECTIONS, StatisticsService
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.validation import _reject_unsupported

JsonObject = dict[str, Any]

_SUPPORTED_FIELDS = {"since", "until", "timezone", "sections"}
_RUN_ACTIVITY_SUPPORTED_FIELDS = {"since", "until"}


async def _statistics_report(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, _SUPPORTED_FIELDS, "statistics.report")

    since = _optional_utc_timestamp(params, "since")
    until = _optional_utc_timestamp(params, "until")
    if since is not None and until is not None and since > until:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.since must not be after params.until")

    timezone = _report_timezone(state, params)
    sections = _report_sections(params)
    return await statistics_service(state).report_async(
        since=since, until=until, timezone=timezone, sections=sections
    )


def _report_timezone(state: Any, params: JsonObject) -> str:
    value = params.get("timezone")
    if value is None:
        return cast(str, state.runtime.timezone_name())
    try:
        return validate_timezone_name(value, label="params.timezone")
    except SettingsValidationError as error:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(error)) from error


def _report_sections(params: JsonObject) -> tuple[str, ...] | None:
    value = params.get("sections")
    if value is None:
        return None
    if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST, "params.sections must be a non-empty list of section names"
        )
    unknown = sorted(set(value) - set(REPORT_SECTIONS))
    if unknown:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unknown statistics sections: {', '.join(unknown)}; "
            f"expected any of {', '.join(REPORT_SECTIONS)}",
        )
    return tuple(name for name in REPORT_SECTIONS if name in value)


async def _statistics_run_activity(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(
        params,
        _RUN_ACTIVITY_SUPPORTED_FIELDS,
        "statistics.run_activity",
    )
    since = _required_utc_timestamp(params, "since")
    until = _required_utc_timestamp(params, "until")
    if since > until:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.since must not be after params.until")
    report = await statistics_service(state).run_activity_async(since=since, until=until)
    return report.to_dict()


def _required_utc_timestamp(params: JsonObject, key: str) -> datetime:
    parsed = _optional_utc_timestamp(params, key)
    if parsed is None:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.{key} must be an ISO 8601 timestamp string",
        )
    return parsed


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
        parsed = datetime.fromisoformat(value)
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


def statistics_service(state: Any) -> StatisticsService:
    service = getattr(state, "statistics_service", None)
    if service is not None:
        return cast(StatisticsService, service)
    service = StatisticsService(
        state.runtime.chat_sessions,
        state.runtime.agents,
        state.runtime.projects,
        _RuntimeSkillInventory(state.runtime),
        pricing_lookup=state.runtime.models.pricing_for,
        index=state.runtime.statistics_index,
        usage_recorder=state.runtime.usage_recorder,
    )
    state.statistics_service = service
    return service


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return the statistics RPC handlers."""

    return {
        "statistics.report": _statistics_report,
        "statistics.run_activity": _statistics_run_activity,
    }
