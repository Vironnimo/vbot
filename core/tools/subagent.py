"""Tool registration for sub-agent spawning and result lookup."""

from __future__ import annotations

from core.subagents import SubAgentCoordinator
from core.tools.tools import JsonObject, ToolDisplay, ToolRegistry

SUBAGENT_TOOL_NAME = "subagent"
SUBAGENT_RESULT_TOOL_NAME = "subagent_result"

SUBAGENT_TOOL_DESCRIPTION = (
    "Spawn a sub-agent run in a persisted session. Runs in the background by "
    "default so you can fan out parallel work; set background:false to wait for "
    "the result. After a background spawn, end your turn instead of polling: when "
    "every sub-agent in the batch finishes, their complete final outputs are "
    "delivered to you automatically. Only check on a running sub-agent before then "
    "if the user explicitly asks for its status. When you are yourself a sub-agent, "
    "spawns always run in the foreground regardless of this setting; to run several "
    "in parallel, make all subagent calls in a single turn."
)
SUBAGENT_RESULT_TOOL_DESCRIPTION = (
    "Fetch the latest result from a spawned sub-agent session and mark it as "
    "retrieved. You normally do not need this: completed background batches are "
    "delivered to you automatically. Use it only when the user explicitly asks to "
    "check a sub-agent's status or result before the batch finishes."
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
        "background": {
            "type": "boolean",
            "description": (
                "Run in the background and return immediately so you can fan out "
                "parallel work (default true). Set false to wait for the sub-agent "
                "to finish and return its result."
            ),
            "default": True,
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
