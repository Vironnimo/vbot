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
    MemoryWriter,
)
from core.projects import ProjectError, ProjectStore
from core.tools.availability import MEMORY_TOOL_NAME
from core.tools.call_syntax import normalize_call_arguments, spelling
from core.tools.contracts import compile_tool_contract
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolDisplayPart,
    ToolRegistry,
    offload_tool_handler,
    result_count_fact_builder,
    tool_failure,
    tool_success,
)

MEMORY_TOOL_DESCRIPTION = (
    "Manage Memory: facts shown to you in every future Session. Scope user holds facts "
    "about the user and preferences that apply to all of their work; scope agent holds "
    "stable environment facts; scope project holds lasting project decisions, user preferences "
    "and corrections shared with other Agents working in that Project. A lesson for one kind "
    "of task belongs in that "
    "task's Skill, not in Memory. Change or remove an entry by old_text, any unique part of "
    "its current text; a failed match returns the current entries."
)
MEMORY_ACTIONS = ("list", "add", "replace", "remove")
MEMORY_SCOPES = ("user", "agent", "project")
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
    "description": (
        "One concise declarative fact: the new entry for add, the complete replacement "
        "entry for replace."
    ),
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
        "project_id": {"type": "string"},
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
    "project": ("project", "projectmemory"),
}

# Consecutive failed mutations tolerated per run before the tool cuts the loop off.
# Recovery from a full scope is normally a single failure (add rejected → remove
# succeeds → re-add), so a legitimate flow never approaches this; the cap only bites
# a model that keeps re-issuing failing writes and would otherwise loop the turn to
# budget exhaustion, re-sending the whole context each round and starving the reply.
_MAX_MEMORY_FAILURES_PER_RUN = 3
_PREVIEW_CHARS = 200
# How the user sees each scope, the same headings the Memory block shows.
_NAMES = {"agent": "Agent Memory", "user": "User Profile", "project": "Project Memory"}


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


def make_memory_handler(memory_service: MemoryService, projects: ProjectStore | None = None):
    """Create a memory tool handler bound to a memory service."""

    tracker = _MemoryThrashTracker()

    def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return memory_handler(context, arguments, memory_service, tracker, projects)

    return offload_tool_handler(handler)


def memory_handler(
    context: ToolContext,
    arguments: JsonObject,
    memory_service: MemoryService,
    tracker: _MemoryThrashTracker | None = None,
    projects: ProjectStore | None = None,
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
        return tool_failure("invalid_arguments", "scope must be user, agent or project")
    if scope == "project":
        return _project_memory_result(context, arguments, memory_service, tracker, projects)
    if arguments.get("project_id") is not None:
        return tool_failure(
            "invalid_arguments",
            "Nothing was changed. project_id targets Project Memory; set scope to project.",
        )
    if getattr(context, "memory_prompt_mode", "agent_user") == "off":
        return tool_failure(
            "memory_unavailable",
            "Nothing was changed. Personal Memory is disabled for this Agent. "
            + (
                "Use scope project for the active Project's Memory."
                if getattr(context, "project_memory_enabled", False)
                else "Ask the user to enable the needed Memory scope in the Agent settings."
            ),
        )
    workspace = Path(context.workspace)
    return _run_memory_operation(context, arguments, memory_service, tracker, workspace, scope)


def _project_memory_result(
    context: ToolContext,
    arguments: JsonObject,
    service: MemoryService,
    tracker: _MemoryThrashTracker | None,
    projects: ProjectStore | None,
) -> JsonObject:
    """Resolve a live Project target without changing the Session's execution identity."""
    if not getattr(context, "project_memory_enabled", False) or projects is None:
        return tool_failure(
            "memory_unavailable",
            "Nothing was changed. Project Memory is unavailable for this Agent. "
            "Ask the user to enable Project Memory in the Agent settings.",
        )
    active = {
        project_id
        for project_id in (context.working_project_id, context.skill_project_id)
        if project_id is not None
    }
    selected = arguments.get("project_id")
    if selected is not None and not isinstance(selected, str):
        return tool_failure(
            "invalid_arguments", "Nothing was changed. project_id must be a string."
        )
    if selected is not None and selected not in active:
        return tool_failure(
            "memory_project_unavailable",
            f"Nothing was changed. Project {selected!r} is not an active Project Context. "
            "Use the Working Project or load the intended Project Context first.",
        )
    if selected is None:
        if len(active) != 1:
            choices = ", ".join(sorted(active))
            return tool_failure(
                "memory_project_ambiguous" if active else "memory_project_unavailable",
                "Nothing was changed. "
                + (
                    f"More than one Project Context is active: {choices}. "
                    "Set project_id to the Project whose Memory you mean."
                    if active
                    else "No Project Context is active. Work in a Project or load its "
                    "Project Context before using Project Memory."
                ),
            )
        selected = next(iter(active))
    if not isinstance(selected, str):
        return tool_failure(
            "invalid_arguments", "Nothing was changed. project_id must be a string."
        )
    try:
        # The owner lock holds capability checks, the anchor and the complete
        # write together, so archival cannot detach a resolved Memory path.
        with projects.configuration_guard():
            project = projects.get(selected)
            if not project.memory_enabled:
                return tool_failure(
                    "memory_unavailable",
                    f"Nothing was changed. Project Memory is disabled for {selected!r}. "
                    "Ask the user to enable it in that Project's settings.",
                )
            result = _run_memory_operation(
                context,
                arguments,
                service,
                tracker,
                projects.memory_workspace(selected),
                "project",
                project_id=selected,
            )
    except ProjectError, OSError:
        return tool_failure(
            "memory_error",
            f"Nothing was changed. Project Memory for {selected!r} is unavailable. "
            "Restore access to the registered Project before retrying.",
        )
    if result["ok"]:
        result["data"]["project_id"] = selected
        result["data"]["content"] = f"Project {selected!r}:\n{result['data']['content']}"
    else:
        result["error"]["message"] = f"Project {selected!r}: {result['error']['message']}"
    return result


def _run_memory_operation(
    context: ToolContext,
    arguments: JsonObject,
    memory_service: MemoryService,
    tracker: _MemoryThrashTracker | None,
    workspace: Path,
    scope: str | None,
    *,
    project_id: str | None = None,
) -> JsonObject:
    action = str(arguments["action"])
    if action == "list":
        try:
            return tool_success(_list_result(memory_service, workspace, scope))
        except MemoryError as error:
            return tool_failure("memory_error", str(error))

    try:
        data = _mutate(
            memory_service, context, action, scope, arguments, workspace, project_id=project_id
        )
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
    context: ToolContext,
    action: str,
    scope: str | None,
    arguments: JsonObject,
    workspace: Path,
    *,
    project_id: str | None = None,
) -> JsonObject:
    """Apply one mutation and show the user the entries it changed."""
    writer = MemoryWriter(
        agent_id=context.agent_id,
        actor="tool",
        session_id=context.session_id,
        run_id=context.run_id,
        run_kind=None if context.run_kind is None else context.run_kind.value,
        project_id=project_id,
    )
    content = arguments.get("content")
    old_text = arguments.get("old_text")
    entry_id = arguments.get("entry_id")
    if action == "add":
        return _add(service, context, scope, content, old_text, entry_id, writer, workspace)
    if old_text is None and entry_id is None and action == "remove" and content is not None:
        # The entry text itself, sent in the only text field the Agent had for it.
        old_text, content = content, None
    if entry_id is not None:
        old_text = _checked_entry_id(
            service, workspace, action, scope, entry_id, old_text, content, project_id=project_id
        )
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
            f"instead, call memory with "
            f"{_call('replace', scope, old_text, content, project_id=project_id)}. Nothing "
            "changed.",
        )
    if action == "replace" and content is None:
        raise _MemoryRefusalError(
            "invalid_arguments",
            "replace needs content: the entry's new text. To delete the entry, call memory "
            f"with {_call('remove', scope, old_text, project_id=project_id)}. Nothing changed.",
        )
    target = _memory_scope(scope) if scope is not None else _scope_for(service, workspace, old_text)
    if action == "replace":
        change = service.replace_matching(workspace, target, old_text, str(content), writer=writer)
        if change.current == change.previous:
            context.add_display_notice(
                "info", "The entry already reads this way; nothing changed.", subject=_NAMES[target]
            )
            return {
                "content": f"The {target} Memory entry already reads that way; nothing changed."
            }
        verb = "Replaced in"
        shown = {"op": "replaced", "previous": change.previous, "text": change.current}
    else:
        change = service.remove_matching(workspace, target, old_text, writer=writer)
        verb = "Removed from"
        shown = {"op": "removed", "text": change.previous}
    context.add_display_memory_changes(target, [shown], revision=change.revision)
    used, budget = service.scope_usage(workspace, target)
    return {
        "content": (
            f"{verb} {target} Memory ({used}/{budget} chars used). Previous text: "
            f'"{_preview(change.previous)}"'
        )
    }


def _add(
    service: MemoryService,
    context: ToolContext,
    scope: str | None,
    content: Any,
    old_text: Any,
    entry_id: Any,
    writer: MemoryWriter,
    workspace: Path,
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
    entry = service.add_entry(workspace, target, content, writer=writer)
    if entry.content in existing:
        context.add_display_notice(
            "info", "This entry already exists; nothing changed.", subject=_NAMES[target]
        )
        return {"content": f"{target.capitalize()} Memory already has this entry; nothing changed."}
    context.add_display_memory_changes(
        target, [{"op": "added", "text": entry.content}], revision=entry.revision
    )
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
    *,
    project_id: str | None = None,
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
        f"{_call(action, target, text, replacement, project_id=project_id)}. Nothing changed.",
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


def _call(
    action: str,
    scope: str | None,
    old_text: str,
    content: str | None = None,
    *,
    project_id: str | None = None,
) -> str:
    call: JsonObject = {"action": action}
    if scope is not None:
        call["scope"] = scope
    if project_id is not None:
        call["project_id"] = project_id
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
    if scope == "project":
        return "project"
    raise ValueError(f"scope must be one of: {', '.join(MEMORY_SCOPES)}")


def _synonym(value: Any, table: dict[str, tuple[str, ...]]) -> Any:
    if not isinstance(value, str):
        return value
    key = spelling(value)
    for canonical, spellings in table.items():
        if key in spellings:
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


def register_memory_tool(
    registry: ToolRegistry, memory_service: MemoryService, projects: ProjectStore | None = None
) -> None:
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
        make_memory_handler(memory_service, projects),
        summary="Save lasting facts about the user and the environment.",
        activation="memory_mode",
        open_input_schema=True,
        unadvertised_parameters=_MEMORY_UNADVERTISED_PARAMETERS,
        argument_normalizer=lambda arguments: _normalize_memory_arguments(
            runtime_contract, arguments
        ),
        announce_changes=("description", "parameters.scope", "parameters.project_id"),
        # Same-turn Memory calls apply in the order written, so "free space, then add"
        # works within one message and text addressing never races a sibling change.
        parallel_safe=False,
        result_schema={"type": "object", "required": ["content"]},
        display=ToolDisplay(
            parts_builder=_memory_display_parts,
            fact_builder=result_count_fact_builder("count", when_arguments={"action": "list"}),
            hidden_argument_keys=("content", "old_text"),
            details=True,
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
