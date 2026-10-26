"""Project-scoped Session coordination through the canonical Chat owners."""

from __future__ import annotations

import hashlib
import json
from functools import cache
from typing import TYPE_CHECKING, Any

from core.chat.errors import ChatError
from core.chat.session_peers import SessionPeerError, SessionPeers
from core.runs import RunError
from core.tools.arguments import optional_int, optional_string, required_string
from core.tools.call_syntax import SpellingAliases, normalize_call_arguments
from core.tools.contracts import ToolContract, ToolContractError, compile_tool_contract
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolDisplayPart,
    ToolRegistry,
    display_results,
    display_text,
    tool_failure,
    tool_success,
)

if TYPE_CHECKING:
    from core.chat import ChatLoop

SESSION_TOOL_NAME = "session"
SESSION_TOOL_DESCRIPTION = (
    "Coordinate with other Sessions in your Working Project. "
    "Messages are visible to the user and wake idle recipients."
)
SESSION_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["list", "read", "send", "wait"],
            "description": (
                "read shows the task and recent conversation. send delivers at the next "
                "Model step without interrupting Tools; replies use send too. wait returns "
                "on new conversation, completion, or a message to you."
            ),
        },
        "id": {
            "type": "string",
            "description": (
                "Session reference from list or a received message. "
                "Required for read, send and wait."
            ),
        },
        "content": {"type": "string", "description": "Message text. Required for send."},
        "cursor": {
            "type": "string",
            "description": (
                "Returned continuation for list or read, or baseline cursor for wait. "
                "Omit for the first page, recent conversation, or changes from now."
            ),
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 20,
            "description": "For list or read. Omit for 10 Sessions or 8 messages.",
        },
        "timeout": {
            "type": "integer",
            "minimum": 0,
            "maximum": 60,
            "default": 60,
            "description": (
                "For wait, seconds to wait, up to 60; 0 checks immediately. Omit for 60 seconds."
            ),
        },
    },
    "required": ["action"],
}
_FIELDS = SpellingAliases(
    {
        "action": ("operation",),
        "id": ("session_id", "thread_id", "target", "recipient"),
        "content": ("message", "text", "prompt"),
        "cursor": ("after_cursor", "next_cursor", "older_cursor"),
        "timeout": ("timeout_seconds",),
        "limit": ("max_results",),
    }
)


@cache
def _contract() -> ToolContract:
    return compile_tool_contract(
        name=SESSION_TOOL_NAME,
        input_schema=SESSION_TOOL_PARAMETERS,
        require_closed_input=False,
    )


def _normalize(arguments: Any) -> Any:
    return normalize_call_arguments(
        _contract(),
        arguments,
        enum_fields=("action",),
        field_aliases=_FIELDS,
        placeholder_as_omitted=("id", "cursor", "limit", "timeout"),
        wrapping_fields=("arguments", "parameters"),
    )


def register_session_tool(registry: ToolRegistry, peers: SessionPeers, chat: ChatLoop) -> None:
    async def handle(context: ToolContext, arguments: JsonObject) -> JsonObject:
        action = required_string(arguments.get("action"), field_name="action")
        try:
            if action == "list":
                data = await peers.list(
                    context,
                    cursor=optional_string(arguments.get("cursor"), field_name="cursor"),
                    limit=_limit(arguments, 10),
                )
            else:
                target = required_string(arguments.get("id"), field_name=f"session {action} id")
                if action == "send":
                    content = required_string(
                        arguments.get("content"), field_name="session send content", strip=False
                    )
                    data = await _send(context, peers, chat, target, content)
                elif action == "read":
                    data = await peers.read(
                        context,
                        target,
                        cursor=optional_string(arguments.get("cursor"), field_name="cursor"),
                        limit=_limit(arguments, 8),
                    )
                elif action == "wait":
                    data = await peers.wait(
                        context,
                        target,
                        cursor=optional_string(arguments.get("cursor"), field_name="cursor"),
                        timeout=optional_int(
                            arguments.get("timeout"),
                            field_name="timeout",
                            default=60,
                            minimum=0,
                            maximum=60,
                        ),
                    )
                else:
                    raise ToolContractError(
                        f'Unknown action "{action}". Use list, read, send or wait.'
                    )
            return tool_success(data)
        except SessionPeerError as error:
            return tool_failure(error.code, str(error))

    registry.register(
        SESSION_TOOL_NAME,
        SESSION_TOOL_DESCRIPTION,
        SESSION_TOOL_PARAMETERS,
        handle,
        summary="Coordinate with Sessions in your Working Project.",
        execution_slot_required=False,
        open_input_schema=True,
        argument_normalizer=_normalize,
        result_schema={"type": "object"},
        display=ToolDisplay(
            parts_builder=_parts,
            detail_builder=_details,
            hidden_argument_keys=("id", "cursor", "content"),
        ),
    )


def _limit(arguments: JsonObject, default: int) -> int:
    return optional_int(
        arguments.get("limit"), field_name="limit", default=default, minimum=1, maximum=20
    )


async def _send(
    context: ToolContext, peers: SessionPeers, chat: ChatLoop, target: str, content: str
) -> JsonObject:
    if (
        context.permissions.restricted
        or context.tool_restriction is not None
        or context.tool_denial_resolver is not None
        or context.execution_owner is not None
    ):
        next_step = (
            "Use subagent to delegate work with your inherited limits."
            if context.can_call("subagent")
            else "Ask the user to send the request in the intended Session."
        )
        raise SessionPeerError(
            "permission_denied",
            "Nothing was sent. Your execution limits cannot be transferred to an independent "
            f"Session. {next_step}",
        )
    source = await peers.own(context)
    recipient = await peers.resolve(context, target)
    # Capture before admission: a fast recipient can answer before submit returns.
    baseline = await peers.read(context, target, limit=1)
    if context.is_cancelled():
        raise SessionPeerError("run_cancelled", "Nothing was sent. Your Run was cancelled.")
    # Providers can reuse call ids in later Assistant steps of the same Run.
    identity = json.dumps(
        [
            source.id,
            context.run_id,
            context.iteration_number,
            context.tool_call_index,
            context.tool_call_id,
        ],
        separators=(",", ":"),
    )
    payload = json.dumps([source.id, recipient.id, content], separators=(",", ":"))
    # This is an accepted-input idempotency key, not a new public Message id.
    input_id = "session:" + hashlib.sha256(identity.encode()).hexdigest()
    try:
        submission = await chat.submit_session_message(
            source=source.address,
            source_ref=source.id,
            expected_source_generation_id=source.generation_id,
            target=recipient.address,
            content=content,
            input_id=input_id,
            request_hash=hashlib.sha256(payload.encode()).hexdigest(),
            expected_generation_id=recipient.generation_id,
            expected_working_project_id=source.working_project_id,
        )
    except (ChatError, RunError) as error:
        raise SessionPeerError(
            "session_send_failed",
            f"Delivery was not confirmed. {error} Read the recipient Session before sending again.",
        ) from error
    disposition = "queued"
    if submission.run is not None:
        disposition = "started"
    elif submission.queued_item is not None and submission.queued_item.steering:
        disposition = "steered"
    return {
        "id": recipient.id,
        "agent_id": recipient.address.agent_id,
        "title": recipient.title,
        "status": "accepted",
        "delivery": disposition,
        "cursor": baseline["cursor"],
        "content": (
            "Message accepted for delivery. This does not confirm that the recipient has "
            "read or acted on it. Replies arrive as messages; continue other work, or use "
            "wait with this id and cursor."
        ),
    }


def _parts(arguments: JsonObject) -> list[ToolDisplayPart]:
    action = arguments.get("action")
    return [ToolDisplayPart(action)] if isinstance(action, str) and action else []


def _details(arguments: JsonObject, result: JsonObject | None) -> list[JsonObject]:
    blocks: list[JsonObject] = []
    content = arguments.get("content")
    if arguments.get("action") == "send" and isinstance(content, str) and content.strip():
        blocks.append(display_text("content", text=content))
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, dict):
        return blocks
    if isinstance(data.get("title"), str) and data["title"]:
        blocks.insert(0, display_results([{"title": data["title"], "meta": data.get("agent_id")}]))
    text = data.get("content")
    if isinstance(text, str) and text.strip():
        blocks.append(display_text("results", text=text))
    items = data.get("items")
    if isinstance(items, list):
        blocks.append(
            display_results(
                [
                    {
                        "title": item.get("title") or item.get("agent_id") or "Session",
                        "meta": item.get("activity"),
                        "text": item.get("progress"),
                    }
                    for item in items
                    if isinstance(item, dict)
                ]
            )
        )
    messages = data.get("messages")
    if isinstance(messages, list):
        task = data.get("task")
        if isinstance(task, dict) and isinstance(task.get("text"), str) and task["text"].strip():
            blocks.append(display_text("task", text=task["text"]))
        blocks.append(
            display_results(
                [
                    {
                        "title": (
                            "Session"
                            if message.get("sender_kind") == "session"
                            else str(message.get("role") or "Message")
                        ),
                        "text": message.get("text"),
                        "time": message.get("timestamp"),
                    }
                    for message in messages
                    if isinstance(message, dict)
                ]
            )
        )
    return blocks
