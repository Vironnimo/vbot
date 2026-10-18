"""Sub-Agent target admission, Session setup and operation projections."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, cast

from core.agents import is_builtin_agent
from core.projects import (
    AgentOverrides,
    AgentResolutionError,
    ModelConfigurationError,
    ResolutionAgentNotFoundError,
    ResolutionProjectNotFoundError,
    format_agent_address,
    parse_agent_address,
)
from core.runs import (
    Run,
    RunAdmission,
    RunKind,
)
from core.sessions import (
    AGENT_DEFAULT_PROJECT,
    SESSION_AUTO_TITLE_INITIALIZED_KEY,
    SESSION_AUTO_TITLE_KEY,
    SessionAddress,
    TemporarySessionBinding,
)
from core.settings import validate_thinking_effort
from core.subagents._constants import (
    DEFAULT_MAX_ACTIVE_SUBAGENTS,
    DEFAULT_MAX_ACTIVE_SUBAGENTS_TOTAL,
    DEFAULT_MAX_SUBAGENT_DEPTH,
    SUBAGENT_SESSION_MODEL_UNUSABLE_MESSAGE_TEMPLATE,
    SUBAGENT_SESSION_SETTINGS_UNREADABLE_MESSAGE_TEMPLATE,
    SUBAGENT_SESSION_TITLE_MAX_CHARACTERS,
    SUBAGENT_TARGET_UNAVAILABLE_MESSAGE_TEMPLATE,
)
from core.subagents._interpretation import (
    optional_text,
)
from core.subagents.forwarding import (
    is_working,
)
from core.subagents.links import (
    SubAgentLink,
    _link_metadata,
    read_link,
)
from core.tools.availability import subagent_allowed_agents
from core.tools.tools import (
    JsonObject,
    ToolContext,
    tool_failure,
)
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.runtime.interfaces import RuntimeServices
    from core.sessions.session import ChatSession


_LOGGER = get_logger("subagents")


def _caller(context: ToolContext) -> SessionAddress:
    return SessionAddress(
        project_id=context.project_id, agent_id=context.agent_id, session_id=context.session_id
    )


def _admission(
    context: ToolContext,
    subagent_id: str,
    working_project_id: str | None,
    *,
    expected_session_generation_id: str | None = None,
) -> RunAdmission:
    return RunAdmission(
        working_project_id=working_project_id,
        run_kind=RunKind.SUBAGENT,
        work_id=subagent_id,
        owner=context.execution_owner,
        contributes_to_agent_activity=context.execution_owner is None,
        expected_session_generation_id=expected_session_generation_id,
    )


def _public_identity(subagent_id: str, address: SessionAddress) -> JsonObject:
    """Identify a Sub-Agent for the calling Agent; ``agent_id`` is what the Tool accepts."""
    data: JsonObject = {
        "id": subagent_id,
        "agent_id": format_agent_address(address.agent_id, address.project_id),
        "session_id": address.session_id,
    }
    if address.project_id is not None:
        data["project_id"] = address.project_id
    return data


def _event_data(
    subagent_id: str,
    address: SessionAddress,
    *,
    status: str,
    run_id: str | None = None,
    activity_file: str | None = None,
) -> JsonObject:
    """Describe a Sub-Agent for WebUI events, with bare ids."""
    data: JsonObject = {
        "id": subagent_id,
        "agent_id": address.agent_id,
        "session_id": address.session_id,
        "status": status,
        "delivery": "automatic",
        "activity_file": activity_file,
    }
    if address.project_id is not None:
        data["project_id"] = address.project_id
    if run_id is not None:
        data["run_id"] = run_id
    return data


async def _emit(context: ToolContext, event: str, data: JsonObject) -> None:
    await context.emit(
        event,
        {
            "tool_call": {
                "id": context.tool_call_id,
                "index": context.tool_call_index,
                "name": context.tool_name,
            },
            "data": data,
        },
    )


async def _run_end(run: Run) -> None:
    try:
        await run.wait()
    except Exception:
        return


def _latest_tool_name(run: Run) -> str | None:
    for event in reversed(run.events):
        if event.type != "tool_call_started":
            continue
        tool_call = event.payload.get("tool_call")
        if isinstance(tool_call, dict):
            name = tool_call.get("name")
            if isinstance(name, str) and name:
                return name
    return None


def _session_title(description: str) -> str:
    return " ".join(description.split())[:SUBAGENT_SESSION_TITLE_MAX_CHARACTERS]


def _open_subagent_session(
    runtime: RuntimeServices,
    agent_id: str,
    project_id: str | None,
    title: str,
    subagent_id: str,
    context: ToolContext,
    overrides: dict[str, Any],
) -> ChatSession:
    """Create the Sub-Agent Session, title it, link it to its Parent and store *overrides*.

    Blocking. One creation transaction keeps title, Parent link and settings
    together even when storage fails. The Session works in
    :func:`_child_working_project`.
    """
    sessions = runtime.chat_sessions
    metadata = AgentOverrides.from_stored(overrides).session_metadata()
    metadata.update(
        _link_metadata(
            subagent_id=subagent_id,
            parent=_caller(context),
            run_id=context.run_id,
            tool_call_id=context.tool_call_id,
            tool_call_index=context.tool_call_index,
        )
    )
    metadata[SESSION_AUTO_TITLE_KEY] = title
    metadata[SESSION_AUTO_TITLE_INITIALIZED_KEY] = True
    return sessions.create(
        agent_id,
        project_id=project_id,
        metadata=metadata,
        working_project_id=(
            AGENT_DEFAULT_PROJECT
            if project_id is not None
            else _child_working_project(context, project_id)
        ),
    )


async def _remove_unstarted_session(
    runtime: RuntimeServices, address: SessionAddress, generation_id: str
) -> None:
    """Discard only the unused generation whose first Run could not start."""
    sessions = runtime.chat_sessions
    try:
        async with runtime.chat_run_manager.session_admission_guard(address):
            await sessions.run_async(sessions.discard_unused, address, generation_id)
    except Exception:
        _LOGGER.warning(
            "Could not remove a Sub-Agent Session whose Run did not start (session=%s)",
            address.session_id,
            exc_info=True,
        )


def _child_working_project(context: ToolContext, target_project_id: str | None) -> str | None:
    """Return the Project a new Sub-Agent Session works in.

    A Team target works in its Team's Project. An Identity target, including a
    copy of the Parent itself, works where the Parent's Run works, so delegated
    work stays in the Parent's working Project (or its Workspace).
    """
    return target_project_id if target_project_id is not None else context.working_project_id


def _load_subagent_settings(runtime: RuntimeServices) -> dict[str, int]:
    settings = runtime.storage.load_subagent_settings()
    return {
        "max_subagent_depth": _positive_int(
            settings.get("max_subagent_depth"), DEFAULT_MAX_SUBAGENT_DEPTH
        ),
        "max_active_subagents": _positive_int(
            settings.get("max_active_subagents"), DEFAULT_MAX_ACTIVE_SUBAGENTS
        ),
        "max_active_subagents_total": _positive_int(
            settings.get("max_active_subagents_total"), DEFAULT_MAX_ACTIVE_SUBAGENTS_TOTAL
        ),
    }


def _counts_as_working(runtime: RuntimeServices, link: SubAgentLink) -> bool:
    """Whether *link* takes a place under the limits: working, whatever its answer destination."""
    return is_working(runtime, link.session)


def _working_subagent_count(runtime: RuntimeServices, busy: Iterable[SessionAddress]) -> int:
    """Count the Sub-Agent Sessions among *busy*."""
    sessions = runtime.chat_sessions
    links = (read_link(sessions, address) for address in busy)
    return sum(1 for link in links if link is not None)


def _positive_int(value: Any, default: int) -> int:
    if isinstance(value, int) and value > 0:
        return value
    return default


def _parse_session_overrides(arguments: JsonObject) -> dict[str, Any]:
    """Parse the Sub-Agent Session's Agent overrides the call sets.

    An omitted field keeps the Session's current value: the Agent's own for a
    new Sub-Agent, the stored override for an existing one. An empty
    ``thinking_effort`` string counts as omitted.
    """
    model = optional_text(arguments, "model")
    thinking_effort: str | None = None
    raw = arguments.get("thinking_effort")
    if isinstance(raw, str) and raw:
        thinking_effort = cast(
            str, validate_thinking_effort(raw, label="thinking_effort", allow_none=False)
        )
    overrides: dict[str, Any] = {}
    if model is not None:
        overrides["model"] = model
    if thinking_effort is not None:
        overrides["thinking_effort"] = thinking_effort
    return overrides


async def _validate_target_agent(
    runtime: RuntimeServices,
    target_agent_id: str,
    project_id: str | None,
    *,
    model: str | None = None,
    temporary_parent_binding: TemporarySessionBinding | None = None,
    caller: ToolContext | None = None,
    overrides: dict[str, Any] | None = None,
) -> JsonObject | None:
    """Validate that the target resolves under its addressed Project and can run.

    Only a missing Agent (unknown, off-Team or the built-in Librarian) or
    Project reports ``agent_not_found`` / ``project_not_found``; a target that
    cannot run reports ``agent_unavailable`` with the resolver's reason, and a
    requested *model* that cannot run reports ``invalid_arguments``.
    """
    try:
        own_agent = (
            caller is not None
            and caller.agent_id == target_agent_id
            and caller.project_id == project_id
        )
        if temporary_parent_binding is not None:
            target = await runtime.agent_resolver.resolve_temporary_agent_async(
                temporary_parent_binding.address,
                generation_id=temporary_parent_binding.generation_id,
                session=temporary_parent_binding.address,
            )
        elif own_agent:
            assert caller is not None
            if caller.agent_model:
                inherited = AgentOverrides(
                    model=caller.agent_model,
                    thinking_effort=caller.agent_thinking_effort or None,
                    thinking_effort_default=not caller.agent_thinking_effort,
                )
            else:
                inherited = await runtime.agent_resolver.session_overrides_async(_caller(caller))
            source = {**inherited.as_dict(), **(overrides or {})}
            if overrides and "thinking_effort" in overrides:
                source.pop("thinking_effort_default", None)
            target = await runtime.agent_resolver.resolve_agent_async(
                project_id,
                target_agent_id,
                new_session_overrides=AgentOverrides.from_stored(source),
            )
        else:
            if (
                model is None
                and project_id is not None
                and caller is not None
                and overrides is not None
            ):
                caller_model = caller.agent_model
                if not caller_model:
                    parent_address = _caller(caller)
                    binding = await runtime.chat_sessions.run_async(
                        runtime.chat_sessions.temporary_binding, parent_address
                    )
                    if binding is not None:
                        parent = await runtime.agent_resolver.resolve_temporary_agent_async(
                            parent_address,
                            generation_id=binding.generation_id,
                            session=parent_address,
                        )
                    else:
                        parent = await runtime.agent_resolver.resolve_agent_async(
                            caller.project_id, caller.agent_id, session_id=caller.session_id
                        )
                    caller_model = parent.model
                target = await runtime.agent_resolver.resolve_delegated_agent_async(
                    project_id, target_agent_id, caller_model=caller_model
                )
                if getattr(target, "model_inherit", False):
                    overrides["model"] = caller_model
            else:
                target = await runtime.agent_resolver.resolve_agent_async(
                    project_id,
                    target_agent_id,
                    new_session_overrides=AgentOverrides.from_stored(overrides),
                )
        if is_builtin_agent(target):
            raise ResolutionAgentNotFoundError(f"Agent not found: {target_agent_id}")
        if model is not None:
            await runtime.agent_resolver.require_model_configured_async(model)
        if overrides is not None:
            if own_agent and caller is not None and caller.agent_model:
                overrides.setdefault("model", caller.agent_model)
                if "thinking_effort" not in overrides:
                    if caller.agent_thinking_effort:
                        overrides["thinking_effort"] = caller.agent_thinking_effort
                    else:
                        overrides["thinking_effort_default"] = True
            overrides.update(AgentOverrides.from_stored(overrides).capture(target).as_dict())
    except ResolutionProjectNotFoundError as error:
        return tool_failure("project_not_found", str(error))
    except ResolutionAgentNotFoundError as error:
        return tool_failure("agent_not_found", str(error))
    except AgentResolutionError as error:
        return tool_failure(
            "agent_unavailable",
            SUBAGENT_TARGET_UNAVAILABLE_MESSAGE_TEMPLATE.format(
                target=format_agent_address(target_agent_id, project_id), reason=error
            ),
            retryable=False,
        )
    except ModelConfigurationError as error:
        return tool_failure("invalid_arguments", str(error))
    return None


async def _validate_continued_session(
    runtime: RuntimeServices, link: SubAgentLink, *, model: str | None
) -> JsonObject | None:
    """Refuse a message to a Sub-Agent whose stored Agent settings stop its next Run."""
    resolver = runtime.agent_resolver
    try:
        if model is not None:
            await resolver.require_model_configured_async(model)
        stored = await resolver.session_overrides_async(link.session)
    except ModelConfigurationError as error:
        return tool_failure("invalid_arguments", str(error))
    except ValueError as error:
        return tool_failure(
            "invalid_arguments",
            SUBAGENT_SESSION_SETTINGS_UNREADABLE_MESSAGE_TEMPLATE.format(id=link.id, reason=error),
        )
    if model is not None or stored.model is None:
        return None
    try:
        await resolver.require_model_configured_async(stored.model)
    except ModelConfigurationError as error:
        return tool_failure(
            "invalid_arguments",
            SUBAGENT_SESSION_MODEL_UNUSABLE_MESSAGE_TEMPLATE.format(id=link.id, reason=error),
        )
    return None


def _resolve_target_address(address: str, caller_project_id: str | None) -> tuple[str, str | None]:
    """Resolve the Tool's address; a bare address inherits the caller's Project scope."""
    agent_id, addressed_project_id = parse_agent_address(address)
    return agent_id, addressed_project_id or caller_project_id


def _target_binding(
    context: ToolContext, target_agent_id: str, target_project_id: str | None
) -> str | int | None:
    """Return an explicit Identity grant's captured incarnation, if it has one."""
    if (
        context.project_id is not None
        or target_project_id is not None
        or target_agent_id == context.agent_id
        or "*" in subagent_allowed_agents(context.tool_settings)
    ):
        return None
    return context.delegation_bindings.get(target_agent_id)


def _target_is_allowed(
    context: ToolContext, target_agent_id: str, target_project_id: str | None
) -> bool:
    """Check the caller's Tool settings and enforce the Project boundary independently."""
    if context.project_id is not None and target_project_id != context.project_id:
        return False
    if target_agent_id == context.agent_id and target_project_id == context.project_id:
        return True
    allowed = subagent_allowed_agents(context.tool_settings)
    if "*" in allowed:
        return True
    address = (
        target_agent_id
        if context.project_id is not None
        else format_agent_address(target_agent_id, target_project_id)
    )
    return address in allowed
