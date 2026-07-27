"""Tool registration for sub-agent spawning and result lookup."""

from __future__ import annotations

from typing import Any

from core.settings import ALLOWED_THINKING_EFFORTS
from core.subagents import SubAgentCoordinator, SubAgentPromptTarget
from core.tools.tools import (
    JsonObject,
    ToolDisplay,
    ToolPromptBlockRegistry,
    ToolRegistry,
    operation_envelope_schema,
)

SUBAGENT_TOOL_NAME = "subagent"
SUBAGENT_RESULT_TOOL_NAME = "subagent_result"

SUBAGENT_TOOL_DESCRIPTION = (
    "Delegate work by starting or queueing a Run in a persisted Sub-Agent Session."
)
SUBAGENT_RESULT_TOOL_DESCRIPTION = (
    "Return the current queued or running status, or the terminal result, of a spawned "
    "Sub-Agent Run without waiting for active work to finish."
)

SUBAGENT_PROMPT_BLOCK_TEMPLATE = (
    "## Sub-Agents\n\n"
    "A Sub-Agent is a delegated Run in its own persisted Session. It can use the "
    "calling Agent or one of the additional Agents listed below. You remain "
    "responsible for deciding what to delegate, integrating the results, and "
    "verifying the final outcome.\n\n"
    "Use the `start` operation to create a separate Session; omit `agent_id` there "
    "to delegate to the calling Agent, which is always available and is not repeated "
    "in the list below.\n\n"
    "The following additional Agents are available. Use each Agent id exactly as "
    "shown:\n\n"
    "{subagent_list}\n\n"
    "Use `subagent` for a bounded task that another Agent can perform independently. "
    "Send a self-contained task containing the goal, relevant context, scope, "
    "constraints, and expected result. When several Sub-Agents may edit shared "
    "files, give them non-overlapping ownership; do not run conflicting edits in "
    "parallel.\n\n"
    "Project Agents may be listed with a qualified `agent@project` id. Use every "
    "Agent id exactly as listed above.\n\n"
    "Inside either operation, `background` defaults to `true`. Use background Runs when you "
    "can continue without their immediate results. Issue independent sibling "
    "`subagent` calls in the same turn so they can run concurrently. Do not poll "
    "background Runs: after finishing any independent work, end your turn. When "
    "every Sub-Agent in the batch finishes, their complete final outputs are "
    "delivered to you automatically.\n\n"
    "Set `background` to `false` when your next step depends immediately on the "
    "result. When you are yourself a Sub-Agent, every spawn runs in the foreground "
    "regardless of the requested setting; issue sibling calls in the same turn to "
    "run them concurrently.\n\n"
    "Optional `model` and `thinking_effort` values override only the newly admitted "
    "Sub-Agent Run. They do not modify the target Agent or Session, persist into a "
    "later continuation, or pass to nested Sub-Agents.\n\n"
    "Use the `continue` operation for a specific existing Session and repeat both "
    "the exact `agent_id` and `session_id` returned by the original `subagent` call; "
    "Session ids are Agent-scoped. Use `subagent_result` only when the user explicitly "
    "asks for a running Sub-Agent's status or result before automatic batch delivery."
)

NO_ADDITIONAL_SUBAGENTS_TEXT = "**No additional Agents are available.**"

_SUBAGENT_CONTENT_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": "Self-contained task or message to send to the target Sub-Agent.",
}
_SUBAGENT_AGENT_ID_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": "Target Agent id from the allowed values.",
}
_SUBAGENT_BACKGROUND_PARAMETER: JsonObject = {
    "type": "boolean",
    "description": (
        "When true, return after the Run is started or queued. When false, wait for "
        "its final result. Defaults to true."
    ),
    "default": True,
}
_SUBAGENT_MODEL_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Run-local primary Model override in <provider>/<model-id> form. "
        "Does not modify the target Agent or Session."
    ),
}
_SUBAGENT_THINKING_PARAMETER: JsonObject = {
    "type": "string",
    "enum": sorted(ALLOWED_THINKING_EFFORTS),
    "description": (
        "Run-local thinking effort override. Omit to inherit the target Agent; "
        "an empty string selects the Provider default."
    ),
}


def _subagent_operation(
    description: str,
    properties: JsonObject,
    *,
    required: tuple[str, ...],
) -> JsonObject:
    return {
        "type": "object",
        "description": description,
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


SUBAGENT_TOOL_PARAMETERS: JsonObject = operation_envelope_schema(
    {
        "start": _subagent_operation(
            "Create a new persisted Sub-Agent Session. Omit agent_id to use the calling Agent.",
            {
                "content": _SUBAGENT_CONTENT_PARAMETER,
                "agent_id": _SUBAGENT_AGENT_ID_PARAMETER,
                "background": _SUBAGENT_BACKGROUND_PARAMETER,
                "model": _SUBAGENT_MODEL_PARAMETER,
                "thinking_effort": _SUBAGENT_THINKING_PARAMETER,
            },
            required=("content",),
        ),
        "continue": _subagent_operation(
            "Continue one existing Agent-scoped Sub-Agent Session.",
            {
                "content": _SUBAGENT_CONTENT_PARAMETER,
                "agent_id": _SUBAGENT_AGENT_ID_PARAMETER,
                "session_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Existing Sub-Agent Session id returned by start.",
                },
                "background": _SUBAGENT_BACKGROUND_PARAMETER,
                "model": _SUBAGENT_MODEL_PARAMETER,
                "thinking_effort": _SUBAGENT_THINKING_PARAMETER,
            },
            required=("content", "agent_id", "session_id"),
        ),
    },
    description=(
        "Set request.operation to start for a new Sub-Agent Session or continue for an "
        "existing one, and include the operation arguments in the same request object."
    ),
)

SUBAGENT_RESULT_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "session_id": {
            "type": "string",
            "minLength": 1,
            "description": "Persisted Sub-Agent Session id returned by subagent.",
        },
        "agent_id": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Agent id that owns the Sub-Agent Session. Omit it if the Session belongs "
                "to the calling Agent."
            ),
        },
        "run_id": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Specific in-memory Sub-Agent Run id to retrieve. Omit it to resolve the Run "
                "associated with the Session."
            ),
        },
    },
    "required": ["session_id"],
    "additionalProperties": False,
}


def register_subagent_tools(
    registry: ToolRegistry,
    coordinator: SubAgentCoordinator,
    prompt_blocks: ToolPromptBlockRegistry | None = None,
) -> None:
    """Register the public sub-agent tools."""
    registry.register(
        SUBAGENT_TOOL_NAME,
        SUBAGENT_TOOL_DESCRIPTION,
        SUBAGENT_TOOL_PARAMETERS,
        coordinator.spawn,
        result_schema={"type": "object"},
        display=ToolDisplay(
            summary_builder=_subagent_display_summary,
            hidden_argument_keys=("content",),
        ),
    )
    registry.register(
        SUBAGENT_RESULT_TOOL_NAME,
        SUBAGENT_RESULT_TOOL_DESCRIPTION,
        SUBAGENT_RESULT_TOOL_PARAMETERS,
        coordinator.result,
        result_schema={"type": "object"},
        display=ToolDisplay(summary_fields=("agent_id", "session_id")),
    )
    if prompt_blocks is not None:
        prompt_blocks.register(
            SUBAGENT_TOOL_NAME,
            render=lambda context: _render_subagent_prompt_block(context, coordinator),
        )


def _subagent_display_summary(arguments: JsonObject) -> str:
    operation_arguments = arguments.get("request")
    if not isinstance(operation_arguments, dict):
        return ""
    operation = operation_arguments.get("operation")
    if operation not in {"start", "continue"}:
        return ""
    parts = [operation]
    agent_id = operation_arguments.get("agent_id")
    if isinstance(agent_id, str) and agent_id:
        parts.append(agent_id)
    content = operation_arguments.get("content")
    if isinstance(content, str) and content:
        parts.append(content)
    return " · ".join(parts)


def _render_subagent_prompt_block(context: Any, coordinator: SubAgentCoordinator) -> str:
    targets = coordinator.prompt_targets(context.agent, context.agent_project_id)
    rendered_targets = _format_subagent_targets(targets)
    return SUBAGENT_PROMPT_BLOCK_TEMPLATE.replace("{subagent_list}", rendered_targets)


def _format_subagent_targets(targets: list[SubAgentPromptTarget]) -> str:
    if not targets:
        return NO_ADDITIONAL_SUBAGENTS_TEXT
    lines: list[str] = []
    for target in targets:
        name = _single_line(target.name) or target.agent_id
        description = _single_line(target.description)
        suffix = f" — {name}"
        if description:
            suffix = f"{suffix} — {description}"
        lines.append(f"- `{target.agent_id}`{suffix}")
    return "\n".join(lines)


def _single_line(value: str) -> str:
    return " ".join(value.split())


__all__ = [
    "SUBAGENT_RESULT_TOOL_DESCRIPTION",
    "SUBAGENT_RESULT_TOOL_NAME",
    "SUBAGENT_RESULT_TOOL_PARAMETERS",
    "SUBAGENT_PROMPT_BLOCK_TEMPLATE",
    "SUBAGENT_TOOL_DESCRIPTION",
    "SUBAGENT_TOOL_NAME",
    "SUBAGENT_TOOL_PARAMETERS",
    "register_subagent_tools",
]
