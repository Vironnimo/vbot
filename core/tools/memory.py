"""Built-in memory tool for pinned USER.md and MEMORY.md entries."""

from __future__ import annotations

import asyncio

from core.memory import MemoryEntry, MemoryError, MemoryScope, MemoryService
from core.tools.arguments import required_int
from core.tools.availability import MEMORY_TOOL_NAME
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolRegistry,
    tool_failure,
    tool_success,
)

MEMORY_TOOL_DESCRIPTION = (
    "List or edit pinned memory in USER.md ('user' scope: who the user is — preferences, "
    "role, style) and MEMORY.md ('agent' scope: your own environment, conventions, and "
    "tool quirks). Entries are injected into every future turn, so keep them compact.\n\n"
    "WHEN: save proactively when the user states a preference, correction, or personal "
    "detail, or you learn a stable fact about their environment or workflow. The best "
    "memory stops the user from having to repeat themselves.\n\n"
    "SKIP: trivial or easily re-discovered facts, raw data, task progress, completed-work "
    "logs, and anything stale within a week (PR numbers, commit hashes, 'phase N done').\n\n"
    "For replace/remove, call action='list' first — 1-based ids shift after a remove."
)
MEMORY_ACTIONS = ("list", "add", "replace", "remove")
MEMORY_SCOPES = ("user", "agent")
MEMORY_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(MEMORY_ACTIONS),
            "description": "Memory operation to perform.",
        },
        "scope": {
            "type": "string",
            "enum": list(MEMORY_SCOPES),
            "description": "Pinned memory file to operate on: user=USER.md, agent=MEMORY.md.",
        },
        "content": {
            "type": "string",
            "description": "Entry content for add/replace. Keep it concise and durable.",
        },
        "entry_id": {
            "type": "integer",
            "description": "1-based entry id for replace/remove.",
        },
    },
    "required": ["action", "scope"],
    "additionalProperties": False,
}

_ALLOWED_ARGUMENTS = set(MEMORY_TOOL_PARAMETERS["properties"])


def make_memory_handler(memory_service: MemoryService):
    """Create a memory tool handler bound to a memory service."""

    async def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return await asyncio.to_thread(memory_handler, context, arguments, memory_service)

    return handler


def memory_handler(
    context: ToolContext,
    arguments: JsonObject,
    memory_service: MemoryService,
) -> JsonObject:
    """Handle a memory tool call and return a stable vBot result envelope."""
    unknown_arguments = set(arguments) - _ALLOWED_ARGUMENTS
    if unknown_arguments:
        names = ", ".join(sorted(unknown_arguments))
        return tool_failure("invalid_arguments", f"Unknown argument(s): {names}")

    try:
        action = _required_enum(arguments.get("action"), field_name="action", values=MEMORY_ACTIONS)
        scope = _required_enum(arguments.get("scope"), field_name="scope", values=MEMORY_SCOPES)
        data = _dispatch_memory_action(context, arguments, memory_service, action, scope)
    except MemoryError as error:
        return tool_failure("memory_error", str(error))
    except ValueError as error:
        return tool_failure("invalid_arguments", str(error))

    return tool_success(data)


def _dispatch_memory_action(
    context: ToolContext,
    arguments: JsonObject,
    memory_service: MemoryService,
    action: str,
    scope: str,
) -> JsonObject:
    memory_scope = _memory_scope(scope)
    if action == "list":
        entries = memory_service.list_entries(context.workspace, memory_scope)
        return _entries_result(scope=memory_scope, entries=entries)
    if action == "add":
        entry = memory_service.add_entry(
            context.workspace,
            memory_scope,
            _required_content(arguments.get("content")),
        )
        entries = memory_service.list_entries(context.workspace, memory_scope)
        return _mutation_result("added", entry, entries)
    if action == "replace":
        entry = memory_service.replace_entry(
            context.workspace,
            memory_scope,
            _required_entry_id(arguments.get("entry_id")),
            _required_content(arguments.get("content")),
        )
        entries = memory_service.list_entries(context.workspace, memory_scope)
        return _mutation_result("replaced", entry, entries)
    if action == "remove":
        entry = memory_service.remove_entry(
            context.workspace,
            memory_scope,
            _required_entry_id(arguments.get("entry_id")),
        )
        entries = memory_service.list_entries(context.workspace, memory_scope)
        return _mutation_result("removed", entry, entries)
    raise ValueError(f"action must be one of: {', '.join(MEMORY_ACTIONS)}")


def _entries_result(*, scope: MemoryScope, entries: list[MemoryEntry]) -> JsonObject:
    return {
        "content": _render_entries(scope, entries),
        "scope": scope,
        "entries": [entry.to_dict() for entry in entries],
    }


def _mutation_result(action: str, entry: MemoryEntry, entries: list[MemoryEntry]) -> JsonObject:
    return {
        "content": f"Memory entry {entry.id} {action} in {entry.scope} scope.",
        "scope": entry.scope,
        "entry": entry.to_dict(),
        "entries": [item.to_dict() for item in entries],
    }


def _render_entries(scope: MemoryScope, entries: list[MemoryEntry]) -> str:
    if not entries:
        return f"No pinned memory entries recorded for {scope} scope."
    lines = [f"Pinned memory entries for {scope} scope:"]
    lines.extend(f"[{entry.id}] {entry.content}" for entry in entries)
    return "\n".join(lines)


def _required_enum(value: object, *, field_name: str, values: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in values:
        supported = ", ".join(values)
        raise ValueError(f"{field_name} must be one of: {supported}")
    return value


def _required_entry_id(value: object) -> int:
    return required_int(value, field_name="entry_id")


def _required_content(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("content must be a string")
    return value


def _memory_scope(scope: str) -> MemoryScope:
    if scope == "user":
        return "user"
    if scope == "agent":
        return "agent"
    raise ValueError(f"scope must be one of: {', '.join(MEMORY_SCOPES)}")


def register_memory_tool(registry: ToolRegistry, memory_service: MemoryService) -> None:
    """Register the memory tool with a vBot tool registry."""
    registry.register(
        MEMORY_TOOL_NAME,
        MEMORY_TOOL_DESCRIPTION,
        MEMORY_TOOL_PARAMETERS,
        make_memory_handler(memory_service),
        display=ToolDisplay(
            summary_fields=("action", "scope", "entry_id"),
            hidden_argument_keys=("content",),
        ),
    )


__all__ = [
    "MEMORY_TOOL_DESCRIPTION",
    "MEMORY_TOOL_NAME",
    "MEMORY_TOOL_PARAMETERS",
    "make_memory_handler",
    "memory_handler",
    "register_memory_tool",
]
