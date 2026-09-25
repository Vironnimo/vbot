"""Tool registration for Sub-Agent work."""

from __future__ import annotations

from functools import cache
from typing import Any

from core.settings import ALLOWED_THINKING_EFFORTS
from core.subagents import SubAgentCoordinator, SubAgentPromptTarget
from core.tools._subagent_arguments import (
    UNADVERTISED_PARAMETERS,
    normalize_subagent_arguments,
)
from core.tools.contracts import ToolContract, compile_tool_contract
from core.tools.tools import (
    JsonObject,
    ToolDisplay,
    ToolDisplayPart,
    ToolPromptBlockRegistry,
    ToolRegistry,
)

SUBAGENT_TOOL_NAME = "subagent"

SUBAGENT_TOOL_DESCRIPTION = (
    "Delegate a task to a Sub-Agent: a copy of yourself, or an Agent listed under Sub-Agents "
    "in the System Prompt. It works in its own Session, and its final answer comes back to "
    "you."
)

SUBAGENT_PROMPT_BLOCK_TEMPLATE = (
    "## Sub-Agents\n\n"
    "{target_choices}\n\n"
    "Delegate bounded work that another Agent can finish independently. Start independent "
    "Sub-Agents with sibling calls in the same turn so they run concurrently, and give "
    "Sub-Agents that edit files non-overlapping ownership. You remain responsible for "
    "integrating and verifying their results.\n\n"
    "{execution_guidance}"
)

TARGET_CHOICES_TEMPLATE = (
    "In `subagent`, omit `agent_id` to delegate to a copy of yourself, or use one of these "
    "Agent ids exactly:\n\n{subagent_list}"
)
NO_ADDITIONAL_SUBAGENTS_TEXT = (
    "In `subagent`, omit `agent_id` to delegate to a copy of yourself; no other Agents are "
    "available."
)
TOP_LEVEL_EXECUTION_GUIDANCE = (
    "Each `run` starts in the background and returns a work id at once. vBot delivers each "
    "Sub-Agent's final answer to you automatically: during your current turn if you are "
    "still working, otherwise in a new turn right after yours ends. Continue other work, or "
    "end your turn to wait; do not poll `status` for completion."
)
NESTED_EXECUTION_GUIDANCE_TEMPLATE = (
    "You are a Sub-Agent, so each `run` waits and returns the Sub-Agent's final answer. vBot "
    "cancels work that takes longer than {timeout}, including time spent waiting for a busy "
    "Session, and returns a timeout failure. Sibling calls issued together still run "
    "concurrently."
)

SUBAGENT_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["run", "status", "cancel"],
            "description": (
                "run (default) delegates content; status reports on delegated work; cancel "
                "stops it."
            ),
        },
        "content": {
            "type": "string",
            "description": (
                "The task: goal, relevant context, scope, constraints and expected result. "
                "Self-contained unless it continues a Session. Required for run."
            ),
        },
        "description": {
            "type": "string",
            "description": "Short title for the work. Omit to use the start of content.",
        },
        "agent_id": {
            "type": "string",
            "description": (
                "Agent to delegate to, exactly as listed under Sub-Agents. Omit for a copy of "
                "yourself."
            ),
        },
        "session_id": {
            "type": "string",
            "description": (
                "Continues an earlier Sub-Agent Session: the session_id from its subagent "
                "result, sent with that result's agent_id. Omit to start a new Session."
            ),
        },
        "model": {
            "type": "string",
            "description": (
                "Model for this run only, as <provider>/<model-id>. Omit to use the Agent's model."
            ),
        },
        "thinking_effort": {
            "type": "string",
            "enum": sorted(e for e in ALLOWED_THINKING_EFFORTS if e),
            "description": "Thinking effort for this run only. Omit to use the Agent's setting.",
        },
        "id": {
            "type": "string",
            "description": (
                "Work id from a run result. Required for cancel; omit with status to list all "
                "tracked work."
            ),
        },
    },
    "required": [],
}


@cache
def _repair_contract() -> ToolContract:
    return compile_tool_contract(
        name=SUBAGENT_TOOL_NAME,
        input_schema=SUBAGENT_TOOL_PARAMETERS,
        require_closed_input=False,
    )


def _normalize_subagent_arguments(arguments: Any) -> Any:
    return normalize_subagent_arguments(_repair_contract(), arguments)


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
        execution_slot_required=False,
        open_input_schema=True,
        argument_normalizer=_normalize_subagent_arguments,
        unadvertised_parameters=UNADVERTISED_PARAMETERS,
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


def _subagent_display_parts(raw_arguments: JsonObject) -> tuple[ToolDisplayPart, ...]:
    # Persisted calls keep the Model's own spelling; label what the call meant.
    try:
        arguments = _normalize_subagent_arguments(raw_arguments)
    except ValueError:
        arguments = raw_arguments
    if not isinstance(arguments, dict):
        return ()
    action = arguments.get("action", "run")
    if action not in {"run", "status", "cancel"}:
        return ()
    agent_id = arguments.get("agent_id")
    if action == "run":
        parts: list[ToolDisplayPart] = []
        description = arguments.get("description")
        content = arguments.get("content")
        if isinstance(description, str) and description.strip():
            parts.append(ToolDisplayPart(description.strip(), kind="description"))
        elif isinstance(content, str) and content:
            parts.append(ToolDisplayPart(content))
        if isinstance(agent_id, str) and agent_id:
            parts.append(ToolDisplayPart(agent_id, kind="identifier", truncate="middle"))
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
    if getattr(context, "nesting_depth", 0) > 0:
        # Read per prompt build like the target catalog, so a changed setting
        # reaches the next request of every nested Agent.
        minutes = coordinator.foreground_timeout_minutes()
        timeout = f"{minutes} minute" if minutes == 1 else f"{minutes} minutes"
        execution_guidance = NESTED_EXECUTION_GUIDANCE_TEMPLATE.replace("{timeout}", timeout)
    else:
        execution_guidance = TOP_LEVEL_EXECUTION_GUIDANCE
    return SUBAGENT_PROMPT_BLOCK_TEMPLATE.replace("{target_choices}", rendered_targets).replace(
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
    return TARGET_CHOICES_TEMPLATE.replace("{subagent_list}", "\n".join(lines))


def _single_line(value: str) -> str:
    return " ".join(value.split())


__all__ = [
    "SUBAGENT_PROMPT_BLOCK_TEMPLATE",
    "SUBAGENT_TOOL_DESCRIPTION",
    "SUBAGENT_TOOL_NAME",
    "SUBAGENT_TOOL_PARAMETERS",
    "register_subagent_tools",
]
