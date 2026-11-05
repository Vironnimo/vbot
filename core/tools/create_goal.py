"""The configurable Tool that starts a Session goal from a direct user request."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.runs import RunKind
from core.sessions import SessionAddress
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
    from core.automation.goals import GoalService

_LOGGER = get_logger("tools.create_goal")

# Reasons for the Agent-facing text: tools/update_goal.md.
CREATE_GOAL_TOOL_DESCRIPTION = (
    "Keep this Session working on an objective through automatic follow-up turns. "
    "Use it when the direct human request is a long-running objective, even if the "
    'user did not say "goal". Skip tasks that fit in one turn.'
)
CREATE_GOAL_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "objective": {
            "type": "string",
            "description": "The requested end result, inferred from the user's direct request.",
        }
    },
    "required": ["objective"],
    "additionalProperties": False,
}


def register_create_goal_tool(registry: ToolRegistry, goals: GoalService) -> None:
    async def create_goal(context: ToolContext, arguments: JsonObject) -> JsonObject:
        from core.automation.goals import GoalOperationError

        if (
            context.subagent_session
            or not (
                context.direct_user_input or context.run_kind in (RunKind.USER, RunKind.CHANNEL)
            )
            or (
                context.reply_surface is not None
                and context.reply_surface.conversation_kind == "group"
            )
        ):
            return tool_failure(
                "goal_creation_not_allowed",
                "Nothing was changed. Create a goal only for a direct user request in "
                "an individual conversation; continue the current task without creating one.",
            )
        try:
            await goals.create_from_agent(
                SessionAddress(context.project_id, context.agent_id, context.session_id),
                arguments["objective"],
                run_id=context.run_id,
                direct_user_input=context.direct_user_input,
            )
        except GoalOperationError as error:
            return tool_failure(error.code, str(error))
        except Exception:
            _LOGGER.error(
                "create_goal failed (agent=%s session=%s)",
                context.agent_id,
                context.session_id,
                exc_info=True,
            )
            return tool_failure(
                "goal_creation_failed",
                "The goal could not be started. Continue the current task and tell the user "
                "that automatic continuation could not be enabled.",
            )
        return tool_success({"content": "Goal started."})

    registry.register(
        "create_goal",
        CREATE_GOAL_TOOL_DESCRIPTION,
        CREATE_GOAL_TOOL_PARAMETERS,
        create_goal,
        summary="Start a goal for a long-running user request.",
        execution_slot_required=False,
        result_schema={"type": "object"},
        display=ToolDisplay(summary_builder=lambda _arguments: "Start goal"),
    )
