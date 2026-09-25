"""Built-in status tool that reports current or targeted agent/session/runtime status."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from core.chat.errors import ChatSessionError
from core.chat.status_report import (
    ReasoningRenderDescriber,
    StatusSessionFacts,
    build_status_reply,
    resolve_reported_thinking_effort,
    resolve_status_activity,
    resolve_status_model_details,
    resolve_status_project_label,
    resolve_status_temperature,
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
from core.sessions import ChatSessionManager, SessionAddress
from core.tools._argument_repair import normalize_call_arguments
from core.tools._call_vocabulary import PLACEHOLDER_WORDS, SpellingAliases, is_placeholder, spelling
from core.tools.arguments import optional_string
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
# Sub-Agent work ids come from ``subagent`` results; this Tool reports Sessions.
_WORK_ID_MESSAGE_TEMPLATE = (
    "status was not run: {work_id} is a Sub-Agent work id. For that work's progress, call "
    'subagent with {{"action": "status", "id": "{work_id}"}}. status reports a chat Session '
    "and takes session_id, with agent_id for another Agent's Session."
)
_ID_MESSAGE_TEMPLATE = (
    'status was not run: it has no "id" parameter, so {value} is ambiguous. Pass a Session '
    'as "session_id", with "agent_id" for another Agent\'s Session, or omit both to check '
    "your current Session."
)
_ID_SESSION_CONFLICT_MESSAGE = "Conflicting values for session_id; provide one intended value."

_FIELD_ALIASES = SpellingAliases(
    {"session_id": ("session", "session_key"), "agent_id": ("agent", "agent_name")}
)
# Words that mean "my current Session"; generated Session ids never take these forms.
_CURRENT_SESSION_WORDS = PLACEHOLDER_WORDS | {"current", "this", "active", "mine"}
_ID_KEYS = frozenset({"id", "workid", "subagentid"})
_WORK_ID_PREFIX = "sub_"
_SESSION_ID_PREFIX = "ses_"


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
    if is_placeholder(repaired.get("session_id"), _CURRENT_SESSION_WORDS):
        repaired.pop("session_id", None)
    if is_placeholder(repaired.get("agent_id")):
        repaired.pop("agent_id", None)
    return repaired


def _read_id_field(arguments: dict[str, Any]) -> None:
    """Read an ``id`` field by what its value is: a Session, Sub-Agent work, or unknown."""
    for key in [key for key in arguments if spelling(key) in _ID_KEYS]:
        value = arguments.pop(key)
        if is_placeholder(value):
            continue
        text = value.strip() if isinstance(value, str) else value
        if isinstance(text, str) and text.startswith(_WORK_ID_PREFIX):
            raise ToolContractError(_WORK_ID_MESSAGE_TEMPLATE.format(work_id=text))
        if spelling(key) == "id" and isinstance(text, str) and text.startswith(_SESSION_ID_PREFIX):
            session_id = arguments.get("session_id")
            if not is_placeholder(session_id, _CURRENT_SESSION_WORDS) and session_id != text:
                raise ToolContractError(_ID_SESSION_CONFLICT_MESSAGE)
            arguments["session_id"] = text
            continue
        raise ToolContractError(_ID_MESSAGE_TEMPLATE.format(value=json.dumps(value)))


def make_status_handler(
    agent_resolver: AgentResolver,
    sessions: ChatSessionManager,
    models: ModelRegistry,
    chat_runs: ChatRunManager,
    started_at: datetime | None,
    providers: ProviderRegistry | None = None,
    projects: ProjectStore | None = None,
    local_context_windows_loader: Callable[[], Mapping[str, Any]] | None = None,
    reasoning_render_describer: ReasoningRenderDescriber | None = None,
    timezone_name_loader: Callable[[], str] | None = None,
):
    """Create a status tool handler bound to runtime services."""

    def _load_local_context_windows() -> Mapping[str, Any]:
        if local_context_windows_loader is None:
            return {}
        try:
            return local_context_windows_loader()
        except Exception:
            _LOGGER.warning("Failed to load local-model context windows", exc_info=True)
            return {}

    def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:

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
            agent = agent_resolver.resolve_agent(context.project_id, agent_id)
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

        try:
            snapshot = sessions.get(
                SessionAddress(
                    project_id=context.project_id, agent_id=agent_id, session_id=session_id
                )
            ).status_snapshot()
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
            agent, models, providers, local_context_windows=_load_local_context_windows()
        )

        try:
            text = build_status_reply(
                agent,
                StatusSessionFacts(
                    first_message_at=snapshot.first_message_at,
                    user_message_count=snapshot.user_message_count,
                    latest_assistant_usage=snapshot.latest_assistant_usage,
                    session_usage=snapshot.session_usage,
                    cache_input_tokens=snapshot.cache_input_tokens,
                ),
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
                    agent.temperature,
                    model_details,
                ),
                timezone=(
                    ZoneInfo(timezone_name_loader()) if timezone_name_loader is not None else None
                ),
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
    local_context_windows_loader: Callable[[], Mapping[str, Any]] | None = None,
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
                local_context_windows_loader,
                reasoning_render_describer,
                timezone_name_loader,
            )
        ),
        open_input_schema=True,
        argument_normalizer=_normalize_status_arguments,
        result_schema={"type": "object", "required": ["text", "agent_id", "session_id"]},
        display=ToolDisplay(),
        parallel_safe=True,
    )
