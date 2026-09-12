"""Private Built-in Command model and status operations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from core.chat.commands import (
    _COMMAND_WORKERS,
    _LOGGER,
    CommandExecutionContext,
    CommandFeedback,
    CommandOutcome,
    _command_session_io,
    _has_exception_name,
    _require_dependency,
)
from core.chat.messages import ChatMessage
from core.chat.status_report import (
    STATUS_PLACEHOLDER,
    ReasoningRenderDescriber,
    StatusSessionFacts,
    build_status_reply,
    resolve_reported_thinking_effort,
    resolve_status_activity,
    resolve_status_model_details,
    resolve_status_project_label,
    resolve_status_temperature,
)
from core.projects import format_agent_address
from core.runs import ChatRunManager
from core.sessions import SessionAddress
from core.settings.settings import effective_timezone_name

if TYPE_CHECKING:
    from core.agents import AgentStore
    from core.models.models import ModelRegistry
    from core.projects import AgentResolver, ProjectStore, RuntimeAgent
    from core.providers.providers import ProviderRegistry
    from core.sessions import ChatSessionManager


_MODEL_ORIGIN_NOT_CONFIGURED = "not configured"

_IDENTITY_MODEL_ORIGINS: dict[str | None, str] = {
    "agent": "agent configuration",
    "global_default": "global default",
}

_PROJECT_MODEL_ORIGINS: dict[str | None, str] = {
    "override": "override (set via /model)",
    "agent": "agent file in repo",
    "project_default": "project default",
    "global_default": "global default",
}

MODEL_RESET_TOKEN = "reset"

_MISSING = object()


def _stored_agent_model(agents: Any, agent_id: str) -> object:
    getter = getattr(agents, "get_raw", None) or getattr(agents, "get", None)
    if not callable(getter):
        return _MISSING
    return getattr(getter(agent_id), "model", _MISSING)


def _stored_project_override(projects: Any, project_id: str, agent_id: str, field: str) -> object:
    getter = getattr(projects, "get", None)
    if not callable(getter):
        return _MISSING
    project = getter(project_id)
    overrides = getattr(project, "overrides", {})
    if not isinstance(overrides, Mapping):
        return None
    agent_override = overrides.get(agent_id, {})
    if not isinstance(agent_override, Mapping):
        return None
    return agent_override.get(field)


async def _execute_model(
    context: CommandExecutionContext,
    argument: str | None,
    *,
    agent_resolver: AgentResolver | None,
    agents: AgentStore | None,
    projects: ProjectStore | None,
) -> CommandOutcome:
    if argument is None:
        return CommandOutcome(
            command="model",
            feedback=CommandFeedback(
                kind="detail",
                text=await _COMMAND_WORKERS.run(
                    partial(_build_model_summary, agent_resolver=agent_resolver),
                    context.agent_id,
                    context.project_id,
                ),
            ),
        )
    raw = argument.strip()
    is_reset = raw.lower() == MODEL_RESET_TOKEN
    model = "" if is_reset else raw
    changed = await _COMMAND_WORKERS.run(
        partial(
            _apply_model_setting, agent_resolver=agent_resolver, agents=agents, projects=projects
        ),
        context.agent_id,
        context.project_id,
        model,
        is_reset,
    )
    if changed:
        _LOGGER.info(
            "Agent model configuration %s (agent=%s field=model)",
            "reset" if is_reset else "updated",
            format_agent_address(context.agent_id, context.project_id),
        )
    return CommandOutcome(
        command="model",
        feedback=CommandFeedback(
            kind="notice", text="Model reset." if is_reset else f"Model set to {model}."
        ),
        facts={"agent_id": context.agent_id, "model": model},
    )


def _apply_model_setting(
    agent_id: str,
    project_id: str | None,
    model: str,
    is_reset: bool,
    *,
    agent_resolver: AgentResolver | None,
    agents: AgentStore | None,
    projects: ProjectStore | None,
) -> bool:
    resolver = _require_dependency(agent_resolver, "AgentResolver")
    if not is_reset:
        resolver.require_model_configured(model)
    if project_id is None:
        agents = _require_dependency(agents, "AgentStore")
        previous_model = _stored_agent_model(agents, agent_id)
        agents.update(agent_id, model=model)
        return previous_model is _MISSING or previous_model != model
    if is_reset:
        projects = _require_dependency(projects, "ProjectStore")
        previous_model = _stored_project_override(projects, project_id, agent_id, "model")
        projects.clear_override(project_id, agent_id, "model")
        return previous_model is _MISSING or previous_model is not None
    projects = _require_dependency(projects, "ProjectStore")
    previous_model = _stored_project_override(projects, project_id, agent_id, "model")
    projects.set_override(project_id, agent_id, "model", model)
    return previous_model is _MISSING or previous_model != model


async def _execute_status(
    context: CommandExecutionContext,
    argument: str | None,
    *,
    agent_resolver: AgentResolver | None,
    chat_runs: ChatRunManager,
    local_context_windows_loader: Callable[[], Mapping[str, Any]] | None,
    models: ModelRegistry | None,
    projects: ProjectStore | None,
    providers: ProviderRegistry | None,
    reasoning_render_describer: ReasoningRenderDescriber | None,
    sessions: ChatSessionManager | None,
    started_at: datetime | None,
    storage: Any | None,
) -> CommandOutcome:
    agent: RuntimeAgent | None = None
    status_session: list[ChatMessage] | StatusSessionFacts = []
    try:
        if agent_resolver is not None:
            agent = await _COMMAND_WORKERS.run(
                agent_resolver.resolve_agent,
                context.project_id,
                context.agent_id,
            )
    except Exception as error:
        log = (
            _LOGGER.warning if _has_exception_name(error, "AgentResolutionError") else _LOGGER.error
        )
        log(
            "Failed to resolve agent %r while building /status reply",
            context.agent_id,
            exc_info=True,
        )
    try:
        if sessions is not None:
            session = await _command_session_io(
                sessions,
                "get_async",
                "get",
                SessionAddress(
                    project_id=context.project_id,
                    agent_id=context.agent_id,
                    session_id=context.session_id,
                ),
            )
            snapshot = await _command_session_io(
                session,
                "status_snapshot_async",
                "status_snapshot",
            )
            status_session = StatusSessionFacts(
                first_message_at=snapshot.first_message_at,
                user_message_count=snapshot.user_message_count,
                latest_assistant_usage=snapshot.latest_assistant_usage,
                session_usage=snapshot.session_usage,
                cache_input_tokens=snapshot.cache_input_tokens,
            )
    except Exception as error:
        log = _LOGGER.warning if _has_exception_name(error, "ChatSessionError") else _LOGGER.error
        log(
            "Failed to load session %r for agent %r while building /status reply",
            context.session_id,
            context.agent_id,
            exc_info=True,
        )
    model_details = resolve_status_model_details(
        agent,
        models,
        providers,
        local_context_windows=_load_local_context_windows(
            local_context_windows_loader=local_context_windows_loader
        ),
    )
    activity = resolve_status_activity(
        chat_runs,
        context.agent_id,
        context.session_id,
        context.project_id,
    )
    text = build_status_reply(
        agent,
        status_session,
        model_details.context_window,
        started_at,
        model_details.display_name,
        activity,
        actual_thinking_effort=resolve_reported_thinking_effort(
            agent=agent,
            models=models,
            model_details=model_details,
            describe_render=reasoning_render_describer,
        ),
        project_label=resolve_status_project_label(projects, context.project_id),
        temperature_status=resolve_status_temperature(
            agent.temperature if agent is not None else None,
            model_details,
        ),
        timezone=_status_timezone(storage=storage),
    )
    return CommandOutcome(
        command="status",
        feedback=CommandFeedback(kind="detail", text=text),
    )


def _build_model_summary(
    agent_id: str, project_id: str | None, *, agent_resolver: AgentResolver | None
) -> str:
    """Describe the session's current model and where it resolves from.

    Reads the resolver's provenance seam once (``effective_config``): the model
    value is what the next run would use (already post-override), and its source names
    the winning tier. None-guarded like ``/status`` so a minimally constructed
    dispatcher degrades to a placeholder instead of crashing; a resolver error is
    logged and degrades to placeholder + "not configured".
    """
    model = STATUS_PLACEHOLDER
    source: str | None = None
    if agent_resolver is not None:
        try:
            effective = agent_resolver.effective_config(project_id, agent_id)
            model_field = effective.get("model", {})
            value = model_field.get("value")
            model = (value or "").strip() or STATUS_PLACEHOLDER
            source = model_field.get("source")
        except Exception as error:
            log = (
                _LOGGER.warning
                if _has_exception_name(error, "AgentResolutionError")
                else _LOGGER.error
            )
            log(
                "Failed to resolve agent %r while building /model reply",
                agent_id,
                exc_info=True,
            )
    return f"Current model: {model}\nSource: {_model_origin(project_id, source)}"


def _model_origin(project_id: str | None, source: str | None) -> str:
    """Return where the session's current model comes from, in plain English.

    Maps the provenance ``source`` tier (from ``effective_config``) onto the
    wire wording, keyed by session kind so identity and project sessions read
    differently. A ``None`` source (chain fully fell through) reports "not
    configured".
    """
    if project_id is None:
        return _IDENTITY_MODEL_ORIGINS.get(source, _MODEL_ORIGIN_NOT_CONFIGURED)
    return _PROJECT_MODEL_ORIGINS.get(source, _MODEL_ORIGIN_NOT_CONFIGURED)


def _load_local_context_windows(
    *, local_context_windows_loader: Callable[[], Mapping[str, Any]] | None
) -> Mapping[str, Any]:
    """Return the live local-model window map, empty when no loader is wired."""
    if local_context_windows_loader is None:
        return {}
    try:
        return local_context_windows_loader()
    except Exception:
        _LOGGER.warning("Failed to load local-model context windows", exc_info=True)
        return {}


def _status_timezone(*, storage: Any | None) -> ZoneInfo | None:
    if storage is None:
        return None
    try:
        return ZoneInfo(effective_timezone_name(storage.load_settings()))
    except (AttributeError, OSError, ValueError):
        _LOGGER.warning("Failed to load application timezone", exc_info=True)
        return None
