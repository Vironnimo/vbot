"""Shared `/status` report rendering for the command dispatcher and status tool.

Pure presentation: every resolver here degrades gracefully to placeholders so a
minimally wired caller renders a readable card instead of failing. The command
dispatcher and the ``status`` Tool both call :func:`build_status_reply`; this
module is deliberately its single home (see tests asserting one source of truth).

The dispatcher workflow that gathers inputs lives in
:mod:`core.chat.commands`; this module never touches sessions or queues itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from core.chat.messages import ChatMessage
from core.chat.usage import aggregate_session_usage
from core.providers.providers import resolve_effective_context_window
from core.providers.reasoning import (
    REASONING_INTENT_BUDGET,
    REASONING_INTENT_DEFAULT,
    REASONING_INTENT_OFF,
    REASONING_INTENT_ON,
    resolve_reasoning_intent,
)
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.models.models import ModelRegistry
    from core.projects import ProjectStore, RuntimeAgent
    from core.providers.providers import ProviderRegistry
    from core.runs import ChatRunManager
else:
    ModelRegistry = Any
    ProjectStore = Any
    ProviderRegistry = Any
    RuntimeAgent = Any

_LOGGER = get_logger("chat.status")

STATUS_PLACEHOLDER = "—"

# Reported "actual" reasoning state for a model steered by a thinking toggle or a
# token budget rather than an effort ladder: there is no effort level to show, so
# ``/status`` reports whether reasoning is on or off for the selection.
REASONING_STATE_ON = "on"
REASONING_STATE_OFF = "off"
_STATUS_TIME_FORMAT = "%Y-%m-%d %H:%M:%S %Z"
_CACHE_PERCENT_SCALE = 100
_CACHE_HIT_RATE_DECIMALS = 1
_STATUS_MODEL_DISPLAY_OVERRIDE: ContextVar[str | None] = ContextVar(
    "status_model_display_override",
    default=None,
)

StatusActivityName = Literal["idle", "running"]


@dataclass(frozen=True)
class StatusActivity:
    """Run activity summary for one Session."""

    activity: StatusActivityName
    run_id: str | None
    created_at: str | None
    updated_at: str | None


@dataclass(frozen=True)
class StatusModelDetails:
    """Model facts needed to render a status reply.

    ``reasoning_levels`` is the model's effective effort ladder (empty when the
    model has no feed ladder), ``reasoning_control`` its wire control kind
    (``levels`` / ``on_off`` / ``budget`` / ``None``), and ``reasoning_budget_max``
    the max thinking-token budget for a ``budget`` model (``None`` when unknown).
    Together they let ``resolve_actual_thinking_effort`` report the *actual*
    reasoning sent on the wire — a snapped effort for a ladder, ``on``/``off`` for
    a toggle, or the rendered token budget for a budget model.

    ``recommended_temperature`` and ``provider_default_temperature`` feed
    ``resolve_status_temperature`` so the temperature line reports the resolved
    value with its source rather than only the configured agent field.
    """

    context_window: int | None
    display_name: str | None
    reasoning_levels: tuple[str, ...] = ()
    reasoning_control: str | None = None
    reasoning_budget_max: int | None = None
    recommended_temperature: float | None = None
    provider_default_temperature: float | None = None


def resolve_status_model_details(
    agent: RuntimeAgent | None,
    models: ModelRegistry | None,
    providers: ProviderRegistry | None = None,
    local_context_windows: Mapping[str, Any] | None = None,
) -> StatusModelDetails:
    """Resolve model facts for status output from the model registry.

    Returns context window, display name, and the effective reasoning-effort
    ladder. A missing agent/registry/model yields empty details so status
    rendering degrades to placeholders instead of failing.

    ``context_window`` is the *effective* window (user-set/capped for
    flagged-local models, else the read-side default chain — see
    :func:`resolve_effective_context_window`), so ``/status`` reports the budget
    compaction actually uses rather than ``unknown`` for a window-less model.
    It stays ``None`` only when no model could be resolved at all.
    """
    if agent is None or models is None:
        return StatusModelDetails(context_window=None, display_name=None)

    provider_id, model_id = _parse_registry_model_key(agent.model)
    if provider_id is None or model_id is None:
        return StatusModelDetails(context_window=None, display_name=None)

    try:
        model = models.get(provider_id, model_id)
    except KeyError:
        _LOGGER.warning(
            "Model registry entry missing for %r/%r while building status",
            provider_id,
            model_id,
        )
        return StatusModelDetails(context_window=None, display_name=None)
    except Exception:
        _LOGGER.error(
            "Failed model registry lookup for %r/%r while building status",
            provider_id,
            model_id,
            exc_info=True,
        )
        return StatusModelDetails(context_window=None, display_name=None)

    provider_config = _status_provider_config(providers, provider_id)
    return StatusModelDetails(
        context_window=resolve_effective_context_window(
            model.context_window,
            provider_config,
            model_metadata=model.metadata,
            model_key=f"{provider_id}/{model_id}",
            local_context_windows=local_context_windows,
        ),
        display_name=model.name,
        reasoning_levels=tuple(model.capabilities.reasoning.levels),
        reasoning_control=model.capabilities.reasoning.control,
        reasoning_budget_max=model.capabilities.reasoning.budget_max,
        recommended_temperature=model.recommended_temperature,
        provider_default_temperature=_provider_default_temperature(provider_config),
    )


def _provider_default_temperature(provider_config: Any) -> float | None:
    """Read the provider-config ``defaults.temperature``, None when absent."""

    defaults = getattr(provider_config, "defaults", None)
    if not isinstance(defaults, Mapping):
        return None
    value = defaults.get("temperature")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _status_provider_config(providers: ProviderRegistry | None, provider_id: str) -> Any:
    """Return the ProviderConfig for the read-side window default, or None."""
    if providers is None:
        return None
    try:
        return providers.get(provider_id)
    except (KeyError, AttributeError):
        return None


def resolve_status_project_label(
    projects: ProjectStore | None,
    project_id: str | None,
) -> str | None:
    """Return a display label for the session's project, or ``None`` for identity.

    An identity session (``project_id is None``) has no project, so status renders
    the placeholder. A project session resolves the project's display name as
    ``"<display name> (<id>)"``; it degrades to the bare id when the store is
    absent or the project can't be loaded — the stable id is still informative.
    """
    if project_id is None:
        return None
    if projects is None:
        return project_id
    try:
        project = projects.get(project_id)
    except Exception:
        _LOGGER.warning(
            "Failed to load project %r while building status reply",
            project_id,
            exc_info=True,
        )
        return project_id
    return f"{project.display_name} ({project_id})"


def resolve_actual_thinking_effort(
    selected_effort: str | None,
    reasoning_levels: tuple[str, ...],
    reasoning_control: str | None = None,
    reasoning_budget_max: int | None = None,
) -> str | None:
    """Return the reasoning actually sent on the wire for the selected effort.

    Reuses :func:`resolve_reasoning_intent` — the same policy the adapters render
    — so ``/status`` reports exactly what reaches the provider:

    * ``levels`` control (or any non-empty ladder): the snapped effort level.
    * ``budget`` control: ``"on (<N> tokens)"`` — the rendered token budget,
      scaled by ``reasoning_budget_max`` when seeded (else the absolute ladder).
    * ``on_off`` control: ``"on"`` / ``"off"``.
    * Otherwise ``None`` (no effort selected, or no ladder/control to report —
      the adapter then applies its own floor, which is not visible here).
    """
    intent = resolve_reasoning_intent(
        supported=True,
        control=reasoning_control,
        levels=reasoning_levels,
        effort=selected_effort,
        budget_max=reasoning_budget_max,
        max_tokens=None,
    )
    if intent.kind == REASONING_INTENT_DEFAULT:
        return None
    if intent.kind == REASONING_INTENT_OFF:
        return REASONING_STATE_OFF
    if intent.kind == REASONING_INTENT_ON:
        return REASONING_STATE_ON
    if intent.kind == REASONING_INTENT_BUDGET:
        return f"{REASONING_STATE_ON} ({intent.budget_tokens:,} tokens)"
    return intent.effort_level


def resolve_status_temperature(
    agent_temperature: float | None,
    model_details: StatusModelDetails,
) -> str:
    """Render the resolved temperature with the tier that supplied it.

    Mirrors the chat resolution chain — explicit agent value, then the model's
    recommended temperature, then the provider-config default — and reports the
    API default when no tier has a value. Adapter-level sampling drops (active
    thinking, sampling-free models) are wire policy and stay invisible here.
    """
    if agent_temperature is not None:
        return f"{agent_temperature:g} (agent)"
    if model_details.recommended_temperature is not None:
        return f"{model_details.recommended_temperature:g} (model recommendation)"
    if model_details.provider_default_temperature is not None:
        return f"{model_details.provider_default_temperature:g} (provider default)"
    return "default"


def build_status_reply(
    agent: RuntimeAgent | None,
    messages: list[ChatMessage],
    context_window: int | None,
    started_at: datetime | None,
    model_display_name: str | None,
    activity: StatusActivity | None = None,
    actual_thinking_effort: str | None = None,
    project_label: str | None = None,
    temperature_status: str | None = None,
) -> str:
    """Build status text while applying an optional model-display override."""
    token = _STATUS_MODEL_DISPLAY_OVERRIDE.set(model_display_name)
    try:
        return build_status_text(
            agent,
            messages,
            context_window,
            started_at,
            activity,
            actual_thinking_effort=actual_thinking_effort,
            project_label=project_label,
            temperature_status=temperature_status,
        )
    finally:
        _STATUS_MODEL_DISPLAY_OVERRIDE.reset(token)


def build_status_text(
    agent: RuntimeAgent | None,
    messages: list[ChatMessage],
    context_window: int | None,
    started_at: datetime | None,
    activity: StatusActivity | None = None,
    actual_thinking_effort: str | None = None,
    project_label: str | None = None,
    temperature_status: str | None = None,
) -> str:
    """Build human-readable status text for the current session and runtime state.

    ``actual_thinking_effort`` is what reaches the wire after the model's ladder
    snaps the agent's selection (see :func:`resolve_actual_thinking_effort`); it
    is rendered alongside the selected effort so the two can differ visibly.
    ``temperature_status`` is the resolved temperature with its source (see
    :func:`resolve_status_temperature`); without it the line degrades to the
    configured agent value alone.
    ``project_label`` names the session's project (``None`` for an identity
    session, rendered as the placeholder).
    """
    now_utc = datetime.now(UTC)
    now_local = now_utc.astimezone()

    if agent is None:
        agent_summary = STATUS_PLACEHOLDER
        model_display = STATUS_PLACEHOLDER
        fallback_model = STATUS_PLACEHOLDER
        selected_thinking_effort = STATUS_PLACEHOLDER
        temperature = STATUS_PLACEHOLDER
    else:
        model_string = agent.model.strip() or STATUS_PLACEHOLDER
        agent_summary = f"{agent.name} ({model_string})"
        model_display = _STATUS_MODEL_DISPLAY_OVERRIDE.get() or _model_display_name(model_string)
        fallback_model = agent.fallback_model.strip() or STATUS_PLACEHOLDER
        selected_thinking_effort = _thinking_effort_text(agent.thinking_effort)
        temperature = (
            temperature_status
            if temperature_status is not None
            else _temperature_text(agent.temperature)
        )

    actual_thinking_effort_text = _actual_thinking_effort_text(actual_thinking_effort)
    context_usage = _context_usage_text(messages, context_window)
    last_request_cache = _last_request_cache_text(messages)
    session_cache = _session_cache_text(messages)
    session_started = _session_started_text(messages, now_utc)
    turn_count = _turn_count_text(messages)
    app_uptime = _app_uptime_text(started_at, now_utc)
    activity_name = activity.activity if activity is not None else STATUS_PLACEHOLDER
    run_created_at = activity.created_at if activity is not None else None
    run_updated_at = activity.updated_at if activity is not None else None

    lines = [
        f"Agent: {agent_summary}",
        f"Project: {project_label or STATUS_PLACEHOLDER}",
        f"Model display name: {model_display}",
        f"Fallback model: {fallback_model}",
        f"Selected thinking effort: {selected_thinking_effort}",
        f"Actual model thinking effort: {actual_thinking_effort_text}",
        f"Temperature: {temperature}",
        f"Activity: {activity_name}",
        f"Run created at: {run_created_at or STATUS_PLACEHOLDER}",
        f"Run updated at: {run_updated_at or STATUS_PLACEHOLDER}",
        f"Context usage: {context_usage}",
        f"Last request cache: {last_request_cache}",
        f"Session cache: {session_cache}",
        f"Session started: {session_started}",
        f"Turn count: {turn_count}",
        f"App uptime: {app_uptime}",
        f"Current time: {now_local.strftime(_STATUS_TIME_FORMAT)}",
    ]
    return "\n".join(lines)


def resolve_status_activity(
    chat_runs: ChatRunManager,
    agent_id: str,
    session_id: str,
    project_id: str | None,
) -> StatusActivity:
    """Return running/idle activity for one Session (project-scoped run key)."""
    run = chat_runs.active_run(agent_id=agent_id, session_id=session_id, project_id=project_id)
    if run is None:
        return StatusActivity(
            activity="idle",
            run_id=None,
            created_at=None,
            updated_at=None,
        )
    return StatusActivity(
        activity="running",
        run_id=run.id,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _model_display_name(model_string: str) -> str:
    _, model_id = _parse_registry_model_key(model_string)
    if model_id is None:
        return STATUS_PLACEHOLDER
    return model_id


def _thinking_effort_text(value: str | None) -> str:
    if value is None:
        return "default"
    return value.strip() or "default"


def _actual_thinking_effort_text(value: str | None) -> str:
    """Render the snapped wire effort, or a placeholder when it is not resolvable.

    ``None`` means there is nothing to report: no effort was selected (provider
    default) or the model exposes no ladder to snap against (the adapter floor is
    not visible here). The selected-effort line still shows the agent's choice.
    """
    if not value:
        return STATUS_PLACEHOLDER
    return value


def _temperature_text(value: float | None) -> str:
    if value is None:
        return "default"
    return f"{value:g}"


def _parse_registry_model_key(model_string: str) -> tuple[str | None, str | None]:
    normalized_model = _strip_pinned_connection_suffix(model_string.strip())
    provider_id, separator, model_id = normalized_model.partition("/")
    if not provider_id or not separator or not model_id:
        return None, None
    return provider_id, model_id


def _strip_pinned_connection_suffix(model_string: str) -> str:
    base_model, separator, _connection_id = model_string.rpartition("::")
    if separator and base_model:
        return base_model
    return model_string


def _context_usage_text(messages: list[ChatMessage], context_window: int | None) -> str:
    if context_window is None or context_window <= 0:
        return STATUS_PLACEHOLDER

    latest_usage = _latest_assistant_usage(messages)
    if latest_usage is None:
        return STATUS_PLACEHOLDER

    input_tokens, estimated = latest_usage
    prefix = "~" if estimated else ""
    return f"{prefix}{input_tokens} / {context_window}"


def _turn_count_text(messages: list[ChatMessage]) -> str:
    if not messages:
        return STATUS_PLACEHOLDER
    return str(sum(1 for message in messages if message.role == "user"))


def _latest_assistant_usage(messages: list[ChatMessage]) -> tuple[int, bool] | None:
    usage = _latest_assistant_usage_object(messages, require_input=True)
    if usage is None:
        return None
    input_tokens = _coerce_int(usage.get("input_tokens"))
    if input_tokens is None:
        return None
    return input_tokens, bool(usage.get("estimated"))


def _latest_assistant_usage_object(
    messages: list[ChatMessage],
    *,
    require_input: bool = False,
) -> dict[str, Any] | None:
    for message in reversed(messages):
        if message.role != "assistant" or not isinstance(message.usage, dict):
            continue
        if require_input and _coerce_int(message.usage.get("input_tokens")) is None:
            continue
        return message.usage
    return None


def _last_request_cache_text(messages: list[ChatMessage]) -> str:
    usage = _latest_assistant_usage_object(messages)
    if usage is None or usage.get("estimated") is True:
        return STATUS_PLACEHOLDER

    cache_data = _cache_data_from_usage(usage)
    if cache_data is None:
        return STATUS_PLACEHOLDER
    return _format_cache_data(cache_data)


def _session_cache_text(messages: list[ChatMessage]) -> str:
    totals = aggregate_session_usage(messages)
    cache_turns = _coerce_non_negative_int(totals.get("cache_turns")) or 0
    if cache_turns <= 0:
        return STATUS_PLACEHOLDER

    cache_input_tokens = 0
    for message in messages:
        if message.role != "assistant" or not isinstance(message.usage, dict):
            continue
        if message.usage.get("estimated") is True:
            continue
        cache_data = _cache_data_from_usage(message.usage)
        if cache_data is None:
            continue
        cache_input_tokens += cache_data[0]

    cache_data = (
        cache_input_tokens,
        _coerce_non_negative_int(totals.get("cache_read_tokens")) or 0,
        _coerce_non_negative_int(totals.get("cache_write_tokens")) or 0,
    )
    return f"{_format_cache_data(cache_data)}, turns {cache_turns}"


def _cache_data_from_usage(usage: dict[str, Any]) -> tuple[int, int, int] | None:
    if "cache_read_tokens" not in usage and "cache_write_tokens" not in usage:
        return None
    input_tokens = _coerce_non_negative_int(usage.get("input_tokens"))
    if input_tokens is None:
        return None
    return (
        input_tokens,
        _coerce_non_negative_int(usage.get("cache_read_tokens")) or 0,
        _coerce_non_negative_int(usage.get("cache_write_tokens")) or 0,
    )


def _format_cache_data(cache_data: tuple[int, int, int]) -> str:
    input_tokens, cache_read_tokens, cache_write_tokens = cache_data
    return (
        f"read {cache_read_tokens} / {input_tokens} "
        f"({_cache_hit_rate_text(cache_read_tokens, input_tokens)} hit), "
        f"write {cache_write_tokens}"
    )


def _cache_hit_rate_text(cache_read_tokens: int, input_tokens: int) -> str:
    if input_tokens <= 0:
        return STATUS_PLACEHOLDER
    hit_rate = cache_read_tokens / input_tokens * _CACHE_PERCENT_SCALE
    return f"{hit_rate:.{_CACHE_HIT_RATE_DECIMALS}f}%"


def _session_started_text(messages: list[ChatMessage], now_utc: datetime) -> str:
    if not messages:
        return STATUS_PLACEHOLDER

    parsed_timestamp = _parse_utc_timestamp(messages[0].timestamp)
    if parsed_timestamp is None:
        return STATUS_PLACEHOLDER

    local_started = parsed_timestamp.astimezone()
    age_text = _format_duration(now_utc - parsed_timestamp)
    return f"{local_started.strftime(_STATUS_TIME_FORMAT)} ({age_text} ago)"


def _app_uptime_text(started_at: datetime | None, now_utc: datetime) -> str:
    if started_at is None:
        return STATUS_PLACEHOLDER
    started_at_utc = _to_utc(started_at)
    return _format_duration(now_utc - started_at_utc)


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_utc_timestamp(value: str) -> datetime | None:
    normalized_value = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized_value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return int(stripped)
        except ValueError:
            return None
    return None


def _coerce_non_negative_int(value: object) -> int | None:
    coerced = _coerce_int(value)
    if coerced is None or coerced < 0:
        return None
    return coerced


def _format_duration(delta: timedelta) -> str:
    total_seconds = max(0, int(delta.total_seconds()))
    days, remainder = divmod(total_seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, seconds = divmod(remainder, 60)

    parts: list[str] = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0 or days > 0:
        parts.append(f"{hours}h")
    if minutes > 0 or hours > 0 or days > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)
