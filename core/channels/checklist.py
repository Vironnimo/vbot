"""Checklist messages: item buttons that toggle in the message, and their submitted changes.

A checklist is an inline keyboard ``channel_send`` builds from the Agent's items:
one button per item, then any other buttons of the call, then the done button.
An item button's label carries the item's current state as a leading glyph and
its callback data ``chk:<0|1>:<item id>`` the state it was sent with, so the
whole checklist lives in the message: a tap flips the glyph in place without a
Run (the adapter calls :func:`toggled_checklist_buttons`), and the done button,
a reserved ``run:`` button, wakes the Agent with what changed since sending
(:func:`checklist_changes`). Nothing is stored server-side, so a checklist
works the same from Identity and Project Sessions and across restarts.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from core.channels.config import ChannelConfigError
from core.extensions import CHECKLIST_PREFIX, RUN_TRIGGER_PREFIX, InteractionButton

CHECKLIST_DONE_DATA = f"{RUN_TRIGGER_PREFIX}:checklist"
UNCHECKED_GLYPH = "⬜"
CHECKED_GLYPH = "✅"

_MAX_CALLBACK_DATA_BYTES = 64
# "chk:<0|1>:" precedes the id inside the platform's 64-byte callback data.
CHECKLIST_ITEM_ID_MAX_BYTES = _MAX_CALLBACK_DATA_BYTES - len(f"{CHECKLIST_PREFIX}:0:")


@dataclass(frozen=True)
class ChecklistItem:
    """One checklist entry as the Agent sends it: its id, visible label and state."""

    id: str
    label: str
    checked: bool


@dataclass(frozen=True)
class ChecklistChanges:
    """The items whose state differs from the state they were sent with."""

    checked: tuple[ChecklistItem, ...]
    unchecked: tuple[ChecklistItem, ...]
    unchanged: int


def checklist_keyboard(
    items: Sequence[ChecklistItem],
    *,
    done_label: str,
    extra_rows: Iterable[list[InteractionButton]] = (),
) -> list[list[InteractionButton]]:
    """Return the keyboard of a checklist: item rows, ``extra_rows``, then the done button."""
    if not items:
        raise ChannelConfigError("a checklist needs at least one item")
    seen: set[str] = set()
    rows: list[list[InteractionButton]] = []
    for item in items:
        if not item.id or not item.label or not done_label:
            raise ChannelConfigError("checklist ids and labels must be non-empty")
        if len(item.id.encode("utf-8")) > CHECKLIST_ITEM_ID_MAX_BYTES:
            raise ChannelConfigError(
                f"checklist item id exceeds {CHECKLIST_ITEM_ID_MAX_BYTES} bytes: {item.id!r}"
            )
        if item.id in seen:
            raise ChannelConfigError(f"checklist item id appears twice: {item.id!r}")
        seen.add(item.id)
        glyph = CHECKED_GLYPH if item.checked else UNCHECKED_GLYPH
        rows.append(
            [
                InteractionButton(
                    label=f"{glyph} {item.label}",
                    data=f"{CHECKLIST_PREFIX}:{int(item.checked)}:{item.id}",
                )
            ]
        )
    rows.extend(list(row) for row in extra_rows)
    rows.append([InteractionButton(label=done_label, data=CHECKLIST_DONE_DATA)])
    return rows


def toggled_checklist_buttons(
    buttons: Iterable[Iterable[InteractionButton]], tapped_data: str
) -> list[list[InteractionButton]]:
    """Return the keyboard with the tapped item's leading glyph flipped.

    Every other label and every callback value stays as the platform delivered it.
    """
    return [
        [
            InteractionButton(label=_flipped(button.label), data=button.data)
            if button.data == tapped_data
            else button
            for button in row
        ]
        for row in buttons
    ]


def checklist_changes(
    buttons: Iterable[Iterable[InteractionButton]],
) -> ChecklistChanges | None:
    """Compare each item's current glyph with its sent state; None without checklist items."""
    checked: list[ChecklistItem] = []
    unchecked: list[ChecklistItem] = []
    unchanged = 0
    found = False
    for row in buttons:
        for button in row:
            item = _current_item(button)
            if item is None:
                continue
            found = True
            sent_checked = button.data.split(":", 2)[1] == "1"
            if item.checked == sent_checked:
                unchanged += 1
            elif item.checked:
                checked.append(item)
            else:
                unchecked.append(item)
    if not found:
        return None
    return ChecklistChanges(checked=tuple(checked), unchecked=tuple(unchecked), unchanged=unchanged)


def is_checklist_button(button: InteractionButton) -> bool:
    """Whether ``button`` is a checklist item or done button ``checklist_keyboard`` built."""
    return button.data == CHECKLIST_DONE_DATA or _sent_item(button) is not None


def _sent_item(button: InteractionButton) -> tuple[bool, str] | None:
    prefix, separator, rest = button.data.partition(":")
    state, separator_2, item_id = rest.partition(":")
    if prefix != CHECKLIST_PREFIX or not separator or not separator_2 or not item_id:
        return None
    if state not in ("0", "1"):
        return None
    return state == "1", item_id


def _current_item(button: InteractionButton) -> ChecklistItem | None:
    sent = _sent_item(button)
    if sent is None:
        return None
    sent_checked, item_id = sent
    label = button.label
    for glyph, checked in ((CHECKED_GLYPH, True), (UNCHECKED_GLYPH, False)):
        if label.startswith(glyph):
            return ChecklistItem(id=item_id, label=label[len(glyph) :].strip(), checked=checked)
    # A label without a glyph shows no state change; keep the sent state.
    return ChecklistItem(id=item_id, label=label.strip(), checked=sent_checked)


def _flipped(label: str) -> str:
    if label.startswith(UNCHECKED_GLYPH):
        return CHECKED_GLYPH + label[len(UNCHECKED_GLYPH) :]
    if label.startswith(CHECKED_GLYPH):
        return UNCHECKED_GLYPH + label[len(CHECKED_GLYPH) :]
    return label
