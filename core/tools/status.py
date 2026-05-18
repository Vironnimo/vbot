"""Built-in status tool that reports current agent/session/runtime status."""

from __future__ import annotations

from datetime import datetime

from core.agents.agents import AgentStore
from core.chat.chat import ChatSessionManager
from core.chat.commands import build_status_text
from core.models.models import ModelRegistry
from core.tools.tools import JsonObject, ToolContext, ToolRegistry, tool_success

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
        context_window = None

        try:
            agent = agents.get(context.agent_id)
        except Exception:
            agent = None

        try:
            messages = sessions.get(context.agent_id, context.session_id).load()
        except Exception:
            messages = []

        if agent is not None:
            provider_id, separator, model_id = agent.model.partition("/")
            if separator and model_id:
                try:
                    context_window = models.get(provider_id, model_id).context_window
                except KeyError:
                    context_window = None
                except Exception:
                    context_window = None

        try:
            text = build_status_text(agent, messages, context_window, started_at)
        except Exception:
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
    )
