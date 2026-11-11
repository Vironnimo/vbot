"""The one-time environment transition at an imported conversation's first Run."""

from __future__ import annotations

from collections.abc import Sequence

from core.chat.messages import ChatMessage
from core.chat.wire_shaping import quote_external_value

IMPORT_REMINDER_PREFIX = "[imported-conversation] "


def import_transition_note(messages: Sequence[ChatMessage]) -> str | None:
    """Return the note until the transition itself is persisted.

    Inspect canonical lineage, including original entries before Compaction. The
    caller commits the note before the first request under its write lock. Accessor
    input can already be persisted by admission, before this Run starts.
    Forks made before that input receive their own transition; later forks inherit it.
    """
    sources: set[str] = set()
    for message in messages:
        if message.external_history is not None:
            sources.add(str(message.external_history["source"]))
        elif (
            message.role == "note"
            and isinstance(message.content, str)
            and message.content.startswith(IMPORT_REMINDER_PREFIX)
        ):
            return None
    if not sources:
        return None
    return (
        IMPORT_REMINDER_PREFIX
        + "This conversation was imported from "
        + quote_external_value(", ".join(sorted(sources)))
        + " and is now continuing in vBot. Earlier tool calls and results record work "
        "in the previous environment. Those tool definitions no longer apply; use the "
        "tools provided with this request and their current names and parameters. "
        "Previous working directories, permissions, and running processes do not "
        "establish the current environment."
    )
