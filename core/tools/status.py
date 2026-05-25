"""Built-in status tool that reports current agent/session/runtime status."""

from __future__ import annotations

from datetime import datetime

from core.agents.agents import AgentNotFoundError, AgentStore
from core.chat.commands import build_status_reply, build_status_text, resolve_status_model_details
from core.chat.errors import ChatSessionError
from core.models.models import ModelRegistry
from core.sessions import ChatSessionManager
from core.tools.tools import JsonObject, ToolContext, ToolDisplay, ToolRegistry, tool_success
from core.utils.logging import get_logger

_LOGGER = get_logger("tools.status")

STATUS_TOOL_NAME = "status"
STATUS_TOOL_DESCRIPTION = "Show current session and runtime status."
STATUS_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


def make_status_handler(
    agents: AgentStore,
    sessions: ChatSessionManager,
    models: ModelRegistry,
    started_at: datetime | None,
):
    """Create a status tool handler bound to runtime services."""

    def handler(context: ToolContext, _arguments: JsonObject) -> JsonObject:
        agent = None
        messages = []

        try:
            agent = agents.get(context.agent_id)
        except AgentNotFoundError:
            _LOGGER.warning(
                "Failed to load agent %r while running status tool",
                context.agent_id,
                exc_info=True,
            )
            agent = None
        except Exception:
            _LOGGER.error(
                "Failed to load agent %r while running status tool",
                context.agent_id,
                exc_info=True,
            )
            agent = None

        try:
            messages = sessions.get(context.agent_id, context.session_id).load()
        except ChatSessionError:
            _LOGGER.warning(
                "Failed to load session %r for agent %r while running status tool",
                context.session_id,
                context.agent_id,
                exc_info=True,
            )
            messages = []
        except Exception:
            _LOGGER.error(
                "Failed to load session %r for agent %r while running status tool",
                context.session_id,
                context.agent_id,
                exc_info=True,
            )
            messages = []

        context_window, model_display_name = resolve_status_model_details(agent, models)

        try:
            text = build_status_reply(
                agent,
                messages,
                context_window,
                started_at,
                model_display_name,
            )
        except Exception:
            _LOGGER.error("Failed to build status tool reply", exc_info=True)
            text = build_status_text(None, [], None, None)
        return tool_success({"text": text})

    return handler


def register_status_tool(
    registry: ToolRegistry,
    agents: AgentStore,
    sessions: ChatSessionManager,
    models: ModelRegistry,
    started_at: datetime | None,
) -> None:
    """Register the status tool with a vBot tool registry."""
    registry.register(
        STATUS_TOOL_NAME,
        STATUS_TOOL_DESCRIPTION,
        STATUS_TOOL_PARAMETERS,
        make_status_handler(agents, sessions, models, started_at),
        display=ToolDisplay(),
    )
