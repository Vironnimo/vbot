"""Checklist bundled extension — flip ⬜/✅ on an interactive channel message tap.

A generic, content-agnostic tap handler: it owns the callback-data prefix
``chk`` and, on each tap, flips the tapped button's leading ⬜↔✅ glyph and edits
the message's keyboard back. It knows nothing about *what* the checklist is
(shopping list, todos, …) — the agent (or a skill) builds and posts the buttons
with ``chk:<payload>`` callback data; this handler just toggles the visual.

The checkbox state lives entirely in the message: the platform delivers the
current keyboard with every tap, so there is no server-side persistence — the
edit is computed purely from the tap event. Concurrent taps on the same message
can momentarily clobber each other (each edits from its own snapshot); this
self-heals on the next tap and is acceptable for the checklist use case.
"""

from __future__ import annotations

from typing import Any

from core.extensions import InteractionButton

# Callback-data prefix this extension owns: a tap whose data is ``chk:<payload>``
# routes here (see core.extensions.interactions for the "<prefix>:<payload>" rule).
_CALLBACK_PREFIX = "chk"

_UNCHECKED = "⬜"
_CHECKED = "✅"


def _flip_label(label: str) -> str:
    """Toggle a leading ⬜↔✅ glyph; leave a label with neither unchanged."""
    if label.startswith(_UNCHECKED):
        return _CHECKED + label[len(_UNCHECKED) :]
    if label.startswith(_CHECKED):
        return _UNCHECKED + label[len(_CHECKED) :]
    return label


async def _toggle(event: Any, responder: Any) -> None:
    """Flip the tapped button's checkbox glyph and edit the keyboard back.

    Rebuilds the full keyboard, preserving every button's callback ``data`` and
    every other button's label, then acknowledges the tap silently (empty ack, no
    toast).
    """
    new_rows: list[list[InteractionButton]] = []
    for row in event.buttons:
        new_row: list[InteractionButton] = []
        for button in row:
            label = _flip_label(button.label) if button.data == event.data else button.label
            new_row.append(InteractionButton(label=label, data=button.data))
        new_rows.append(new_row)

    await responder.edit(buttons=new_rows)
    await responder.answer()


def register(api: Any) -> None:
    # Declares the tap handler; the runtime builds the prefix map after every
    # extension registers. Only collects the declaration (house style).
    api.register_interaction_handler(_CALLBACK_PREFIX, _toggle)
