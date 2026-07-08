"""Tests for the shipped checklist bundled extension.

These load the **real** extension out of ``resources/extensions/checklist``
through ``ExtensionRegistry.load`` (with a bundled root) and dispatch taps
through the registry — so they double as proof that the bundled root ships a
loadable extension whose handler is registered under the ``chk`` prefix. The
responder is a recording stub so the edited keyboard and the ack are observable.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.extensions import ExtensionRegistry, InteractionButton, InteractionEvent

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BUNDLED_EXTENSIONS_DIR = _REPO_ROOT / "resources" / "extensions"

_UNCHECKED = "⬜"
_CHECKED = "✅"


@pytest.fixture(autouse=True)
def _clean_extension_modules() -> Iterator[None]:
    """Drop the synthetic ``vbot_ext`` namespace after each test."""
    yield
    for module_name in list(sys.modules):
        if module_name == "vbot_ext" or module_name.startswith("vbot_ext."):
            del sys.modules[module_name]


class _RecordingResponder:
    """Records the keyboard passed to ``edit`` and whether the tap was answered."""

    def __init__(self) -> None:
        self.answered = False
        self.edited_buttons: list[list[InteractionButton]] | None = None

    async def answer(self, text: str | None = None, *, alert: bool = False) -> None:
        self.answered = True

    async def edit(
        self,
        *,
        text: str | None = None,
        buttons: list[list[InteractionButton]] | None = None,
    ) -> None:
        self.edited_buttons = buttons


def _load_registry() -> ExtensionRegistry:
    return ExtensionRegistry.load(
        _REPO_ROOT / "does-not-exist-data-extensions",
        bundled_dir=_BUNDLED_EXTENSIONS_DIR,
    )


def _event(data: str, rows: list[list[InteractionButton]]) -> InteractionEvent:
    return InteractionEvent(
        platform="telegram",
        channel_id="ch",
        chat_id="1",
        user_id="2",
        message_id="3",
        data=data,
        buttons=tuple(tuple(row) for row in rows),
    )


def _dispatch(data: str, rows: list[list[InteractionButton]]) -> _RecordingResponder:
    registry = _load_registry()
    responder = _RecordingResponder()
    handled = asyncio.run(registry.dispatch_channel_interaction(_event(data, rows), responder))
    assert handled is True
    return responder


def test_checklist_extension_is_loaded_and_registered_for_chk_prefix() -> None:
    registry = _load_registry()

    record = next(r for r in registry.records() if r.name == "checklist")
    assert record.status == "loaded"
    assert [d.prefix for d in record.declarations.interaction_handlers] == ["chk"]


def test_tapping_unchecked_item_checks_only_that_item() -> None:
    rows = [
        [
            InteractionButton(label=f"{_UNCHECKED} Milk", data="chk:milk"),
            InteractionButton(label=f"{_UNCHECKED} Eggs", data="chk:eggs"),
        ]
    ]

    responder = _dispatch("chk:milk", rows)

    assert responder.answered is True
    assert responder.edited_buttons == [
        [
            InteractionButton(label=f"{_CHECKED} Milk", data="chk:milk"),
            InteractionButton(label=f"{_UNCHECKED} Eggs", data="chk:eggs"),
        ]
    ]


def test_tapping_checked_item_unchecks_it() -> None:
    rows = [[InteractionButton(label=f"{_CHECKED} Milk", data="chk:milk")]]

    responder = _dispatch("chk:milk", rows)

    assert responder.edited_buttons == [
        [InteractionButton(label=f"{_UNCHECKED} Milk", data="chk:milk")]
    ]


def test_label_without_a_glyph_is_left_unchanged() -> None:
    rows = [[InteractionButton(label="Milk", data="chk:milk")]]

    responder = _dispatch("chk:milk", rows)

    assert responder.edited_buttons == [[InteractionButton(label="Milk", data="chk:milk")]]
    assert responder.answered is True


def test_preserves_other_labels_and_every_callback_data_across_rows() -> None:
    rows = [
        [InteractionButton(label=f"{_UNCHECKED} Milk", data="chk:milk")],
        [
            InteractionButton(label=f"{_UNCHECKED} Eggs", data="chk:eggs"),
            InteractionButton(label=f"{_CHECKED} Bread", data="chk:bread"),
        ],
    ]

    responder = _dispatch("chk:eggs", rows)

    # Only "Eggs" flipped; Milk and Bread keep their labels, and every data survives.
    assert responder.edited_buttons == [
        [InteractionButton(label=f"{_UNCHECKED} Milk", data="chk:milk")],
        [
            InteractionButton(label=f"{_CHECKED} Eggs", data="chk:eggs"),
            InteractionButton(label=f"{_CHECKED} Bread", data="chk:bread"),
        ],
    ]
