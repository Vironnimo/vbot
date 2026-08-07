"""Tool registration for Sub-Agent work."""

from __future__ import annotations

from typing import Any

from core.settings import ALLOWED_THINKING_EFFORTS
from core.subagents import SubAgentCoordinator, SubAgentPromptTarget
from core.tools.tools import (
    JsonObject,
    ToolDisplay,
    ToolDisplayPart,
    ToolPromptBlockRegistry,
    ToolRegistry,
)

SUBAGENT_TOOL_NAME = "subagent"

SUBAGENT_TOOL_DESCRIPTION = (
    "Run, inspect, or cancel owned Sub-Agent work. Top-level run actions return "
    "immediately and deliver results automatically; run actions made by a Sub-Agent "
    "wait for completion."
)

SUBAGENT_PROMPT_BLOCK_TEMPLATE = (
    "## Sub-Agents\n\n"
    "A Sub-Agent is a delegated Run in its own persisted Session. It can use the "
    "calling Agent or one of the additional Agents listed below. You remain "
    "responsible for deciding what to delegate, integrating the results, and "
    "verifying the final outcome.\n\n"
    'Use `action: "run"` to start work in a new Session or continue an existing '
    "Sub-Agent Session. Omit `session_id` to create a separate Session; omit `agent_id` "
    "there to use the calling Agent, which is always available and is not repeated in "
    "the list below. To continue a prior Sub-Agent Session, repeat both its exact "
    "`agent_id` and `session_id`.\n\n"
    "The following additional Agents are available. Use each Agent id exactly as "
    "shown:\n\n"
    "{subagent_list}\n\n"
    "Use `subagent` for a bounded task that another Agent can perform independently. "
    "When starting a new Session, send a self-contained task containing the goal, "
    "relevant context, scope, constraints, and expected result. A continuation message "
    "may rely on that Sub-Agent Session's existing history; include the follow-up "
    "instruction and any new context. When several Sub-Agents may edit shared files, "
    "give them non-overlapping ownership; do not run conflicting edits in parallel.\n\n"
    "Project Agents may be listed with a qualified `agent@project` id. Use every "
    "Agent id exactly as listed above.\n\n"
    "{execution_guidance}\n\n"
    "Issue independent sibling `subagent` calls in the same turn so they can run "
    "concurrently.\n\n"
    "Optional `model` and `thinking_effort` values override only the newly admitted "
    "Sub-Agent Run. They do not modify the target Agent or Session, persist into a "
    "later continuation, or pass to nested Sub-Agents.\n\n"
    "Each run action returns one stable `id`. Use only that `id` with "
    '`action: "status"` or `action: "cancel"`; queued and running state are internal '
    "and never change the handle. Status is a non-blocking snapshot. Cancellation waits "
    "until that exact owned work is cancelled and cannot target another Parent Session's "
    "work."
)

NO_ADDITIONAL_SUBAGENTS_TEXT = "**No additional Agents are available.**"
TOP_LEVEL_EXECUTION_GUIDANCE = (
    "You are the top-level Agent. Every `run` action starts in the background and "
    "returns immediately; vBot monitors it, so you do not need to keep the current Run open. "
    "Continue work that does not depend on the result, or finish the current Run now. Do not "
    "poll merely to wait; request status only when your next action genuinely depends on the "
    "result. At Run end, vBot combines every background result already finished into one "
    "automatic follow-up Run. Work still running at that boundary is delivered later."
)
NESTED_EXECUTION_GUIDANCE = (
    "You are a Sub-Agent. Every `run` action executes in the foreground and the Tool "
    "Call returns only when that work finishes. Sibling calls issued together still "
    "run concurrently."
)

_SUBAGENT_ID_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": "Stable id returned by run. Required for status and cancel.",
}
_SUBAGENT_CONTENT_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Task or continuation message. Required for run; make it self-contained unless "
        "continuing session_id."
    ),
}
_SUBAGENT_AGENT_ID_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Target Agent for run. Omit to use the caller when starting a new Session; required "
        "with session_id."
    ),
}
_SUBAGENT_MODEL_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Model override for run as <provider>/<model-id>. Omit to inherit the target Agent; "
        "applies only to this Run."
    ),
}
_SUBAGENT_THINKING_PARAMETER: JsonObject = {
    "type": "string",
    "enum": sorted(ALLOWED_THINKING_EFFORTS),
    "description": (
        "Thinking effort for run. Omit to inherit the target Agent; an empty string selects "
        "the Provider default. Applies only to this Run."
    ),
}
_SUBAGENT_SESSION_ID_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Existing Sub-Agent Session to continue. Omit to start a new Session; requires agent_id."
    ),
}


SUBAGENT_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["run", "status", "cancel"],
            "description": (
                "Lifecycle action: run starts or continues work, status inspects it, "
                "cancel stops it."
            ),
        },
        "content": _SUBAGENT_CONTENT_PARAMETER,
        "agent_id": _SUBAGENT_AGENT_ID_PARAMETER,
        "session_id": _SUBAGENT_SESSION_ID_PARAMETER,
        "model": _SUBAGENT_MODEL_PARAMETER,
        "thinking_effort": _SUBAGENT_THINKING_PARAMETER,
        "id": _SUBAGENT_ID_PARAMETER,
    },
    "required": ["action"],
}


def register_subagent_tools(
    registry: ToolRegistry,
    coordinator: SubAgentCoordinator,
    prompt_blocks: ToolPromptBlockRegistry | None = None,
) -> None:
    """Register the public Sub-Agent Tool."""
    registry.register(
        SUBAGENT_TOOL_NAME,
        SUBAGENT_TOOL_DESCRIPTION,
        SUBAGENT_TOOL_PARAMETERS,
        coordinator.spawn,
        open_input_schema=True,
        result_schema={"type": "object"},
        display=ToolDisplay(
            parts_builder=_subagent_display_parts,
            hidden_argument_keys=("content",),
        ),
    )
    if prompt_blocks is not None:
        prompt_blocks.register(
            SUBAGENT_TOOL_NAME,
            render=lambda context: _render_subagent_prompt_block(context, coordinator),
        )


def _subagent_display_parts(arguments: JsonObject) -> tuple[ToolDisplayPart, ...]:
    action = arguments.get("action")
    if action not in {"run", "status", "cancel"}:
        return ()
    agent_id = arguments.get("agent_id")
    if action == "run":
        parts: list[ToolDisplayPart] = []
        if isinstance(agent_id, str) and agent_id:
            parts.append(ToolDisplayPart(agent_id, kind="identifier", truncate="middle"))
        content = arguments.get("content")
        if isinstance(content, str) and content:
            parts.append(ToolDisplayPart(content))
        if parts:
            return tuple(parts)
    parts = [ToolDisplayPart(action, truncate="never", tooltip="none")]
    work_id = arguments.get("id")
    target = work_id if isinstance(work_id, str) and work_id else agent_id
    if isinstance(target, str) and target:
        parts.append(ToolDisplayPart(target, kind="identifier", truncate="middle"))
    return tuple(parts)


def _render_subagent_prompt_block(context: Any, coordinator: SubAgentCoordinator) -> str:
    targets = coordinator.prompt_targets(context.agent, context.agent_project_id)
    rendered_targets = _format_subagent_targets(targets)
    execution_guidance = (
        NESTED_EXECUTION_GUIDANCE
        if getattr(context, "nesting_depth", 0) > 0
        else TOP_LEVEL_EXECUTION_GUIDANCE
    )
    return SUBAGENT_PROMPT_BLOCK_TEMPLATE.replace("{subagent_list}", rendered_targets).replace(
        "{execution_guidance}", execution_guidance
    )


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
    "SUBAGENT_PROMPT_BLOCK_TEMPLATE",
    "SUBAGENT_TOOL_DESCRIPTION",
    "SUBAGENT_TOOL_NAME",
    "SUBAGENT_TOOL_PARAMETERS",
    "register_subagent_tools",
]
