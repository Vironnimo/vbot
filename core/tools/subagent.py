"""Tool registration for Sub-Agent work."""

from __future__ import annotations

from functools import cache
from typing import Any

from core.settings import ALLOWED_THINKING_EFFORTS
from core.subagents import SubAgentCoordinator, SubAgentPromptTarget
from core.tools._argument_repair import normalize_call_arguments
from core.tools.contracts import compile_tool_contract
from core.tools.tools import (
    JsonObject,
    ToolDisplay,
    ToolDisplayPart,
    ToolPromptBlockRegistry,
    ToolRegistry,
)

SUBAGENT_TOOL_NAME = "subagent"

SUBAGENT_TOOL_DESCRIPTION = (
    "Delegate a task to a Sub-Agent, or inspect/cancel owned work. Top-level delegation "
    "returns immediately with automatic result delivery; delegation by a Sub-Agent "
    "waits for completion."
)

SUBAGENT_PROMPT_BLOCK_TEMPLATE = (
    "## Sub-Agents\n\n"
    "A Sub-Agent runs a delegated task in its own persisted Session, using your Agent "
    "configuration unless you select an additional Agent below. You remain "
    "responsible for deciding what to delegate, integrating the results, and "
    "verifying the final outcome.\n\n"
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
    "`status` is a non-blocking snapshot; `cancel` waits until that exact owned "
    "work is cancelled and cannot target another Parent Session's work."
)

NO_ADDITIONAL_SUBAGENTS_TEXT = "**No additional Agents are available.**"
TOP_LEVEL_EXECUTION_GUIDANCE = (
    "You are the top-level Agent. Every `run` action starts in the background and "
    "returns immediately; vBot monitors it and notifies you with the result once the "
    "Sub-Agent finishes. Continue other work, or finish your turn to wait for a result."
)
NESTED_EXECUTION_GUIDANCE_TEMPLATE = (
    "You are a Sub-Agent. Every `run` action executes in the foreground and the Tool "
    "Call returns when that work finishes, or after {timeout}, including any time spent "
    "queued; vBot then cancels the work and returns a timeout failure. Sibling calls "
    "issued together still run concurrently."
)

_SUBAGENT_ID_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Sub-Agent work id returned by run. Required for cancel; omit for run. "
        "Omit for status to inspect all Sub-Agent work still tracked for this Session."
    ),
}
_SUBAGENT_CONTENT_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Task or continuation message. Required for run; omit for status and cancel. "
        "Make it self-contained unless continuing session_id."
    ),
}
_SUBAGENT_DESCRIPTION_PARAMETER: JsonObject = {
    "type": "string",
    "description": ("Short title for run. Omit to use the beginning of content."),
}
_SUBAGENT_AGENT_ID_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Target Agent for run. Omit for a copy of yourself when starting a new Session; required "
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
    "enum": sorted(e for e in ALLOWED_THINKING_EFFORTS if e),
    "description": (
        "Thinking effort for run. Omit to inherit the target Agent. Applies only to this Run."
    ),
}
_SUBAGENT_SESSION_ID_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Sub-Agent Session id from an earlier subagent result to continue. Used by run "
        "only. Omit to start a new Session; requires agent_id."
    ),
}


SUBAGENT_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["run", "status", "cancel"],
            "description": (
                "run delegates content (default); status inspects owned work; cancel stops it."
            ),
        },
        "content": _SUBAGENT_CONTENT_PARAMETER,
        "description": _SUBAGENT_DESCRIPTION_PARAMETER,
        "agent_id": _SUBAGENT_AGENT_ID_PARAMETER,
        "session_id": _SUBAGENT_SESSION_ID_PARAMETER,
        "model": _SUBAGENT_MODEL_PARAMETER,
        "thinking_effort": _SUBAGENT_THINKING_PARAMETER,
        "id": _SUBAGENT_ID_PARAMETER,
    },
    "required": [],
}


@cache
def _repair_contract():
    return compile_tool_contract(
        name=SUBAGENT_TOOL_NAME,
        input_schema=SUBAGENT_TOOL_PARAMETERS,
        require_closed_input=False,
    )


def _normalize_subagent_arguments(arguments: Any) -> Any:
    return normalize_call_arguments(
        _repair_contract(),
        arguments,
        enum_fields=("action", "thinking_effort"),
        empty_as_omitted=("model", "thinking_effort"),
    )


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
