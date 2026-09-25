"""Built-in memory tool for pinned USER.md and MEMORY.md entries."""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from core.memory import (
    MemoryBudgetError,
    MemoryError,
    MemoryMatchError,
    MemoryScope,
    MemoryService,
)
from core.tools._argument_repair import normalize_call_arguments
from core.tools.availability import MEMORY_TOOL_NAME
from core.tools.contracts import compile_tool_contract
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolDisplayPart,
    ToolRegistry,
    result_count_fact_builder,
    run_tool_worker,
    tool_failure,
    tool_success,
)

MEMORY_TOOL_DESCRIPTION = (
    "Manage pinned Memory: durable facts shown to you in future Sessions. Scope user holds "
    "facts about the user and their standing preferences; scope agent holds stable "
    "environment and project facts. Change or remove an entry by old_text, any unique part "
    "of its current text; a failed match returns the current entries."
)
MEMORY_ACTIONS = ("list", "add", "replace", "remove")
MEMORY_SCOPES = ("user", "agent")
# Both scopes in Memory block order.
_ALL_SCOPES: tuple[MemoryScope, ...] = ("agent", "user")
_MEMORY_SCOPE_PARAMETER: JsonObject = {
    "type": "string",
    "enum": list(MEMORY_SCOPES),
    "description": (
        "Required for add. Omit for list to show both scopes, or for replace and remove "
        "to search both."
    ),
}
_MEMORY_CONTENT_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": "One concise declarative fact: the new entry for add, the new text for replace.",
}
_MEMORY_OLD_TEXT_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": "For replace and remove: a unique part of the entry's current text.",
}

MEMORY_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(MEMORY_ACTIONS),
            "description": "list shows current entries; add, replace, or remove changes one.",
        },
        "scope": _MEMORY_SCOPE_PARAMETER,
        "content": _MEMORY_CONTENT_PARAMETER,
        "old_text": _MEMORY_OLD_TEXT_PARAMETER,
    },
    "required": ["action"],
}

# Positional ids from results before entries were addressed by text. Accepted so an
# id copied from older conversation history gets a precise refusal naming the entry
# at that position instead of a generic unknown-parameter error.
_MEMORY_UNADVERTISED_PARAMETERS: JsonObject = {"entry_id": {"type": "integer", "minimum": 1}}

_MEMORY_FIELD_ALIASES = {
    "target": "scope",
    "new_text": "content",
    "new_content": "content",
    "new_string": "content",
    "text": "content",
    "old_string": "old_text",
    "old_content": "old_text",
    "old": "old_text",
    "match": "old_text",
    "find": "old_text",
    "id": "entry_id",
    "index": "entry_id",
}
_ACTION_SYNONYMS: dict[str, tuple[str, ...]] = {
    "add": ("add", "create", "append", "insert", "save", "store", "remember", "write"),
    "replace": ("replace", "update", "edit", "modify", "change"),
    "remove": ("remove", "delete", "forget", "drop", "erase"),
    "list": ("list", "read", "show", "view", "get"),
}
_SCOPE_SYNONYMS: dict[str, tuple[str, ...]] = {
    "user": ("user", "profile", "userprofile", "usermd"),
    "agent": ("agent", "memory", "agentmemory", "memorymd"),
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
_PREVIEW_CHARS = 200


class _MemoryThrashTracker:
    """Per-run counter of consecutive failed memory mutations (thrash guard).

    Keyed by run id and reset on the first successful mutation of that run. Bounded
    so a long-lived process never accumulates state: a success drops the run's entry,
    and the map evicts oldest-first past a fixed cap. Mutation handlers run in worker
    threads, so the counter is guarded by a lock.
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


class _MemoryRefusalError(Exception):
    """A call whose intended change is unclear; nothing was changed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def make_memory_handler(memory_service: MemoryService):
    """Create a memory tool handler bound to a memory service."""

    tracker = _MemoryThrashTracker()

    async def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return await run_tool_worker(memory_handler, context, arguments, memory_service, tracker)

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
    scope = arguments.get("scope")
    if scope is not None and scope not in MEMORY_SCOPES:
        return tool_failure("invalid_arguments", "scope must be user or agent")
    workspace = Path(context.workspace)
    if action == "list":
        try:
            return tool_success(_list_result(memory_service, workspace, scope))
        except MemoryError as error:
            return tool_failure("memory_error", str(error))

    try:
        data = _mutate(memory_service, workspace, action, scope, arguments)
    except _MemoryRefusalError as refusal:
        if refusal.code == "invalid_arguments":
            return tool_failure(refusal.code, str(refusal))
        return _mutation_failure(tracker, context.run_id, refusal.code, str(refusal))
    except MemoryBudgetError as error:
        return _mutation_failure(
            tracker, context.run_id, "memory_full", _full_message(memory_service, workspace, error)
        )
    except MemoryMatchError as error:
        code = "memory_ambiguous" if error.matches else "memory_no_match"
        return _mutation_failure(
            tracker, context.run_id, code, _match_message(memory_service, workspace, error)
        )
    except MemoryError as error:
        return _mutation_failure(tracker, context.run_id, "memory_error", str(error))

    if tracker is not None:
        tracker.record_success(context.run_id)
    return tool_success(data)


def _mutation_failure(
    tracker: _MemoryThrashTracker | None, run_id: str, code: str, message: str
) -> JsonObject:
    """Return the failure envelope for a rejected mutation, applying the thrash guard.

    Below the per-run cap the model gets the recoverable error with what to change.
    At the cap the message flips terminal: a memory side effect must never loop the
    turn and suppress the user's reply — the fact can be saved in a later turn.
    """
    if tracker is None:
        return tool_failure(code, message)
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
    return tool_failure(code, message)


def _list_result(service: MemoryService, workspace: Path, scope: str | None) -> JsonObject:
    scopes = _scopes(scope)
    count = sum(len(service.list_entries(workspace, item)) for item in scopes)
    return {"count": count, "content": service.render_scopes(workspace, scopes)}


def _mutate(
    service: MemoryService,
    workspace: Path,
    action: str,
    scope: str | None,
    arguments: JsonObject,
) -> JsonObject:
    content = arguments.get("content")
    old_text = arguments.get("old_text")
    entry_id = arguments.get("entry_id")
    if action == "add":
        return _add(service, workspace, scope, content, old_text, entry_id)
    if old_text is None and entry_id is None and action == "remove" and content is not None:
        # The entry text itself, sent in the only text field the Agent had for it.
        old_text, content = content, None
    if entry_id is not None:
        old_text = _checked_entry_id(service, workspace, action, scope, entry_id, old_text, content)
    if old_text is None:
        raise _MemoryRefusalError(
            "invalid_arguments",
            f"{action} needs old_text: a unique part of the entry's current text. Nothing "
            f"changed. Current entries:\n\n{service.render_scopes(workspace, _scopes(scope))}",
        )
    if action == "remove" and content is not None:
        raise _MemoryRefusalError(
            "invalid_arguments",
            "remove deletes the entry and takes no content. To change the entry's text "
            f"instead, call memory with {_call('replace', scope, old_text, content)}. Nothing "
            "changed.",
        )
    if action == "replace" and content is None:
        raise _MemoryRefusalError(
            "invalid_arguments",
            "replace needs content: the entry's new text. To delete the entry, call memory "
            f"with {_call('remove', scope, old_text)}. Nothing changed.",
        )
    target = _memory_scope(scope) if scope is not None else _scope_for(service, workspace, old_text)
    if action == "replace":
        change = service.replace_matching(workspace, target, old_text, str(content))
        if change.current == change.previous:
            return {
                "content": f"The {target} Memory entry already reads that way; nothing changed."
            }
        verb = "Replaced in"
    else:
        change = service.remove_matching(workspace, target, old_text)
        verb = "Removed from"
    used, budget = service.scope_usage(workspace, target)
    return {
        "content": (
            f"{verb} {target} Memory ({used}/{budget} chars used). Previous text: "
            f'"{_preview(change.previous)}"'
        )
    }


def _add(
    service: MemoryService,
    workspace: Path,
    scope: str | None,
    content: Any,
    old_text: Any,
    entry_id: Any,
) -> JsonObject:
    if old_text is not None or entry_id is not None:
        located = f'the entry containing "{_preview(str(old_text))}"' if old_text else "an entry"
        raise _MemoryRefusalError(
            "invalid_arguments",
            f"add saves a new entry and does not address {located}. To add a new entry, "
            "omit old_text and entry_id; to change an existing entry, use action replace "
            "with old_text set to a unique part of its text. Nothing changed.",
        )
    if scope is None:
        raise _MemoryRefusalError(
            "invalid_arguments",
            'add needs scope: "user" for facts about the user and their standing preferences, '
            '"agent" for stable environment and project facts. Nothing was saved.',
        )
    if not isinstance(content, str) or not content.strip():
        raise _MemoryRefusalError("invalid_arguments", "add needs content: the fact to save.")
    target = _memory_scope(scope)
    existing = {entry.content for entry in service.list_entries(workspace, target)}
    entry = service.add_entry(workspace, target, content)
    if entry.content in existing:
        return {"content": f"{target.capitalize()} Memory already has this entry; nothing changed."}
    used, budget = service.scope_usage(workspace, target)
    return {"content": f"Added to {target} Memory ({used}/{budget} chars used)."}


def _checked_entry_id(
    service: MemoryService,
    workspace: Path,
    action: str,
    scope: str | None,
    entry_id: Any,
    old_text: Any,
    content: Any,
) -> str:
    """Refuse a positional id unless old_text already names the same entry."""
    if scope is None:
        raise _MemoryRefusalError(
            "invalid_arguments",
            "Memory entries are addressed by text, not entry_id: positions change as entries "
            f"change. Call memory with action {action} and old_text set to a unique part of the "
            f"entry. Nothing changed. Current entries:\n\n"
            f"{service.render_scopes(workspace, _ALL_SCOPES)}",
        )
    target = _memory_scope(scope)
    entries = [entry.content for entry in service.list_entries(workspace, target)]
    if not isinstance(entry_id, int) or not 1 <= entry_id <= len(entries):
        raise _MemoryRefusalError(
            "invalid_arguments",
            f"{target.capitalize()} Memory has {len(entries)} entries, so entry {entry_id} does "
            "not exist. Entries are addressed by old_text, a unique part of their text. "
            f"Nothing changed. Current entries:\n\n{service.render_scopes(workspace, [target])}",
        )
    text = entries[entry_id - 1]
    if isinstance(old_text, str) and service.find_matches(workspace, target, old_text) == [text]:
        return old_text
    replacement = content if action == "replace" and isinstance(content, str) else None
    raise _MemoryRefusalError(
        "invalid_arguments",
        "Memory entries are addressed by text, not entry_id: positions change as entries "
        f'change. Entry {entry_id} of {target} Memory currently reads "{_preview(text)}". If '
        f"that is the entry you mean, call memory with "
        f"{_call(action, target, text, replacement)}. Nothing changed.",
    )


def _scope_for(service: MemoryService, workspace: Path, old_text: str) -> MemoryScope:
    """Find the one scope whose entries old_text identifies, or refuse to guess."""
    found = {scope: service.find_matches(workspace, scope, old_text) for scope in _ALL_SCOPES}
    hits = [scope for scope, matches in found.items() if matches]
    if len(hits) == 1:
        return _memory_scope(hits[0])
    if not hits:
        raise _MemoryRefusalError(
            "memory_no_match",
            f'No Memory entry contains old_text "{_preview(old_text)}"; nothing changed. Copy '
            "old_text from a current entry:\n\n"
            f"{service.render_scopes(workspace, _ALL_SCOPES)}",
        )
    listing = "\n".join(
        f"{scope}: - {_preview(entry)}" for scope in _ALL_SCOPES for entry in found[scope]
    )
    raise _MemoryRefusalError(
        "memory_ambiguous",
        f'old_text "{_preview(old_text)}" matches entries in both user and agent Memory; '
        f"nothing changed. Set scope to the one you mean:\n{listing}",
    )


def _match_message(service: MemoryService, workspace: Path, error: MemoryMatchError) -> str:
    if error.matches:
        listing = "\n".join(f"- {entry}" for entry in error.matches)
        return (
            f'old_text "{_preview(error.old_text)}" matches {len(error.matches)} {error.scope} '
            "Memory entries; nothing changed. Use a part that appears in only one of them:\n"
            f"{listing}"
        )
    return (
        f'No {error.scope} Memory entry contains old_text "{_preview(error.old_text)}"; '
        "nothing changed. Copy old_text from a current entry:\n\n"
        f"{service.render_scopes(workspace, [error.scope])}"
    )


def _full_message(service: MemoryService, workspace: Path, error: MemoryBudgetError) -> str:
    return (
        f"{error.scope.capitalize()} Memory would hold {error.total}/{error.budget} characters; "
        f"free at least {error.total - error.budget} characters first. Nothing changed. "
        "Shorten entries with replace or delete them with remove, then retry; calls in one "
        f"message run in order. Current entries:\n\n"
        f"{service.render_scopes(workspace, [error.scope])}"
    )


def _call(action: str, scope: str | None, old_text: str, content: str | None = None) -> str:
    call: JsonObject = {"action": action}
    if scope is not None:
        call["scope"] = scope
    call["old_text"] = old_text
    if content is not None:
        call["content"] = content
    return json.dumps(call, ensure_ascii=False)


def _preview(text: str) -> str:
    compact = " ".join(text.split())
    if len(compact) <= _PREVIEW_CHARS:
        return compact
    return f"{compact[: _PREVIEW_CHARS - 3]}..."


def _scopes(scope: str | None) -> list[MemoryScope]:
    return [_memory_scope(scope)] if scope is not None else list(_ALL_SCOPES)


def _memory_scope(scope: str) -> MemoryScope:
    if scope == "user":
        return "user"
    if scope == "agent":
        return "agent"
    raise ValueError(f"scope must be one of: {', '.join(MEMORY_SCOPES)}")


def _synonym(value: Any, table: dict[str, tuple[str, ...]]) -> Any:
    if not isinstance(value, str):
        return value
    spelling = "".join(character for character in value.casefold() if character.isalnum())
    for canonical, spellings in table.items():
        if spelling in spellings:
            return canonical
    return value


def _normalize_memory_arguments(contract: Any, arguments: Any) -> Any:
    return normalize_call_arguments(
        contract,
        arguments,
        enum_fields=("action", "scope"),
        field_aliases=_MEMORY_FIELD_ALIASES,
        field_normalizers={
            "action": lambda value: _synonym(value, _ACTION_SYNONYMS),
            "scope": lambda value: _synonym(value, _SCOPE_SYNONYMS),
        },
        empty_as_omitted=("scope", "content", "old_text", "entry_id"),
    )


def register_memory_tool(registry: ToolRegistry, memory_service: MemoryService) -> None:
    """Register the memory tool with a vBot tool registry."""
    runtime_contract = compile_tool_contract(
        name="memory",
        input_schema={
            **MEMORY_TOOL_PARAMETERS,
            "properties": {
                **MEMORY_TOOL_PARAMETERS["properties"],
                **_MEMORY_UNADVERTISED_PARAMETERS,
            },
        },
        require_closed_input=False,
    )
    registry.register(
        MEMORY_TOOL_NAME,
        MEMORY_TOOL_DESCRIPTION,
        MEMORY_TOOL_PARAMETERS,
        make_memory_handler(memory_service),
        activation="memory_mode",
        constraints=("identity_agent",),
        open_input_schema=True,
        unadvertised_parameters=_MEMORY_UNADVERTISED_PARAMETERS,
        argument_normalizer=lambda arguments: _normalize_memory_arguments(
            runtime_contract, arguments
        ),
        # Same-turn Memory calls apply in the order written, so "free space, then add"
        # works within one message and text addressing never races a sibling change.
        parallel_safe=False,
        result_schema={"type": "object", "required": ["content"]},
        display=ToolDisplay(
            parts_builder=_memory_display_parts,
            fact_builder=result_count_fact_builder("count", when_arguments={"action": "list"}),
            hidden_argument_keys=("content", "old_text"),
        ),
    )


def _memory_display_parts(arguments: JsonObject) -> tuple[ToolDisplayPart, ...]:
    action = arguments.get("action")
    if not isinstance(action, str) or action not in MEMORY_ACTIONS:
        return ()
    parts = [ToolDisplayPart(action, truncate="never", tooltip="none")]
    scope = arguments.get("scope")
    if isinstance(scope, str) and scope:
        parts.append(ToolDisplayPart(scope))
    return tuple(parts)


__all__ = [
    "MEMORY_TOOL_DESCRIPTION",
    "MEMORY_TOOL_NAME",
    "MEMORY_TOOL_PARAMETERS",
    "make_memory_handler",
    "memory_handler",
    "register_memory_tool",
]
