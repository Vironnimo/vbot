"""Interpret one ``subagent`` call against the caller's Sub-Agents and targets.

Runs after the Tool owner normalized the call syntax and before any side
effect. It settles what a call means when the fields alone do not: an action
left implicit, a stand-in Session id, or a generic worker name from another
harness. Unclear calls become a refusal that names the corrected call;
interpretations that involved judgment become notes for the result.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from core.projects import format_agent_address
from core.subagents._constants import (
    SUBAGENT_NO_TARGET_CHOICES_TEXT,
    SUBAGENT_NONE_YOURS_TEXT,
    SUBAGENT_TARGET_CHOICES_TEMPLATE,
    SUBAGENT_YOURS_TEMPLATE,
)
from core.subagents.catalog import SubAgentPromptTarget, subagent_targets
from core.tools.arguments import required_string
from core.tools.call_syntax import is_placeholder, spelling
from core.tools.tools import JsonObject, ToolContext
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.runtime.interfaces import RuntimeServices
    from core.subagents.links import SubAgentLink

_LOGGER = get_logger("subagents")

# Public Sub-Agent ids are ``sub_`` plus lowercase base32; older ids used hex.
SUBAGENT_ID_PATTERN = re.compile(r"sub_[0-9a-z]+")

# Names other harnesses give their general-purpose worker, compared by spelling.
# vBot's general worker is a copy of the caller, so such a name selects it unless
# an Agent the caller may use carries exactly that id.
_GENERIC_TARGETS = frozenset(
    {"any", "default", "general", "generalist", "generalpurpose", "generic", "myself", "self"}
)
_MAX_LISTED_TARGETS = 30
_MAX_LISTED_SUBAGENTS = 20
# Words in a session_id that mark it as a stand-in, not a Session to continue:
# "new-review", "audit-do-not-use-placeholder", "INVALID_REMOVE".
_NEW_SESSION_WORDS = frozenset(
    {"blank", "dummy", "empty", "invalid", "new", "none", "null", "omit", "placeholder", "unused"}
)


def reads_as_new_session(session_id: str) -> bool:
    """Return whether a session_id that names no Sub-Agent stands for a new one."""
    words = re.split(r"[\W_]+", session_id.casefold())
    return any(word in _NEW_SESSION_WORDS for word in words)


def call_text(arguments: JsonObject) -> str:
    """Render an argument object exactly as the Agent should send it."""
    return json.dumps(arguments, ensure_ascii=False)


def quoted(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def has_text(arguments: JsonObject, name: str) -> bool:
    value = arguments.get(name)
    return isinstance(value, str) and not is_placeholder(value, words=())


def optional_text(arguments: JsonObject, name: str) -> str | None:
    """Return a string field, or ``None`` when omitted.

    Raises ``ToolContractError`` for a value that is not a string.
    """
    if name not in arguments:
        return None
    return required_string(arguments[name], field_name=name).strip() or None


def yours_text(links: Sequence[SubAgentLink]) -> str:
    """List the caller's Sub-Agents as copyable ids, or say that it has none."""
    if not links:
        return SUBAGENT_NONE_YOURS_TEXT
    listed = [f"{link.id} ({link.title or link.session.agent_id})" for link in links]
    if len(listed) > _MAX_LISTED_SUBAGENTS:
        listed = [
            *listed[:_MAX_LISTED_SUBAGENTS],
            f"and {len(listed) - _MAX_LISTED_SUBAGENTS} more",
        ]
    return SUBAGENT_YOURS_TEMPLATE.format(entries="; ".join(listed))


def target_choices(runtime: RuntimeServices, context: ToolContext) -> str:
    """Name the valid ``agent_id`` choices, exactly as the System Prompt lists them."""
    targets = _available_targets(runtime, context)
    if not targets:
        return SUBAGENT_NO_TARGET_CHOICES_TEXT
    listed = [target.agent_id for target in targets[:_MAX_LISTED_TARGETS]]
    if len(targets) > len(listed):
        listed.append(f"and {len(targets) - len(listed)} more listed under Sub-Agents")
    return SUBAGENT_TARGET_CHOICES_TEMPLATE.format(targets=", ".join(listed))


def is_generic_target(runtime: RuntimeServices, context: ToolContext, address: str) -> bool:
    """Return whether *address* is another harness's name for a general worker."""
    if spelling(address) not in _GENERIC_TARGETS or address == context.agent_id:
        return False
    return address not in {target.agent_id for target in _available_targets(runtime, context)}


def subagent_address(link: SubAgentLink) -> str:
    """Return the ``agent_id`` value that names this Sub-Agent's Agent."""
    return format_agent_address(link.session.agent_id, link.session.project_id)


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


__all__ = [
    "SUBAGENT_ID_PATTERN",
    "call_text",
    "has_text",
    "is_generic_target",
    "optional_text",
    "quoted",
    "reads_as_new_session",
    "subagent_address",
    "target_choices",
    "yours_text",
]
