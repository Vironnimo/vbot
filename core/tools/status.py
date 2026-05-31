"""Built-in status tool that reports current or targeted agent/session/runtime status."""

from __future__ import annotations

from datetime import datetime

from core.agents.agents import AgentNotFoundError, AgentStore
from core.chat.commands import (
    build_status_reply,
    resolve_status_activity,
    resolve_status_model_details,
)
from core.chat.errors import ChatSessionError
from core.models.models import ModelRegistry
from core.runs import ChatRunManager
from core.sessions import ChatSessionManager
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
    "Show status for a chat session. With no arguments, checks this session. "
    "Use session_id to check another session for this agent; use both session_id "
    "and agent_id to check another agent's session. Returns activity running/idle "
    "and active run timestamps."
)
STATUS_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "session_id": {
            "type": "string",
            "minLength": 1,
            "description": "Optional session id to inspect. Alone, it targets this agent.",
        },
        "agent_id": {
            "type": "string",
            "minLength": 1,
            "description": "Optional agent id for the target session. Requires session_id.",
        },
    },
    "additionalProperties": False,
}


def make_status_handler(
    agents: AgentStore,
    sessions: ChatSessionManager,
    models: ModelRegistry,
    chat_runs: ChatRunManager,
    started_at: datetime | None,
):
    """Create a status tool handler bound to runtime services."""

    def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        try:
            requested_agent_id = _optional_target_string(arguments, "agent_id")
            requested_session_id = _optional_target_string(arguments, "session_id")
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

        try:
            agent = agents.get(agent_id)
        except AgentNotFoundError:
            return tool_failure("agent_not_found", f"agent does not exist: {agent_id}")

        try:
            messages = sessions.get(agent_id, session_id).load()
        except ChatSessionError:
            return tool_failure(
                "session_not_found",
                f"session does not exist for agent {agent_id}: {session_id}",
            )

        activity = resolve_status_activity(chat_runs, agent_id, session_id)
        context_window, model_display_name = resolve_status_model_details(agent, models)

        try:
            text = build_status_reply(
                agent,
                messages,
                context_window,
                started_at,
                model_display_name,
                activity,
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
    agents: AgentStore,
    sessions: ChatSessionManager,
    models: ModelRegistry,
    chat_runs: ChatRunManager,
    started_at: datetime | None,
) -> None:
    """Register the status tool with a vBot tool registry."""
    registry.register(
        STATUS_TOOL_NAME,
        STATUS_TOOL_DESCRIPTION,
        STATUS_TOOL_PARAMETERS,
        make_status_handler(agents, sessions, models, chat_runs, started_at),
        display=ToolDisplay(),
    )


def _optional_target_string(arguments: JsonObject, field_name: str) -> str | None:
    value = arguments.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value
