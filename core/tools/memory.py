"""Built-in memory tool for pinned USER.md and MEMORY.md entries."""

from __future__ import annotations

import asyncio
import threading
from collections import OrderedDict

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
    "List or edit pinned memory by setting action to list, add, replace, or remove. "
    "USER.md "
    "('user' scope — who the user is: preferences, "
    "role, communication style) and MEMORY.md ('agent' scope — your own environment, "
    "conventions, and tool quirks). Entries are injected into every future turn, so keep "
    "them compact and high-signal.\n\n"
    "WHEN: save proactively when the user states a preference, correction, or personal "
    "detail, or you learn a stable fact about their environment or workflow. Priority order "
    "when you save: user preferences and corrections first, then environment facts, then "
    "reusable procedures.\n\n"
    "SKIP: trivial or easily re-discovered facts, raw data, task progress, completed-work "
    "logs, and temporary TODO state (recall those from past sessions with session_search, if "
    "that tool is available). Also skip anything stale within a week: PR/issue numbers, "
    "commit hashes, 'fixed bug X', 'phase N done', file counts. A reusable workflow belongs "
    "in a skill rather than memory (capture it with the skill_manage tool, if you have it).\n\n"
    "IF FULL: an add is rejected once a scope is at its budget. Call list, then "
    "remove or shorten stale entries to make room, and re-add.\n\n"
    "For replace/remove, call list first — 1-based ids shift after a remove."
)
MEMORY_ACTIONS = ("list", "add", "replace", "remove")
MEMORY_SCOPES = ("user", "agent")
_MEMORY_SCOPE_PARAMETER: JsonObject = {
    "type": "string",
    "enum": list(MEMORY_SCOPES),
    "description": "Pinned memory file: user=USER.md, agent=MEMORY.md.",
}
_MEMORY_CONTENT_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": "Concise, durable entry content. Required for add and replace.",
}
_MEMORY_ENTRY_ID_PARAMETER: JsonObject = {
    "type": "integer",
    "minimum": 1,
    "description": "1-based entry id returned by list. Required for replace and remove.",
}

MEMORY_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "description": (
        "Flat action interface. The handler validates the fields required and allowed by "
        "the selected action."
    ),
    "properties": {
        "action": {
            "type": "string",
            "enum": list(MEMORY_ACTIONS),
            "description": "List entries, add one, replace one, or remove one.",
        },
        "scope": _MEMORY_SCOPE_PARAMETER,
        "content": _MEMORY_CONTENT_PARAMETER,
        "entry_id": _MEMORY_ENTRY_ID_PARAMETER,
    },
    "required": ["action", "scope"],
    "additionalProperties": False,
}

_MEMORY_ACTION_FIELDS = {
    "list": frozenset({"action", "scope"}),
    "add": frozenset({"action", "scope", "content"}),
    "replace": frozenset({"action", "scope", "entry_id", "content"}),
    "remove": frozenset({"action", "scope", "entry_id"}),
}

# Actions that mutate a scope. Only these feed the thrash guard: a failed mutation
# invites the model to consolidate and retry, so a model that keeps failing them is
# the loop we cap; list is a pure read and never counts.
_MEMORY_MUTATION_ACTIONS = ("add", "replace", "remove")
# Consecutive failed mutations tolerated per run before the tool cuts the loop off.
# Recovery from a full scope is normally a single failure (add rejected → remove
# succeeds → re-add), so a legitimate flow never approaches this; the cap only bites
# a model that keeps re-issuing failing writes and would otherwise loop the turn to
# budget exhaustion, re-sending the whole context each round and starving the reply.
_MAX_MEMORY_FAILURES_PER_RUN = 3


class _MemoryThrashTracker:
    """Per-run counter of consecutive failed memory mutations (thrash guard).

    Keyed by run id and reset on the first successful mutation of that run. Bounded
    so a long-lived process never accumulates state: a success drops the run's entry,
    and the map evicts oldest-first past a fixed cap. Mutation handlers run in worker
    threads and calls within one turn run concurrently, so the counter is guarded by a
    lock — a lost update would only miscount by one, but the lock keeps it exact.
    """

    _MAX_TRACKED_RUNS = 512

    def __init__(self) -> None:
        self._counts: OrderedDict[str, int] = OrderedDict()
        self._lock = threading.Lock()

    def record_failure(self, run_id: str) -> int:
        """Increment and return the run's consecutive-failure count."""
        with self._lock:
            count = self._counts.get(run_id, 0) + 1
            self._counts[run_id] = count
            self._counts.move_to_end(run_id)
            while len(self._counts) > self._MAX_TRACKED_RUNS:
                self._counts.popitem(last=False)
            return count

    def record_success(self, run_id: str) -> None:
        """Reset the run's failure count after a successful mutation."""
        with self._lock:
            self._counts.pop(run_id, None)


def make_memory_handler(memory_service: MemoryService):
    """Create a memory tool handler bound to a memory service."""

    tracker = _MemoryThrashTracker()

    async def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return await asyncio.to_thread(memory_handler, context, arguments, memory_service, tracker)

    return handler


def memory_handler(
    context: ToolContext,
    arguments: JsonObject,
    memory_service: MemoryService,
    tracker: _MemoryThrashTracker | None = None,
) -> JsonObject:
    """Handle a memory tool call and return a stable vBot result envelope.

    ``tracker`` is the per-run thrash guard the runtime handler supplies; when it is
    absent (direct callers, tests) the guard is simply inert and behavior is unchanged.
    """
    action = arguments.get("action")
    if not isinstance(action, str) or action not in MEMORY_ACTIONS:
        return tool_failure(
            "invalid_arguments",
            f"action must be one of: {', '.join(MEMORY_ACTIONS)}",
        )

    unknown_arguments = set(arguments) - _MEMORY_ACTION_FIELDS[action]
    if unknown_arguments:
        names = ", ".join(sorted(unknown_arguments))
        return tool_failure("invalid_arguments", f"Unknown {action} argument(s): {names}")

    try:
        scope = _required_enum(
            arguments.get("scope"),
            field_name="scope",
            values=MEMORY_SCOPES,
        )
    except ValueError as error:
        return tool_failure("invalid_arguments", str(error))

    is_mutation = action in _MEMORY_MUTATION_ACTIONS
    try:
        data = _dispatch_memory_action(
            context,
            arguments,
            memory_service,
            action,
            scope,
        )
    except MemoryError as error:
        if is_mutation:
            return _mutation_failure(tracker, context.run_id, error)
        return tool_failure("memory_error", str(error))
    except ValueError as error:
        return tool_failure("invalid_arguments", str(error))

    if is_mutation and tracker is not None:
        tracker.record_success(context.run_id)
    return tool_success(data)


def _mutation_failure(
    tracker: _MemoryThrashTracker | None, run_id: str, error: MemoryError
) -> JsonObject:
    """Return the failure envelope for a rejected mutation, applying the thrash guard.

    Below the per-run cap the model gets the underlying recoverable error and a
    ``retryable`` signal so it can consolidate and try again. At the cap the message
    flips terminal (``retryable`` false): a memory side effect must never loop the
    turn and suppress the user's reply — the fact can be saved in a later turn.
    """
    if tracker is None:
        return tool_failure("memory_error", str(error), retryable=True)
    failures = tracker.record_failure(run_id)
    if failures > _MAX_MEMORY_FAILURES_PER_RUN:
        return tool_failure(
            "memory_error",
            f"Memory update failed {failures} times this run. Stop retrying memory calls — "
            "leave memory unchanged for now and continue with your reply to the user. The "
            "fact can be saved in a later turn.",
            retryable=False,
            attempts_made=failures,
        )
    return tool_failure("memory_error", str(error), retryable=True)


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
    return required_int(value, field_name="entry_id", minimum=1)


def _required_content(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("content must be a non-empty string")
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
        result_schema={"type": "object", "required": ["content", "scope", "entries"]},
        display=ToolDisplay(
            summary_builder=_memory_display_summary,
            hidden_argument_keys=("content",),
        ),
    )


def _memory_display_summary(arguments: JsonObject) -> str:
    action = arguments.get("action")
    if not isinstance(action, str) or action not in MEMORY_ACTIONS:
        return ""
    parts = [action]
    scope = arguments.get("scope")
    if isinstance(scope, str) and scope:
        parts.append(scope)
    entry_id = arguments.get("entry_id")
    if isinstance(entry_id, int) and not isinstance(entry_id, bool):
        parts.append(str(entry_id))
    return " · ".join(parts)


__all__ = [
    "MEMORY_TOOL_DESCRIPTION",
    "MEMORY_TOOL_NAME",
    "MEMORY_TOOL_PARAMETERS",
    "make_memory_handler",
    "memory_handler",
    "register_memory_tool",
]
