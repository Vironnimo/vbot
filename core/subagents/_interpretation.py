"""Interpret one ``subagent`` call against tracked work and available targets.

Runs after the Tool owner normalized the call syntax and before any side
effect. It settles what a call means when the fields alone do not: an action
left implicit, a work id sent with ``run``, a Session addressed without its
Agent, or a generic worker name from another harness. Unclear calls become a
refusal that names the corrected call; interpretations that involved judgment
become notes for the result.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from core.projects import format_agent_address
from core.subagents._constants import (
    SUBAGENT_CONTINUE_TRACKED_WORK_TEMPLATE,
    SUBAGENT_CONTINUE_UNTRACKED_WORK_TEXT,
    SUBAGENT_GENERIC_TARGET_NOTE_TEMPLATE,
    SUBAGENT_ID_WITHOUT_ACTION_MESSAGE_TEMPLATE,
    SUBAGENT_IGNORED_WORK_LABEL_NOTE_TEMPLATE,
    SUBAGENT_NO_TARGET_CHOICES_TEXT,
    SUBAGENT_RUN_WITH_WORK_ID_MESSAGE_TEMPLATE,
    SUBAGENT_TARGET_CHOICES_TEMPLATE,
    SUBAGENT_TRACKED_WORK_TEMPLATE,
    SUBAGENT_WORK_SESSION_CONFLICT_MESSAGE_TEMPLATE,
)
from core.subagents.catalog import SubAgentPromptTarget, subagent_targets
from core.tools._call_vocabulary import is_placeholder, spelling
from core.tools.arguments import required_string
from core.tools.tools import JsonObject, ToolContext, tool_failure
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.runtime.interfaces import RuntimeServices
    from core.subagents.tracker import SubAgentBatchTracker, _SubAgentEntry

_LOGGER = get_logger("subagents")

# Public work ids are ``sub_`` plus lowercase base32; older ids used hex.
_WORK_ID = re.compile(r"sub_[0-9a-z]+")

# Names other harnesses give their general-purpose worker, compared by spelling.
# vBot's general worker is a copy of the caller, so such a name selects it unless
# an Agent the caller may use carries exactly that id.
_GENERIC_TARGETS = frozenset(
    {"any", "default", "general", "generalist", "generalpurpose", "generic", "myself", "self"}
)
_MAX_LISTED_TARGETS = 30
_FOLLOW_UP_PLACEHOLDER = "<follow-up>"
# Words in a session_id that mark it as a stand-in, not a Session to continue:
# "new-review", "audit-do-not-use-placeholder", "INVALID_REMOVE".
_NEW_SESSION_WORDS = frozenset(
    {"blank", "dummy", "empty", "invalid", "new", "none", "null", "omit", "placeholder", "unused"}
)


def reads_as_new_session(session_id: str) -> bool:
    """Return whether a session_id that names no Session stands for a new one.

    Only consulted after no Session with that id exists, so an existing Session
    is always continued, whatever its id says.
    """
    words = re.split(r"[\W_]+", session_id.casefold())
    return any(word in _NEW_SESSION_WORDS for word in words)


@dataclass
class RunTarget:
    """What a ``run`` call addresses, settled before any Session work."""

    agent_address: str | None
    session_id: str | None
    tracked_target: tuple[str, str | None] | None = None
    notes: list[str] = field(default_factory=list)


def call_text(arguments: JsonObject) -> str:
    """Render an argument object exactly as the Agent should send it."""
    return json.dumps(arguments, ensure_ascii=False)


def has_task(arguments: JsonObject) -> bool:
    content = arguments.get("content")
    return isinstance(content, str) and not is_placeholder(content, words=())


def implied_action(arguments: JsonObject) -> str | JsonObject:
    """Return the action a call without ``action`` clearly means, or its refusal.

    A task delegates. A work id without a task could mean status or cancel, so it
    is refused with both calls.
    """
    work_id = arguments.get("id")
    if isinstance(work_id, str) and not has_task(arguments):
        return tool_failure(
            "invalid_arguments",
            SUBAGENT_ID_WITHOUT_ACTION_MESSAGE_TEMPLATE.format(
                work_id=work_id,
                status_call=call_text({"action": "status", "id": work_id}),
                cancel_call=call_text({"action": "cancel", "id": work_id}),
            ),
        )
    return "run"


def interpret_run(
    context: ToolContext,
    arguments: JsonObject,
    *,
    runtime: RuntimeServices,
    batch_tracker: SubAgentBatchTracker,
) -> RunTarget | JsonObject:
    """Settle the target of one ``run`` call, or refuse it before side effects.

    Raises ``ToolArgumentError`` for a target field that is not a string.
    """
    agent_address = _optional_text(arguments, "agent_id")
    session_id = _optional_text(arguments, "session_id")
    work_id = _optional_text(arguments, "id")
    notes: list[str] = []

    if work_id is not None:
        entry = _owned_entry(batch_tracker, context, work_id)
        if session_id is not None:
            # session_id names the Session to continue; a copied id is only an echo
            # unless it names work in a different Session.
            if entry is not None and entry.session_id != session_id:
                return tool_failure(
                    "invalid_arguments",
                    SUBAGENT_WORK_SESSION_CONFLICT_MESSAGE_TEMPLATE.format(
                        work_id=work_id,
                        work_session_id=entry.session_id,
                        session_id=session_id,
                        call=call_text(_continuation_arguments(entry)),
                    ),
                )
        elif entry is not None or _WORK_ID.fullmatch(work_id):
            continuation = (
                SUBAGENT_CONTINUE_TRACKED_WORK_TEMPLATE.format(
                    call=call_text(_continuation_arguments(entry))
                )
                if entry is not None
                else SUBAGENT_CONTINUE_UNTRACKED_WORK_TEXT
            )
            return tool_failure(
                "invalid_arguments",
                SUBAGENT_RUN_WITH_WORK_ID_MESSAGE_TEMPLATE.format(
                    work_id=work_id, continuation=continuation
                ),
            )
        else:
            notes.append(SUBAGENT_IGNORED_WORK_LABEL_NOTE_TEMPLATE.format(label=_quoted(work_id)))

    tracked_target = None
    if session_id is not None and agent_address is None:
        session_entry = _entry_for_session(batch_tracker, context, session_id)
        if session_entry is not None:
            tracked_target = (session_entry.agent_id, session_entry.project_id)

    if agent_address is not None and _is_generic_target(runtime, context, agent_address):
        notes.append(SUBAGENT_GENERIC_TARGET_NOTE_TEMPLATE.format(name=_quoted(agent_address)))
        agent_address = None

    return RunTarget(agent_address, session_id, tracked_target, notes)


def target_choices(runtime: RuntimeServices, context: ToolContext) -> str:
    """Name the valid ``agent_id`` choices, exactly as the System Prompt lists them."""
    targets = _available_targets(runtime, context)
    if not targets:
        return SUBAGENT_NO_TARGET_CHOICES_TEXT
    listed = [target.agent_id for target in targets[:_MAX_LISTED_TARGETS]]
    if len(targets) > len(listed):
        listed.append(f"and {len(targets) - len(listed)} more listed under Sub-Agents")
    return SUBAGENT_TARGET_CHOICES_TEMPLATE.format(targets=", ".join(listed))


def tracked_work_text(
    batch_tracker: SubAgentBatchTracker,
    context: ToolContext,
    *,
    unfinished_only: bool = False,
) -> str:
    """List the caller's tracked work as copyable ids, or return an empty string."""
    entries = owned_work(batch_tracker, context, unfinished_only=unfinished_only)
    if not entries:
        return ""
    described = "; ".join(
        f"{entry.work_id} (agent_id {format_agent_address(entry.agent_id, entry.project_id)}, "
        f"session_id {entry.session_id}, {_entry_state(entry)})"
        for entry in entries
    )
    return SUBAGENT_TRACKED_WORK_TEMPLATE.format(entries=described)


def owned_work(
    batch_tracker: SubAgentBatchTracker,
    context: ToolContext,
    *,
    unfinished_only: bool = False,
) -> list[_SubAgentEntry]:
    entries = [
        entry
        for _, entry in batch_tracker.owned_entries(
            context.agent_id, context.session_id, context.project_id
        )
    ]
    if unfinished_only:
        return [entry for entry in entries if not entry.complete]
    return entries


def _optional_text(arguments: JsonObject, name: str) -> str | None:
    if name not in arguments:
        return None
    return required_string(arguments[name], field_name=name)


def _owned_entry(
    batch_tracker: SubAgentBatchTracker, context: ToolContext, work_id: str
) -> _SubAgentEntry | None:
    owned = batch_tracker.owned_entry(
        context.agent_id, context.session_id, context.project_id, work_id
    )
    return owned[1] if owned is not None else None


def _entry_for_session(
    batch_tracker: SubAgentBatchTracker, context: ToolContext, session_id: str
) -> _SubAgentEntry | None:
    matches = [
        entry for entry in owned_work(batch_tracker, context) if entry.session_id == session_id
    ]
    return matches[-1] if matches else None


def _continuation_arguments(entry: _SubAgentEntry) -> JsonObject:
    return {
        "agent_id": format_agent_address(entry.agent_id, entry.project_id),
        "session_id": entry.session_id,
        "content": _FOLLOW_UP_PLACEHOLDER,
    }


def _entry_state(entry: _SubAgentEntry) -> str:
    if entry.complete:
        return "finished"
    if entry.run_id is None:
        return "queued"
    return "running"


def _is_generic_target(runtime: RuntimeServices, context: ToolContext, address: str) -> bool:
    if spelling(address) not in _GENERIC_TARGETS or address == context.agent_id:
        return False
    return address not in {target.agent_id for target in _available_targets(runtime, context)}


def _available_targets(
    runtime: RuntimeServices, context: ToolContext
) -> Sequence[SubAgentPromptTarget]:
    try:
        return subagent_targets(
            runtime, context.agent_id, context.project_id, context.tool_settings
        )
    except Exception:
        # Naming choices only improves a message; a catalog failure must not
        # replace the call's own outcome.
        _LOGGER.warning("Sub-Agent target catalog unavailable", exc_info=True)
        return []


def _quoted(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


__all__ = [
    "RunTarget",
    "call_text",
    "has_task",
    "implied_action",
    "interpret_run",
    "owned_work",
    "reads_as_new_session",
    "target_choices",
    "tracked_work_text",
]
