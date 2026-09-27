"""Checklist: the shipped Extension toggles tapped checklist buttons.

The real Extension is loaded from the bundled root and taps go through the
registry's channel interaction dispatch, so the tests also prove that the root
ships a loadable Extension that owns the ``chk`` prefix.
"""

from __future__ import annotations

import asyncio

import pytest

from core.extensions import ExtensionRegistry, InteractionButton, InteractionEvent
from tests.resources.extensions.bundled_test_support import load_bundled

_UNCHECKED = "⬜"
_CHECKED = "✅"

Rows = list[list[InteractionButton]]


class _RecordingResponder:
    """Records the edited keyboard and every acknowledgement of the tap."""

    def __init__(self) -> None:
        self.answers: list[tuple[str | None, bool]] = []
        self.edited_buttons: Rows | None = None

    async def answer(self, text: str | None = None, *, alert: bool = False) -> None:
        self.answers.append((text, alert))

    async def edit(self, *, text: str | None = None, buttons: Rows | None = None) -> None:
        self.edited_buttons = buttons


def _tap(registry: ExtensionRegistry, data: str, rows: Rows) -> _RecordingResponder:
    event = InteractionEvent(
        platform="telegram",
        channel_id="ch",
        chat_id="1",
        user_id="2",
        message_id="3",
        data=data,
        buttons=tuple(tuple(row) for row in rows),
    )
    responder = _RecordingResponder()
    assert asyncio.run(registry.dispatch_channel_interaction(event, responder)) is True
    return responder


def _button(label: str, item: str) -> InteractionButton:
    return InteractionButton(label=label, data=f"chk:{item}")


def test_checklist_loads_from_the_bundled_root_and_owns_the_chk_prefix() -> None:
    registry = load_bundled("checklist")

    record = next(r for r in registry.records() if r.name == "checklist")
    assert record.status == "loaded"
    assert [d.prefix for d in record.declarations.interaction_handlers] == ["chk"]


@pytest.mark.parametrize(
    ("tapped", "rows", "expected"),
    [
        pytest.param(
            "chk:eggs",
            [
                [_button(f"{_UNCHECKED} Milk", "milk")],
                [_button(f"{_UNCHECKED} Eggs", "eggs"), _button(f"{_CHECKED} Bread", "bread")],
            ],
            [
                [_button(f"{_UNCHECKED} Milk", "milk")],
                [_button(f"{_CHECKED} Eggs", "eggs"), _button(f"{_CHECKED} Bread", "bread")],
            ],
            id="checks-only-the-tapped-item",
        ),
        pytest.param(
            "chk:milk",
            [[_button(f"{_CHECKED} Milk", "milk")]],
            [[_button(f"{_UNCHECKED} Milk", "milk")]],
            id="unchecks-a-checked-item",
        ),
        pytest.param(
            "chk:milk",
            [[_button("Milk", "milk")]],
            [[_button("Milk", "milk")]],
            id="leaves-a-label-without-a-glyph",
        ),
    ],
)
def test_tap_edits_the_keyboard_and_acknowledges_silently(
    tapped: str, rows: Rows, expected: Rows
) -> None:
    responder = _tap(load_bundled("checklist"), tapped, rows)

    assert responder.edited_buttons == expected
    assert responder.answers == [(None, False)]
