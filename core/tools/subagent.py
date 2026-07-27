"""Tool registration for sub-agent spawning and result lookup."""

from __future__ import annotations

from typing import Any

from core.settings import ALLOWED_THINKING_EFFORTS
from core.subagents import SubAgentCoordinator, SubAgentPromptTarget
from core.tools.tools import JsonObject, ToolDisplay, ToolPromptBlockRegistry, ToolRegistry

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
    "To delegate to a separate Session of your own Agent, omit `agent_id`. The "
    "calling Agent is always available and is not repeated in the list below.\n\n"
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
    "At the top level, `background` defaults to `true`. Use background Runs when you "
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
    "Omit `session_id` to create a new Sub-Agent Session. To continue a specific "
    "existing Session, repeat both the exact `agent_id` and `session_id` returned by "
    "the original `subagent` call; Session ids are Agent-scoped and `agent_id` is "
    "required for continuation. Use `subagent_result` only when the user explicitly "
    "asks for a running Sub-Agent's status or result before automatic batch delivery."
)

NO_ADDITIONAL_SUBAGENTS_TEXT = "**No additional Agents are available.**"

SUBAGENT_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "content": {
            "type": "string",
            "description": "Self-contained task or message to send to the target Sub-Agent.",
        },
        "agent_id": {
            "type": "string",
            "description": (
                "Target Agent id from the allowed values. Omit it to run the calling Agent "
                "as a Sub-Agent when creating a new Session. Required with session_id."
            ),
        },
        "background": {
            "type": "boolean",
            "description": (
                "When true, return after the Run is started or queued. When false, wait for "
                "its final result. Defaults to true."
            ),
            "default": True,
        },
        "session_id": {
            "type": "string",
            "description": (
                "Existing Sub-Agent Session to continue. Repeat its owning agent_id with "
                "this value. Creates a new persisted Session when omitted."
            ),
        },
        "model": {
            "type": "string",
            "description": (
                "Run-local primary Model override in <provider>/<model-id> form. "
                "Does not modify the target Agent or Session."
            ),
        },
        "thinking_effort": {
            "type": "string",
            "enum": sorted(ALLOWED_THINKING_EFFORTS),
            "description": (
                "Run-local thinking effort override. Omit to inherit the target Agent; "
                "an empty string selects the Provider default."
            ),
        },
    },
    "required": ["content"],
    "additionalProperties": False,
}

SUBAGENT_RESULT_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "session_id": {
            "type": "string",
            "description": "Persisted Sub-Agent Session id returned by subagent.",
        },
        "agent_id": {
            "type": "string",
            "description": (
                "Agent id that owns the Sub-Agent Session. Omit it if the Session belongs "
                "to the calling Agent."
            ),
        },
        "run_id": {
            "type": "string",
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
    if prompt_blocks is not None:
        prompt_blocks.register(
            SUBAGENT_TOOL_NAME,
            render=lambda context: _render_subagent_prompt_block(context, coordinator),
        )


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
