"""Tests for the neutral channel-interaction contract types.

Construction and immutability of the frozen dataclasses, that the public
package re-exports them, and that the ``InteractionResponder`` Protocol is
structurally satisfiable without inheriting from it.
"""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from core.extensions import (
    InteractionButton,
    InteractionEvent,
    InteractionHandlerDeclaration,
    InteractionResponder,
)
from core.extensions.interactions import InteractionButton as DirectButton


def test_button_construction() -> None:
    button = InteractionButton(label="Milk ⬜", data="chk:milk")
    assert button.label == "Milk ⬜"
    assert button.data == "chk:milk"


def test_button_is_frozen() -> None:
    button = InteractionButton(label="Milk", data="chk:milk")
    with pytest.raises(dataclasses.FrozenInstanceError):
        button.label = "Eggs"  # type: ignore[misc]


def test_event_defaults() -> None:
    event = InteractionEvent(
        platform="telegram",
        channel_id="chan-1",
        chat_id="42",
        user_id="7",
        message_id="99",
        data="chk:milk",
        buttons=((InteractionButton(label="Milk ⬜", data="chk:milk"),),),
    )
    assert event.text is None
    assert event.user_display_name is None
    assert event.thread_id is None
    assert event.buttons[0][0].data == "chk:milk"


def test_event_is_frozen() -> None:
    event = InteractionEvent(
        platform="telegram",
        channel_id="chan-1",
        chat_id="42",
        user_id="7",
        message_id="99",
        data="chk:milk",
        buttons=(),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.data = "chk:eggs"  # type: ignore[misc]


def test_handler_declaration_holds_prefix_and_handler() -> None:
    def handler(event: InteractionEvent, responder: InteractionResponder) -> None:
        return None

    declaration = InteractionHandlerDeclaration(prefix="chk", handler=handler)
    assert declaration.prefix == "chk"
    assert declaration.handler is handler


def test_package_reexports_match_module() -> None:
    assert InteractionButton is DirectButton


def test_responder_protocol_is_structural() -> None:
    calls: list[str] = []

    class FakeResponder:
        async def answer(self, text: str | None = None, *, alert: bool = False) -> None:
            calls.append(f"answer:{text}:{alert}")

        async def edit(
            self,
            *,
            text: str | None = None,
            buttons: list[list[InteractionButton]] | None = None,
        ) -> None:
            calls.append(f"edit:{text}")

    # Structural conformance is enforced statically by the type checker; here we
    # confirm the shape is usable at runtime through the Protocol-typed binding.
    responder: InteractionResponder = FakeResponder()
    asyncio.run(responder.answer("done"))
    asyncio.run(responder.edit(text="hi"))
    assert calls == ["answer:done:False", "edit:hi"]
