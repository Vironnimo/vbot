"""The ``update_goal`` Tool: the Agent ends the automatic turns of its Session goal."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from core.sessions import SessionAddress
from core.tools.availability import TOOL_ACTIVATION_SESSION_GRANT, UPDATE_GOAL_TOOL_NAME
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolRegistry,
    tool_failure,
    tool_success,
)
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.automation.goals import AgentGoalStatus, GoalService

_LOGGER = get_logger("tools.update_goal")

# Reasons for every sentence: ``.vorch/domain-maps/tools/update_goal.md``.
UPDATE_GOAL_TOOL_DESCRIPTION = (
    "Mark the session goal complete, blocked or paused, which stops its automatic turns. "
    "Mark it complete only when you checked every part of the goal against the current "
    "state, not because you are stopping work."
)
UPDATE_GOAL_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["complete", "blocked", "paused"],
            "description": (
                "blocked: you cannot take another step without the user or an external "
                "change; ask the user in your reply. paused: only when the user asks you "
                "to stop or pause the goal."
            ),
        }
    },
    "required": ["status"],
}
_RESULTS: dict[str, str] = {
    "complete": "Goal marked complete; no further automatic turns. Finish your reply to the user.",
    "blocked": "Goal marked blocked; automatic turns continue after the user replies.",
    "paused": (
        "Goal paused. The user can resume it with /goal resume or delete it with /goal clear."
    ),
}
_STATUS_ALIASES: dict[str, str] = {
    "complete": "complete",
    "done": "complete",
    "completed": "complete",
    "achieved": "complete",
    "finished": "complete",
    "blocked": "blocked",
    "block": "blocked",
    "stuck": "blocked",
    "paused": "paused",
    "pause": "paused",
    "stop": "paused",
    "stopped": "paused",
}
_DISPLAY = {"complete": "Goal complete", "blocked": "Goal blocked", "paused": "Goal paused"}
NO_GOAL_MESSAGE = (
    "Nothing was changed. This session has no goal; only the user can set one with /goal."
)


def register_update_goal_tool(registry: ToolRegistry, goals: GoalService) -> None:
    """Register ``update_goal``, which a Session with an open goal grants."""

    async def update_goal(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return await _update_goal(goals, context, arguments)

    registry.register(
        UPDATE_GOAL_TOOL_NAME,
        UPDATE_GOAL_TOOL_DESCRIPTION,
        UPDATE_GOAL_TOOL_PARAMETERS,
        update_goal,
        open_input_schema=True,
        handler_validates_arguments=True,
        catalog_visible=False,
        session_scoped=True,
        activation=TOOL_ACTIVATION_SESSION_GRANT,
        execution_slot_required=False,
        result_schema={"type": "object"},
        display=ToolDisplay(summary_builder=_summary),
    )


def _status(arguments: Any) -> str | None:
    if not isinstance(arguments, dict):
        return None
    value = arguments.get("status")
    if not isinstance(value, str):
        return None
    return _STATUS_ALIASES.get(value.strip().casefold())


def _summary(arguments: JsonObject) -> str | None:
    status = _status(arguments)
    return _DISPLAY.get(status) if status is not None else None


async def _update_goal(
    goals: GoalService, context: ToolContext, arguments: JsonObject
) -> JsonObject:
    from core.automation.goals import NoGoalError

    status = _status(arguments)
    if status is None:
        received = arguments.get("status") if isinstance(arguments, dict) else None
        shown = received if isinstance(received, str) else ""
        return tool_failure(
            "invalid_arguments",
            f'Nothing was changed. Unknown status "{shown}". '
            "Use one of: complete, blocked, paused.",
        )
    address = SessionAddress(context.project_id, context.agent_id, context.session_id)
    try:
        update = await goals.update_from_agent(
            address, cast("AgentGoalStatus", status), run_id=context.run_id
        )
    except NoGoalError:
        return tool_failure("no_goal", NO_GOAL_MESSAGE)
    except Exception:
        _LOGGER.error(
            "update_goal failed (agent=%s session=%s)",
            context.agent_id,
            context.session_id,
            exc_info=True,
        )
        return tool_failure(
            "goal_update_failed",
            "Nothing was changed. The goal could not be updated: vBot could not save the "
            "session. Try the call again once; if it fails again, tell the user.",
        )
    text = _RESULTS[status] if update.changed else f"The goal is already {status}; nothing changed."
    extra = [key for key in arguments if key != "status"] if isinstance(arguments, dict) else []
    for key in extra:
        text += f" {key} has no effect; tell the user in your reply."
    return tool_success({"content": text})


__all__ = [
    "UPDATE_GOAL_TOOL_DESCRIPTION",
    "UPDATE_GOAL_TOOL_PARAMETERS",
    "register_update_goal_tool",
]
