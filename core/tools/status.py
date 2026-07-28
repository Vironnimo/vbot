"""Built-in status tool that reports current or targeted agent/session/runtime status."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from core.chat.commands import (
    build_status_reply,
    resolve_actual_thinking_effort,
    resolve_status_activity,
    resolve_status_model_details,
    resolve_status_project_label,
)
from core.chat.errors import ChatSessionError
from core.models.models import ModelRegistry
from core.projects import AgentResolutionError, AgentResolver, ProjectStore
from core.providers.providers import ProviderRegistry
from core.runs import ChatRunManager
from core.sessions import ChatSessionManager
from core.tools.arguments import optional_string
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolRegistry,
    tool_failure,
    tool_success,
)
from core.utils.logging import get_logger

_LOGGER = get_logger("tools.status")

STATUS_TOOL_NAME = "status"
STATUS_TOOL_DESCRIPTION = (
    "Show status for the current chat Session. Optionally provide session_id for another "
    "Session owned by this Agent, or provide agent_id with session_id for another Agent's "
    "Session."
)
_STATUS_SESSION_ID_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": "Session id to inspect.",
}
_STATUS_AGENT_ID_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": "Agent id that owns the target Session.",
}

STATUS_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "description": (
        "Omit both fields for the current Session. agent_id is valid only together with session_id."
    ),
    "properties": {
        "session_id": _STATUS_SESSION_ID_PARAMETER,
        "agent_id": _STATUS_AGENT_ID_PARAMETER,
    },
    "additionalProperties": False,
}


def make_status_handler(
    agent_resolver: AgentResolver,
    sessions: ChatSessionManager,
    models: ModelRegistry,
    chat_runs: ChatRunManager,
    started_at: datetime | None,
    providers: ProviderRegistry | None = None,
    projects: ProjectStore | None = None,
    local_context_windows_loader: Callable[[], Mapping[str, Any]] | None = None,
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
        unknown_arguments = set(arguments) - {"agent_id", "session_id"}
        if unknown_arguments:
            names = ", ".join(sorted(unknown_arguments))
            return tool_failure("invalid_arguments", f"Unknown argument(s): {names}")

        try:
            requested_agent_id = optional_string(arguments.get("agent_id"), field_name="agent_id")
            requested_session_id = optional_string(
                arguments.get("session_id"), field_name="session_id"
            )
        except ValueError as error:
            return tool_failure("invalid_arguments", str(error))
        if requested_agent_id is not None and requested_session_id is None:
            return tool_failure(
                "invalid_arguments",
                "agent_id requires session_id; provide no target, session_id, "
                "or both agent_id and session_id",
            )

        agent_id = requested_agent_id or context.agent_id
        session_id = requested_session_id or context.session_id

        # Resolve through the one seam so ``/status`` shows the agent profile the
        # run actually uses: a project run (``context.project_id`` set) reports the
        # resolved config-agent profile, an identity run resolves the store agent
        # exactly as before. A resolver "not found" is the same clean failure the
        # former ``AgentNotFoundError`` produced.
        try:
            agent = agent_resolver.resolve_agent(context.project_id, agent_id)
        except AgentResolutionError:
            return tool_failure("agent_not_found", f"agent does not exist: {agent_id}")

        try:
            messages = sessions.get(agent_id, session_id, context.project_id).load()
        except ChatSessionError:
            return tool_failure(
                "session_not_found",
                f"session does not exist for agent {agent_id}: {session_id}",
            )

        activity = resolve_status_activity(chat_runs, agent_id, session_id, context.project_id)
        model_details = resolve_status_model_details(
            agent, models, providers, local_context_windows=_load_local_context_windows()
        )

        try:
            text = build_status_reply(
                agent,
                messages,
                model_details.context_window,
                started_at,
                model_details.display_name,
                activity,
                actual_thinking_effort=resolve_actual_thinking_effort(
                    agent.thinking_effort,
                    model_details.reasoning_levels,
                    model_details.reasoning_control,
                    model_details.reasoning_budget_max,
                ),
                project_label=resolve_status_project_label(projects, context.project_id),
            )
        except Exception:
            _LOGGER.error("Failed to build status tool reply", exc_info=True)
            raise

        return tool_success(
            {
                "text": text,
                "agent_id": agent_id,
                "session_id": session_id,
                "activity": activity.activity,
                "run_id": activity.run_id,
                "created_at": activity.created_at,
                "updated_at": activity.updated_at,
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
) -> None:
    """Register the status tool with a vBot tool registry."""
    registry.register(
        STATUS_TOOL_NAME,
        STATUS_TOOL_DESCRIPTION,
        STATUS_TOOL_PARAMETERS,
        make_status_handler(
            agent_resolver,
            sessions,
            models,
            chat_runs,
            started_at,
            providers,
            projects,
            local_context_windows_loader,
        ),
        result_schema={"type": "object", "required": ["text", "agent_id", "session_id"]},
        display=ToolDisplay(),
        parallel_safe=True,
    )
