"""Built-in status tool that reports current or targeted agent/session/runtime status."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.chat.errors import ChatSessionError
from core.chat.status_report import (
    ContextWindowResolver,
    ReasoningRenderDescriber,
    build_status_reply,
    resolve_reported_thinking_effort,
    resolve_status_activity,
    resolve_status_model_details,
    resolve_status_project_label,
    resolve_status_sampling,
    status_session_facts_from_snapshot,
)
from core.models.models import ModelRegistry
from core.projects import (
    AgentResolutionError,
    AgentResolver,
    ProjectStore,
    ResolutionAgentNotFoundError,
    ResolutionProjectNotFoundError,
    format_agent_address,
)
from core.providers.providers import ProviderRegistry
from core.runs import ChatRunManager
from core.sessions import SESSION_WORKING_PROJECT_META_KEY, ChatSessionManager, SessionAddress
from core.tools._background_tasks_arguments import (
    BACKGROUND_TASKS_TOOL_NAME,
    COMMAND_ID_PREFIX,
    SUBAGENT_ID_PREFIX,
)
from core.tools.arguments import optional_string
from core.tools.call_syntax import (
    PLACEHOLDER_WORDS,
    SpellingAliases,
    is_placeholder,
    normalize_call_arguments,
    spelling,
)
from core.tools.contracts import ToolContractError, compile_tool_contract
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolRegistry,
    offload_tool_handler,
    tool_failure,
    tool_success,
)
from core.utils.logging import get_logger

_LOGGER = get_logger("tools.status")

STATUS_TOOL_NAME = "status"
STATUS_TOOL_DESCRIPTION = (
    "Report a chat Session's Agent, model, context and cache usage, runtime, and activity. "
    "Omit both parameters for your current Session."
)
_STATUS_SESSION_ID_PARAMETER: JsonObject = {
    "type": "string",
    "description": "Session to inspect. Omit for your current Session.",
}
_STATUS_AGENT_ID_PARAMETER: JsonObject = {
    "type": "string",
    "description": (
        "Agent that owns session_id. Omit for yourself; another Agent's Session needs both."
    ),
}

STATUS_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "session_id": _STATUS_SESSION_ID_PARAMETER,
        "agent_id": _STATUS_AGENT_ID_PARAMETER,
    },
    "required": [],
}

# ``target`` is ``agent`` or ``agent@project``; ``reason`` is the resolver's explanation.
_AGENT_UNAVAILABLE_MESSAGE_TEMPLATE = (
    "Agent {target} cannot run, so its status cannot be reported: {reason}. Repeating "
    "this call fails the same way until that is fixed. Tell the user that {target} "
    "cannot run and why."
)
_PROJECT_NOT_FOUND_MESSAGE_TEMPLATE = (
    "project does not exist: {project_id}. Status reports only on Sessions of the "
    "current Project, so tell the user that this Project is missing."
)
_AGENT_NOT_FOUND_MESSAGE_TEMPLATE = (
    "Agent not found: {agent_id}. Omit agent_id and session_id to check your current Session."
)
_AGENT_WITHOUT_SESSION_MESSAGE_TEMPLATE = (
    "status needs session_id to inspect a Session of Agent {agent_id}; nothing was checked. "
    'Call {{"agent_id": "{agent_id}", "session_id": "<session id>"}}, for example with the '
    "session_id from a subagent result, or omit agent_id to check your current Session."
)
_SESSION_NOT_FOUND_MESSAGE_TEMPLATE = "No Session {session_id} exists for Agent {agent_id}."
_CURRENT_SESSION_HINT = " Omit session_id to check your current Session."
_SESSION_OWNER_HINT = (
    " If that Session belongs to another Agent, such as a Sub-Agent, also pass that "
    "Agent's agent_id."
)
# Sub-Agent and background command ids come from ``subagent`` and shell results;
# this Tool reports Sessions. The sentence naming ``background_tasks`` is left out
# when the Agent cannot call it.
_TASK_ID_MESSAGE_TEMPLATE = (
    "status was not run: {task_id} is a {kind} id.{see_task} status reports a chat "
    "Session and takes session_id, with agent_id for another Agent's Session."
)
_SEE_TASK_TEMPLATE = (
    ' To see that {noun}, call background_tasks {{"action": "status", "id": "{task_id}"}}.'
)
_ID_MESSAGE_TEMPLATE = (
    'status was not run: it has no "id" parameter, so {value} is ambiguous. Pass a Session '
    'as "session_id", with "agent_id" for another Agent\'s Session, or omit both to check '
    "your current Session."
)
_FIELD_ALIASES = SpellingAliases(
    {"session_id": ("session", "session_key"), "agent_id": ("agent", "agent_name")}
)
# Words that mean "my current Session"; generated Session ids never take these forms.
_CURRENT_SESSION_WORDS = PLACEHOLDER_WORDS | {"current", "this", "active", "mine"}
_ID_KEYS = frozenset({"id", "workid", "subagentid"})
_SESSION_ID_PREFIX = "ses_"
# Set by the normalizer only: a Sub-Agent or background command id the call sent,
# which the handler refuses, naming ``background_tasks`` only when the Agent can
# call it.
_TASK_ID_SENT = "task_id_sent"
# A task id in agent_id, which otherwise names an Agent: only the generated form,
# so that an Agent named such as sub_reviewer stays an Agent.
_GENERATED_TASK_ID = re.compile(r"(?:sub|cmd)_[0-9a-z]{12,16}")


_STATUS_RUNTIME_CONTRACT = compile_tool_contract(
    name=STATUS_TOOL_NAME,
    input_schema={
        **STATUS_TOOL_PARAMETERS,
        "properties": {**STATUS_TOOL_PARAMETERS["properties"], "action": {"enum": ["current"]}},
    },
    require_closed_input=False,
)


def _normalize_status_arguments(arguments: Any) -> Any:
    repaired = normalize_call_arguments(
        _STATUS_RUNTIME_CONTRACT, arguments, enum_fields=("action",), field_aliases=_FIELD_ALIASES
    )
    if not isinstance(repaired, dict):
        return repaired
    if repaired.pop("action", "current") != "current":
        raise ValueError("status reads Session status; action must be current or omitted.")
    _read_id_field(repaired)
    session_id = repaired.get("session_id")
    if _TASK_ID_SENT not in repaired and _is_task_id(session_id):
        repaired[_TASK_ID_SENT] = repaired.pop("session_id").strip()
    agent_id = repaired.get("agent_id")
    if (
        _TASK_ID_SENT not in repaired
        and isinstance(agent_id, str)
        and _GENERATED_TASK_ID.fullmatch(agent_id.strip())
    ):
        repaired[_TASK_ID_SENT] = repaired.pop("agent_id").strip()
    if is_placeholder(repaired.get("session_id"), _CURRENT_SESSION_WORDS):
        repaired.pop("session_id", None)
    if is_placeholder(repaired.get("agent_id")):
        repaired.pop("agent_id", None)
    return repaired


def _read_id_field(arguments: dict[str, Any]) -> None:
    """Read an ``id`` field by what its value is: a Session, a background task, or unknown."""
    given = {key: arguments.pop(key) for key in list(arguments) if spelling(key) in _ID_KEYS}
    for key, value in given.items():
        if is_placeholder(value):
            continue
        text = value.strip() if isinstance(value, str) else value
        if _is_task_id(text):
            arguments[_TASK_ID_SENT] = text
            return
        if spelling(key) == "id" and isinstance(text, str) and text.startswith(_SESSION_ID_PREFIX):
            session_id = arguments.get("session_id")
            if not is_placeholder(session_id, _CURRENT_SESSION_WORDS) and session_id != text:
                raise ToolContractError.conflicting_values(
                    "session_id",
                    ("session_id", session_id),
                    (key, value),
                    tool_name=STATUS_TOOL_NAME,
                )
            arguments["session_id"] = text
            continue
        raise ToolContractError(_ID_MESSAGE_TEMPLATE.format(value=json.dumps(value)))


def _is_task_id(value: Any) -> bool:
    """Whether *value* is the id of a Sub-Agent or a background command."""
    return isinstance(value, str) and value.strip().startswith(
        (SUBAGENT_ID_PREFIX, COMMAND_ID_PREFIX)
    )


def _task_id_message(context: ToolContext, task_id: str) -> str:
    if task_id.startswith(SUBAGENT_ID_PREFIX):
        kind = noun = "Sub-Agent"
    else:
        kind, noun = "background command", "command"
    see_task = (
        _SEE_TASK_TEMPLATE.format(noun=noun, task_id=task_id)
        if context.can_call(BACKGROUND_TASKS_TOOL_NAME)
        else ""
    )
    return _TASK_ID_MESSAGE_TEMPLATE.format(task_id=task_id, kind=kind, see_task=see_task)


def _configured_zone(timezone_name_loader: Callable[[], str] | None) -> tzinfo | None:
    """The configured time zone for the reply's times; UTC when it cannot be loaded.

    The reply names the zone of every time it shows, so the fallback stays visible.
    """
    if timezone_name_loader is None:
        return None
    try:
        return ZoneInfo(timezone_name_loader())
    except ZoneInfoNotFoundError, ValueError, OSError:
        _LOGGER.warning("status could not load the configured timezone; using UTC", exc_info=True)
        return UTC


def make_status_handler(
    agent_resolver: AgentResolver,
    sessions: ChatSessionManager,
    models: ModelRegistry,
    chat_runs: ChatRunManager,
    started_at: datetime | None,
    providers: ProviderRegistry | None = None,
    projects: ProjectStore | None = None,
    context_window_resolver: ContextWindowResolver | None = None,
    reasoning_render_describer: ReasoningRenderDescriber | None = None,
    timezone_name_loader: Callable[[], str] | None = None,
):
    """Create a status tool handler bound to runtime services."""

    def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        task_id = arguments.get(_TASK_ID_SENT)
        if isinstance(task_id, str):
            return tool_failure("invalid_arguments", _task_id_message(context, task_id))
        try:
            requested_agent_id = optional_string(arguments.get("agent_id"), field_name="agent_id")
            requested_session_id = optional_string(
                arguments.get("session_id"), field_name="session_id"
            )
        except ValueError as error:
            return tool_failure("invalid_arguments", str(error))
        if requested_agent_id not in (None, context.agent_id) and requested_session_id is None:
            return tool_failure(
                "invalid_arguments",
                _AGENT_WITHOUT_SESSION_MESSAGE_TEMPLATE.format(agent_id=requested_agent_id),
            )

        agent_id = requested_agent_id or context.agent_id
        session_id = requested_session_id or context.session_id

        # Resolve through the one seam so ``/status`` shows the agent profile the
        # run actually uses: a project run (``context.project_id`` set) reports the
        # resolved config-agent profile, an identity run resolves the store agent
        # exactly as before. Only a missing Agent or Project is "not found"; an
        # existing target that cannot run reports why.
        try:
            agent = agent_resolver.resolve_agent(
                context.project_id, agent_id, session_id=session_id
            )
        except ResolutionProjectNotFoundError:
            return tool_failure(
                "project_not_found",
                _PROJECT_NOT_FOUND_MESSAGE_TEMPLATE.format(project_id=context.project_id),
                retryable=False,
            )
        except ResolutionAgentNotFoundError:
            return tool_failure(
                "agent_not_found", _AGENT_NOT_FOUND_MESSAGE_TEMPLATE.format(agent_id=agent_id)
            )
        except AgentResolutionError as error:
            return tool_failure(
                "agent_unavailable",
                _AGENT_UNAVAILABLE_MESSAGE_TEMPLATE.format(
                    target=format_agent_address(agent_id, context.project_id), reason=error
                ),
                retryable=False,
            )

        address = SessionAddress(
            project_id=context.project_id, agent_id=agent_id, session_id=session_id
        )
        try:
            snapshot = sessions.get(address).status_snapshot()
            working_project_id = sessions.metadata_value(address, SESSION_WORKING_PROJECT_META_KEY)
        except ChatSessionError:
            message = _SESSION_NOT_FOUND_MESSAGE_TEMPLATE.format(
                session_id=session_id, agent_id=agent_id
            )
            if requested_session_id is not None:
                message += _CURRENT_SESSION_HINT
                if requested_agent_id is None:
                    message += _SESSION_OWNER_HINT
            return tool_failure("session_not_found", message)

        activity = resolve_status_activity(chat_runs, agent_id, session_id, context.project_id)
        model_details = resolve_status_model_details(
            agent, models, providers, context_window_resolver=context_window_resolver
        )

        try:
            text = build_status_reply(
                agent,
                status_session_facts_from_snapshot(snapshot),
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
                project_label=resolve_status_project_label(projects, working_project_id),
                sampling_status=resolve_status_sampling(agent, model_details),
                timezone=_configured_zone(timezone_name_loader),
            )
        except Exception:
            _LOGGER.error("Failed to build status tool reply", exc_info=True)
            raise

        return tool_success(
            {
                "text": text,
                "agent_id": agent_id,
                "session_id": session_id,
            }
        )

    return handler


def register_status_tool(
    registry: ToolRegistry,
    agent_resolver: AgentResolver,
    sessions: ChatSessionManager,
    models: ModelRegistry,
    chat_runs: ChatRunManager,
    started_at: datetime | None,
    providers: ProviderRegistry | None = None,
    projects: ProjectStore | None = None,
    context_window_resolver: ContextWindowResolver | None = None,
    reasoning_render_describer: ReasoningRenderDescriber | None = None,
    timezone_name_loader: Callable[[], str] | None = None,
) -> None:
    """Register the status tool with a vBot tool registry."""
    registry.register(
        STATUS_TOOL_NAME,
        STATUS_TOOL_DESCRIPTION,
        STATUS_TOOL_PARAMETERS,
        offload_tool_handler(
            make_status_handler(
                agent_resolver,
                sessions,
                models,
                chat_runs,
                started_at,
                providers,
                projects,
                context_window_resolver,
                reasoning_render_describer,
                timezone_name_loader,
            )
        ),
        summary="Check a Session's model, context and usage.",
        open_input_schema=True,
        argument_normalizer=_normalize_status_arguments,
        unadvertised_parameters={_TASK_ID_SENT: {"type": "string"}},
        result_schema={"type": "object", "required": ["text", "agent_id", "session_id"]},
        display=ToolDisplay(),
        parallel_safe=True,
    )
