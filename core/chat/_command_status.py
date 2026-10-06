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
    NewSessionCommandContext,
    _command_session_io,
    _has_exception_name,
    _require_dependency,
)
from core.chat.messages import ChatMessage
from core.chat.status_report import (
    STATUS_PLACEHOLDER,
    ReasoningRenderDescriber,
    StatusActivity,
    StatusSessionFacts,
    WireProfileDescriber,
    build_status_reply,
    resolve_reported_thinking_effort,
    resolve_status_activity,
    resolve_status_model_details,
    resolve_status_project_label,
    resolve_status_sampling,
    resolve_status_wire_profile,
)
from core.projects import AgentOverrides, format_agent_address
from core.runs import ChatRunManager
from core.sessions import (
    AGENT_DEFAULT_PROJECT,
    SESSION_WORKING_PROJECT_META_KEY,
    SessionAddress,
)
from core.settings.settings import effective_timezone_name

if TYPE_CHECKING:
    from core.agents import AgentStore
    from core.models.models import ModelRegistry
    from core.projects import AgentResolver, ProjectStore, RuntimeAgent
    from core.providers.providers import ProviderRegistry
    from core.sessions import ChatSessionManager


_MODEL_ORIGIN_NOT_CONFIGURED = "not configured"

_IDENTITY_MODEL_ORIGINS: dict[str | None, str] = {
    "session": "this session",
    "agent": "agent configuration",
    "global_default": "global default",
}

_PROJECT_MODEL_ORIGINS: dict[str | None, str] = {
    "session": "this session",
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
        return await _model_summary_outcome(
            context.agent_id,
            context.project_id,
            agent_resolver=agent_resolver,
            session_id=context.session_id,
        )
    outcome = await _set_model(
        context.agent_id,
        context.project_id,
        argument,
        agent_resolver=agent_resolver,
        agents=agents,
        projects=projects,
    )
    # The Agent's Model is what this Session runs next, so a Session Model
    # override stops shadowing it.
    await _require_dependency(agent_resolver, "AgentResolver").update_session_overrides_async(
        SessionAddress(context.project_id, context.agent_id, context.session_id), {"model": None}
    )
    return outcome


async def _execute_model_without_session(
    context: NewSessionCommandContext,
    argument: str | None,
    *,
    agent_resolver: AgentResolver | None,
    agents: AgentStore | None,
    projects: ProjectStore | None,
) -> CommandOutcome:
    """``/model`` for a new Session: the Model its first Run would use, or the Agent's."""
    if argument is None:
        return await _model_summary_outcome(
            context.agent_id,
            context.project_id,
            agent_resolver=agent_resolver,
            new_session_overrides=context.agent_overrides,
        )
    return await _set_model(
        context.agent_id,
        context.project_id,
        argument,
        agent_resolver=agent_resolver,
        agents=agents,
        projects=projects,
    )


async def _model_summary_outcome(
    agent_id: str,
    project_id: str | None,
    *,
    agent_resolver: AgentResolver | None,
    session_id: str | None = None,
    new_session_overrides: AgentOverrides | None = None,
) -> CommandOutcome:
    return CommandOutcome(
        command="model",
        feedback=CommandFeedback(
            kind="detail",
            text=await _COMMAND_WORKERS.run(
                partial(
                    _build_model_summary,
                    agent_resolver=agent_resolver,
                    new_session_overrides=new_session_overrides,
                ),
                agent_id,
                project_id,
                session_id,
            ),
        ),
    )


async def _set_model(
    agent_id: str,
    project_id: str | None,
    argument: str,
    *,
    agent_resolver: AgentResolver | None,
    agents: AgentStore | None,
    projects: ProjectStore | None,
) -> CommandOutcome:
    raw = argument.strip()
    is_reset = raw.lower() == MODEL_RESET_TOKEN
    model = "" if is_reset else raw
    changed = await _COMMAND_WORKERS.run(
        partial(
            _apply_model_setting, agent_resolver=agent_resolver, agents=agents, projects=projects
        ),
        agent_id,
        project_id,
        model,
        is_reset,
    )
    if changed:
        _LOGGER.info(
            "Agent model configuration %s (agent=%s field=model)",
            "reset" if is_reset else "updated",
            format_agent_address(agent_id, project_id),
        )
    return CommandOutcome(
        command="model",
        feedback=CommandFeedback(
            kind="notice", text="Model reset." if is_reset else f"Model set to {model}."
        ),
        facts={"agent_id": agent_id, "model": model},
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
    wire_profile_describer: WireProfileDescriber | None,
) -> CommandOutcome:
    agent = await _status_agent(
        agent_resolver, context.agent_id, context.project_id, session_id=context.session_id
    )
    status_session: list[ChatMessage] | StatusSessionFacts = []
    # The Project the Session works in; a Project Session works in its own.
    working_project_id = context.project_id
    try:
        if sessions is not None:
            address = SessionAddress(
                project_id=context.project_id,
                agent_id=context.agent_id,
                session_id=context.session_id,
            )
            session = await _command_session_io(sessions, "get_async", "get", address)
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
            working_project_id = await _command_session_io(
                sessions,
                "metadata_value_async",
                "metadata_value",
                address,
                SESSION_WORKING_PROJECT_META_KEY,
            )
    except Exception as error:
        log = _LOGGER.warning if _has_exception_name(error, "ChatSessionError") else _LOGGER.error
        log(
            "Failed to load session %r for agent %r while building /status reply",
            context.session_id,
            context.agent_id,
            exc_info=True,
        )
    return await _status_outcome(
        agent,
        status_session,
        resolve_status_activity(
            chat_runs,
            context.agent_id,
            context.session_id,
            context.project_id,
        ),
        working_project_id,
        local_context_windows_loader=local_context_windows_loader,
        models=models,
        projects=projects,
        providers=providers,
        reasoning_render_describer=reasoning_render_describer,
        started_at=started_at,
        storage=storage,
        wire_profile_describer=wire_profile_describer,
    )


async def _execute_status_without_session(
    context: NewSessionCommandContext,
    argument: str | None,
    *,
    agent_resolver: AgentResolver | None,
    local_context_windows_loader: Callable[[], Mapping[str, Any]] | None,
    models: ModelRegistry | None,
    projects: ProjectStore | None,
    providers: ProviderRegistry | None,
    reasoning_render_describer: ReasoningRenderDescriber | None,
    started_at: datetime | None,
    storage: Any | None,
    wire_profile_describer: WireProfileDescriber | None,
) -> CommandOutcome:
    """``/status`` for a new Session: the Agent as its first Run would run, no History.

    The Project shown is the one the new Session would work in.
    """
    agent = await _status_agent(
        agent_resolver,
        context.agent_id,
        context.project_id,
        new_session_overrides=context.agent_overrides,
    )
    working_project_id: str | None
    if context.project_id is not None:
        working_project_id = context.project_id
    elif context.working_project_id is AGENT_DEFAULT_PROJECT:
        working_project_id = getattr(agent, "root_project_id", None)
    else:
        working_project_id = context.working_project_id
    return await _status_outcome(
        agent,
        [],
        StatusActivity(activity="idle", run_id=None, created_at=None, updated_at=None),
        working_project_id,
        local_context_windows_loader=local_context_windows_loader,
        models=models,
        projects=projects,
        providers=providers,
        reasoning_render_describer=reasoning_render_describer,
        started_at=started_at,
        storage=storage,
        wire_profile_describer=wire_profile_describer,
    )


async def _status_agent(
    agent_resolver: AgentResolver | None,
    agent_id: str,
    project_id: str | None,
    *,
    session_id: str | None = None,
    new_session_overrides: AgentOverrides | None = None,
) -> RuntimeAgent | None:
    """Resolve the Agent a ``/status`` reply describes; a failure degrades to ``None``."""
    if agent_resolver is None:
        return None
    try:
        if session_id is not None:
            return await agent_resolver.resolve_agent_async(
                project_id, agent_id, session_id=session_id
            )
        return await agent_resolver.resolve_agent_async(
            project_id, agent_id, new_session_overrides=new_session_overrides
        )
    except Exception as error:
        log = (
            _LOGGER.warning if _has_exception_name(error, "AgentResolutionError") else _LOGGER.error
        )
        log(
            "Failed to resolve agent %r while building /status reply",
            agent_id,
            exc_info=True,
        )
        return None


async def _status_outcome(
    agent: RuntimeAgent | None,
    status_session: list[ChatMessage] | StatusSessionFacts,
    activity: StatusActivity,
    working_project_id: str | None,
    *,
    local_context_windows_loader: Callable[[], Mapping[str, Any]] | None,
    models: ModelRegistry | None,
    projects: ProjectStore | None,
    providers: ProviderRegistry | None,
    reasoning_render_describer: ReasoningRenderDescriber | None,
    started_at: datetime | None,
    storage: Any | None,
    wire_profile_describer: WireProfileDescriber | None,
) -> CommandOutcome:
    model_details = resolve_status_model_details(
        agent,
        models,
        providers,
        local_context_windows=_load_local_context_windows(
            local_context_windows_loader=local_context_windows_loader
        ),
    )
    # Resolving the Connection reads credential state, so it runs off the loop.
    wire_profile = await _COMMAND_WORKERS.run(
        resolve_status_wire_profile, agent, wire_profile_describer
    )
    actual_thinking_effort = await _COMMAND_WORKERS.run(
        resolve_reported_thinking_effort,
        agent=agent,
        models=models,
        model_details=model_details,
        describe_render=reasoning_render_describer,
    )
    text = build_status_reply(
        agent,
        status_session,
        model_details.context_window,
        started_at,
        model_details.display_name,
        activity,
        actual_thinking_effort=actual_thinking_effort,
        project_label=resolve_status_project_label(projects, working_project_id),
        sampling_status=resolve_status_sampling(agent, model_details),
        timezone=_status_timezone(storage=storage),
        wire_profile=wire_profile,
    )
    return CommandOutcome(
        command="status",
        feedback=CommandFeedback(kind="detail", text=text),
    )


def _build_model_summary(
    agent_id: str,
    project_id: str | None,
    session_id: str | None,
    *,
    agent_resolver: AgentResolver | None,
    new_session_overrides: AgentOverrides | None = None,
) -> str:
    """Describe the session's current model and where it resolves from.

    Reads the resolver's provenance seam once (``effective_config``): the model
    value is what the next run would use (already post-override), and its source names
    the winning tier. Without ``session_id`` it describes a new Session, whose
    ``new_session_overrides`` Model counts as its own. None-guarded like
    ``/status`` so a minimally constructed dispatcher degrades to a placeholder
    instead of crashing; a resolver error is logged and degrades to placeholder +
    "not configured".
    """
    model = STATUS_PLACEHOLDER
    source: str | None = None
    if agent_resolver is not None:
        try:
            effective = agent_resolver.effective_config(project_id, agent_id, session_id=session_id)
            if new_session_overrides is not None and new_session_overrides.model is not None:
                effective["model"] = {"value": new_session_overrides.model, "source": "session"}
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
    except AttributeError, OSError, ValueError:
        _LOGGER.warning("Failed to load application timezone", exc_info=True)
        return None
