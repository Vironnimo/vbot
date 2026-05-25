"""Tool registration for sub-agent spawning and result lookup."""

from __future__ import annotations

from core.subagents import SubAgentCoordinator
from core.tools.tools import JsonObject, ToolDisplay, ToolRegistry

SUBAGENT_TOOL_NAME = "subagent"
SUBAGENT_RESULT_TOOL_NAME = "subagent_result"

SUBAGENT_TOOL_DESCRIPTION = (
    "Spawn a sub-agent run in a persisted session. Use non-blocking mode for "
    "parallel work, or blocking mode when the caller must wait for the result."
)
SUBAGENT_RESULT_TOOL_DESCRIPTION = (
    "Fetch the latest result from a spawned sub-agent session and mark it as retrieved."
)

SUBAGENT_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "content": {
            "type": "string",
            "description": "Message to send to the spawned sub-agent.",
        },
        "agent_id": {
            "type": "string",
            "description": "Target agent id. Defaults to the calling agent.",
        },
        "blocking": {
            "type": "boolean",
            "description": "When true, wait for the sub-agent to finish and return its result.",
        },
        "session_id": {
            "type": "string",
            "description": "Target existing session id. Creates a new session when omitted.",
        },
    },
    "required": ["content"],
    "additionalProperties": False,
}

SUBAGENT_RESULT_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "session_id": {"type": "string", "description": "Sub-agent session id."},
        "agent_id": {
            "type": "string",
            "description": "Sub-agent id. Defaults to the calling agent.",
        },
        "run_id": {
            "type": "string",
            "description": "Optional in-memory sub-agent run id for live result lookup.",
        },
    },
    "required": ["session_id"],
    "additionalProperties": False,
}


def register_subagent_tools(
    registry: ToolRegistry,
    coordinator: SubAgentCoordinator,
) -> None:
    """Register the public sub-agent tools."""
    registry.register(
        SUBAGENT_TOOL_NAME,
        SUBAGENT_TOOL_DESCRIPTION,
        SUBAGENT_TOOL_PARAMETERS,
        coordinator.spawn,
        display=ToolDisplay(
            summary_fields=("agent_id", "content"),
            hidden_argument_keys=("content",),
        ),
    )
    registry.register(
        SUBAGENT_RESULT_TOOL_NAME,
        SUBAGENT_RESULT_TOOL_DESCRIPTION,
        SUBAGENT_RESULT_TOOL_PARAMETERS,
        coordinator.result,
        display=ToolDisplay(summary_fields=("agent_id", "session_id")),
    )


__all__ = [
    "SUBAGENT_RESULT_TOOL_DESCRIPTION",
    "SUBAGENT_RESULT_TOOL_NAME",
    "SUBAGENT_RESULT_TOOL_PARAMETERS",
    "SUBAGENT_TOOL_DESCRIPTION",
    "SUBAGENT_TOOL_NAME",
    "SUBAGENT_TOOL_PARAMETERS",
    "register_subagent_tools",
]
