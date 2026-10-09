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
from core.tools.availability import MESSAGE_PARENT_TOOL_NAME, TOOL_ACTIVATION_SESSION_GRANT
from core.tools.contracts import ToolContract, compile_tool_contract
from core.tools.tools import (
    JsonObject,
    ToolDisplay,
    ToolDisplayPart,
    ToolPromptBlockRegistry,
    ToolRegistry,
    display_notice,
    display_results,
    display_text,
)

SUBAGENT_TOOL_NAME = "subagent"

SUBAGENT_TOOL_DESCRIPTION = (
    "Delegate tasks to Sub-Agents and manage them: run starts a Sub-Agent, send sends one a "
    "message, list shows them, cancel stops one. A Sub-Agent is a copy of yourself or an Agent "
    "listed under Sub-Agents in the System Prompt; it works in its own Session, and vBot "
    "delivers its answers to you."
)

SUBAGENT_PROMPT_BLOCK_TEMPLATE = (
    "## Sub-Agents\n\n"
    "{target_choices}\n\n"
    "When the user asks for an Agent that is not among these choices, tell the user that this "
    "Agent is not available to you, then delegate to a copy of yourself or ask the user. Call "
    "a copy your Sub-Agent, never by the name of the Agent the user asked for.\n\n"
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
EXECUTION_GUIDANCE = (
    "Each `run` starts a Sub-Agent in the background and returns its id at once. vBot "
    "delivers every answer of a Sub-Agent to you automatically: during your current turn if "
    "you are still working, otherwise in a new turn right after yours ends. An answer from a "
    "Sub-Agent that is not finished is a status report, and vBot ends each answer with a line "
    "naming what of that Sub-Agent is still running. Continue other work, or end your turn to "
    "wait; do not poll `list` for completion. To correct or extend a Sub-Agent's work, use "
    "`send` instead of starting a new Sub-Agent."
)
# Names the target list when vBot tells the Model that it changed mid-Session.
SUBAGENT_CATALOG_TITLE = "The Agent ids under Sub-Agents"

SUBAGENT_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["run", "send", "list", "cancel"],
            "description": (
                "run (default) starts a new Sub-Agent with content; send sends a message to one "
                "of your Sub-Agents; list shows your Sub-Agents; cancel stops one."
            ),
        },
        "content": {
            "type": "string",
            "description": (
                "For run, the task: goal, relevant context, scope, constraints and expected "
                "result, self-contained. For send, the message."
            ),
        },
        "description": {
            "type": "string",
            "description": (
                'For run, a 3-5 word title that the user sees, such as "Review auth module". '
                "Required for run."
            ),
        },
        "agent_id": {
            "type": "string",
            "description": (
                "For run, the Agent to delegate to, exactly as listed under Sub-Agents. Omit for "
                "a copy of yourself."
            ),
        },
        "id": {
            "type": "string",
            "description": (
                "Sub-Agent id from a run result or from list. Required for send and cancel."
            ),
        },
        "model": {
            "type": "string",
            "description": (
                "Model for the Sub-Agent, as <provider>/<model-id>. Omit to use the Agent's model."
            ),
        },
        "thinking_effort": {
            "type": "string",
            "enum": sorted(e for e in ALLOWED_THINKING_EFFORTS if e),
            "description": "Thinking effort for the Sub-Agent. Omit to use the Agent's setting.",
        },
    },
    "required": [],
}

MESSAGE_PARENT_TOOL_DESCRIPTION = (
    "Send a message to your Parent Agent, the Agent that delegated your task, without ending "
    "your turn. Use it for questions only your Parent Agent can decide and to answer its "
    "messages. Results and status reports belong in your final answer, which vBot sends to "
    "your Parent Agent."
)
MESSAGE_PARENT_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {"content": {"type": "string", "description": "The message."}},
    "required": ["content"],
}


@cache
def _repair_contract() -> ToolContract:
    # Unadvertised fields belong to the repair contract, so their spellings from
    # other harnesses map onto them too.
    properties = {**SUBAGENT_TOOL_PARAMETERS["properties"], **UNADVERTISED_PARAMETERS}
    return compile_tool_contract(
        name=SUBAGENT_TOOL_NAME,
        input_schema={**SUBAGENT_TOOL_PARAMETERS, "properties": properties},
        require_closed_input=False,
    )


def _normalize_subagent_arguments(arguments: Any) -> Any:
    return normalize_subagent_arguments(_repair_contract(), arguments)


def register_subagent_tools(
    registry: ToolRegistry,
    coordinator: SubAgentCoordinator,
    prompt_blocks: ToolPromptBlockRegistry | None = None,
) -> None:
    """Register the ``subagent`` Tool and the Sub-Agent-only ``message_parent`` Tool."""
    registry.register(
        SUBAGENT_TOOL_NAME,
        SUBAGENT_TOOL_DESCRIPTION,
        SUBAGENT_TOOL_PARAMETERS,
        coordinator.spawn,
        summary="Delegate tasks to Sub-Agents.",
        execution_slot_required=False,
        open_input_schema=True,
        argument_normalizer=_normalize_subagent_arguments,
        unadvertised_parameters=UNADVERTISED_PARAMETERS,
        result_schema={"type": "object"},
        display=ToolDisplay(
            parts_builder=_subagent_display_parts,
            detail_builder=_subagent_detail_blocks,
            hidden_argument_keys=("content",),
        ),
    )
    # Offered only in Sessions linked to a Parent Agent, through a Session grant.
    registry.register(
        MESSAGE_PARENT_TOOL_NAME,
        MESSAGE_PARENT_TOOL_DESCRIPTION,
        MESSAGE_PARENT_TOOL_PARAMETERS,
        coordinator.message_parent,
        open_input_schema=True,
        catalog_visible=False,
        session_scoped=True,
        activation=TOOL_ACTIVATION_SESSION_GRANT,
        execution_slot_required=False,
        result_schema={"type": "object"},
        display=ToolDisplay(detail_builder=_message_parent_detail_blocks),
    )
    if prompt_blocks is not None:
        prompt_blocks.register(
            SUBAGENT_TOOL_NAME,
            render=lambda context: _render_subagent_prompt_block(context, coordinator),
        )


def _message_parent_detail_blocks(
    raw_arguments: JsonObject, result: JsonObject | None
) -> list[JsonObject]:
    content = raw_arguments.get("content")
    return [display_text("content", text=content)] if isinstance(content, str) else []


# Final states the user should notice; a running Sub-Agent changes while the row
# is shown.
_SEND_NOTICES = {
    "steered": "The sub-agent receives the message at its next step.",
    "started": "The sub-agent started a new turn with the message.",
    "queued": "The message starts the sub-agent's next turn.",
    "cancelled": "The sub-agent was stopped.",
}


def _subagent_detail_blocks(
    raw_arguments: JsonObject, result: JsonObject | None
) -> list[JsonObject]:
    """Show the user the task or message, and the listed Sub-Agents.

    Ids, delivery facts, activity paths and notes are for the Agent and stay in
    the raw result. A Sub-Agent's answers appear in its own Session and in the
    Parent's conversation, not on the call row.
    """
    try:
        arguments = _normalize_subagent_arguments(raw_arguments)
    except ValueError:
        arguments = raw_arguments
    call = arguments if isinstance(arguments, dict) else {}
    blocks: list[JsonObject] = []
    action = call.get("action", "run")
    content = call.get("content")
    if action in {"run", "send"} and isinstance(content, str) and content.strip():
        blocks.append(display_text("task" if action == "run" else "content", text=content))
    data = result.get("data") if isinstance(result, dict) and result.get("ok") is True else None
    if not isinstance(data, dict):
        return blocks
    sent = _SEND_NOTICES.get(str(data.get("status")))
    if sent is not None:
        blocks.append(display_notice("info", sent))
    listed = data.get("subagents")
    if isinstance(listed, list):
        entries = [entry for entry in listed if isinstance(entry, dict)]
        if not entries:
            return [*blocks, display_notice("info", "No sub-agents.")]
        items = [
            {
                "title": entry.get("title") or entry.get("agent_id") or entry.get("id"),
                "meta": " · ".join(
                    str(part)
                    for part in (entry.get("agent_id"), entry.get("state"), entry.get("last_tool"))
                    if part
                ),
            }
            for entry in entries
        ]
        blocks.append(display_results(items))
    return blocks


def _subagent_display_parts(raw_arguments: JsonObject) -> tuple[ToolDisplayPart, ...]:
    # Persisted calls keep the Model's own spelling; label what the call meant.
    try:
        arguments = _normalize_subagent_arguments(raw_arguments)
    except ValueError:
        arguments = raw_arguments
    if not isinstance(arguments, dict):
        return ()
    action = arguments.get("action", "run")
    if action == "status":
        action = "list"
    if action not in {"run", "send", "list", "cancel"}:
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
    subagent_id = arguments.get("id")
    if isinstance(subagent_id, str) and subagent_id:
        parts.append(ToolDisplayPart(subagent_id, kind="identifier", truncate="middle"))
    return tuple(parts)


def _render_subagent_prompt_block(context: Any, coordinator: SubAgentCoordinator) -> Any:
    """Render the Sub-Agent guidance with its targets as a catalog, one entry per id."""
    from core.prompts import BlockCatalog, RenderedBlock

    targets = coordinator.prompt_targets(context.agent, context.agent_project_id)
    entries = tuple((target.agent_id, _subagent_target_line(target)) for target in targets)
    target_choices = (
        TARGET_CHOICES_TEMPLATE.replace("{subagent_list}", "\n".join(line for _, line in entries))
        if entries
        else NO_ADDITIONAL_SUBAGENTS_TEXT
    )
    return RenderedBlock(
        text=SUBAGENT_PROMPT_BLOCK_TEMPLATE.replace("{target_choices}", target_choices).replace(
            "{execution_guidance}", EXECUTION_GUIDANCE
        ),
        catalog=BlockCatalog(
            title=SUBAGENT_CATALOG_TITLE, entries=entries, frame=EXECUTION_GUIDANCE
        ),
    )


def _subagent_target_line(target: SubAgentPromptTarget) -> str:
    name = _single_line(target.name) or target.agent_id
    description = _single_line(target.description)
    suffix = f" — {name}"
    if description:
        suffix = f"{suffix} — {description}"
    return f"- `{target.agent_id}`{suffix}"


def _single_line(value: str) -> str:
    return " ".join(value.split())


__all__ = [
    "MESSAGE_PARENT_TOOL_DESCRIPTION",
    "SUBAGENT_PROMPT_BLOCK_TEMPLATE",
    "SUBAGENT_TOOL_DESCRIPTION",
    "SUBAGENT_TOOL_NAME",
    "SUBAGENT_TOOL_PARAMETERS",
    "register_subagent_tools",
]
