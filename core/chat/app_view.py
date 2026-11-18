"""A bounded UI snapshot supplied alongside one specialist Session message."""

from __future__ import annotations

from collections.abc import Callable

from core.chat.messages import ChatMessage
from core.chat.wire_shaping import quote_external_value


def app_view_note(
    context: dict[str, str | list[str]], *, mask: Callable[[str], str]
) -> ChatMessage:
    """Quote UI labels as data, masking Secrets before JSON can escape their characters."""
    safe = {
        key: [mask(option) for option in value] if isinstance(value, list) else mask(value)
        for key, value in context.items()
    }
    return ChatMessage.note(
        "The app showed this view when the user sent the following message (JSON data):\n"
        + quote_external_value(safe)
    )
