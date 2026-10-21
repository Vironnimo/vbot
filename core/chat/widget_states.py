"""The note that hands widget values the user sent to the Agent.

A widget is Agent-authored code and its values come from the user's browser,
so titles and values reach the Model only quoted as JSON data under
kernel-authored framing (``.vorch/domain-maps/tools/show_widget.md``).
"""

from __future__ import annotations

from collections.abc import Sequence

from core.chat.messages import ChatMessage, WidgetState
from core.chat.wire_shaping import quote_external_value

_FRAMING = (
    "The user sent the current values of widgets you showed along with the message that "
    "follows. Each line is a widget's title and its values, as JSON:"
)


def widget_state_note(states: Sequence[WidgetState]) -> ChatMessage:
    """Return the note stored right before the user message that sent *states*."""
    if not states:
        raise ValueError("A widget state note needs at least one widget")
    lines = [
        f"{quote_external_value(state.title)}: {quote_external_value(state.state)}"
        for state in states
    ]
    return ChatMessage.note("\n".join([_FRAMING, *lines]), widget_states=states)
