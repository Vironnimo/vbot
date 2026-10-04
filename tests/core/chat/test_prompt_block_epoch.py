"""The prompt epoch's dynamic block texts: pin payloads, change notes and their reminder text."""

from __future__ import annotations

import json
from collections.abc import Callable, Collection
from typing import Any, cast

import pytest

from core.chat import ChatMessage
from core.chat._prompt_block_epoch import (
    PromptBlockPin,
    ensure_prompt_block_pin,
    plan_prompt_block_change,
    prompt_block_change_from_note,
    without_other_epoch_prompt_block_changes,
)
from core.prompts import BlockCatalog, RenderedBlock
from core.sessions import PROMPT_BLOCK_CHANGE_NOTE_PREFIX

JsonObject = dict[str, Any]


def _catalog(*entries: tuple[str, str], title: str = "Things", frame: str = "") -> RenderedBlock:
    text = "\n".join([frame, *(line for _key, line in entries)])
    return RenderedBlock(text, BlockCatalog(title=title, entries=entries, frame=frame))


def _stored(value: JsonObject) -> JsonObject:
    # The Session store sorts the keys of the JSON objects it persists.
    return cast(JsonObject, json.loads(json.dumps(value, sort_keys=True)))


class _PinStore:
    """The Session's prompt pin slot, with the store's check-and-write contract."""

    def __init__(self) -> None:
        self.value: JsonObject | None = None

    def prompt_pin(self, _address: Any, _slot: str) -> JsonObject | None:
        return self.value

    def ensure_prompt_pin(
        self, _address: Any, _slot: str, value: JsonObject, accept: Callable[[JsonObject], bool]
    ) -> JsonObject:
        if self.value is not None and accept(self.value):
            return self.value
        self.value = _stored(value)
        return self.value


def test_pin_and_note_payloads_round_trip_with_their_entry_order() -> None:
    blocks = {
        "tool:project": _catalog(("zeta", "- zeta"), ("alpha", "- alpha"), frame="## Projects"),
        "tool:bash": RenderedBlock("## Shell"),
    }
    pin = PromptBlockPin.start(blocks)
    change = plan_prompt_block_change(pin, [], {"tool:bash": RenderedBlock("## Shell\n\nKeys")})

    assert PromptBlockPin.from_payload(_stored(pin.to_payload())) == pin
    assert change is not None
    note = ChatMessage.note(change.note_content())
    assert prompt_block_change_from_note(note) == change
    payload = pin.to_payload()
    for broken in (
        None,
        {**payload, "v": 0},
        {**payload, "epoch": ""},
        {**payload, "blocks": {"tool:bash": {"text": 1}}},
        {**payload, "blocks": {"tool:bash": {"text": "", "catalog": {"title": "T"}}}},
    ):
        assert PromptBlockPin.from_payload(broken) is None
    assert prompt_block_change_from_note(ChatMessage.note(PROMPT_BLOCK_CHANGE_NOTE_PREFIX)) is None


@pytest.mark.parametrize(
    ("before", "after", "text"),
    [
        (
            _catalog(("a", "- a"), ("b", "- b")),
            _catalog(("a", "- a2"), ("c", "- c")),
            "Things changed since your System Prompt listed them.\n"
            "Added:\n- c\nChanged to:\n- a2\nRemoved:\n- b",
        ),
        (
            _catalog(("a", "- a"), frame="## Things\n\nWait 5 minutes."),
            _catalog(("a", "- a"), frame="## Things\n\nWait 7 minutes."),
            'The "Things" section of your System Prompt changed. Use this current version '
            "instead:\n\n## Things\n\nWait 7 minutes.\n- a",
        ),
        (
            RenderedBlock("## Shell\n\nKeys: A"),
            RenderedBlock(""),
            'The "Shell" section of your System Prompt no longer applies.',
        ),
        (
            RenderedBlock(""),
            RenderedBlock("## Shell\n\nKeys: A"),
            "This section now applies in addition to your System Prompt:\n\n## Shell\n\nKeys: A",
        ),
        (
            RenderedBlock("x" * 100),
            RenderedBlock("y"),
            f'The "{"x" * 77}..." section of your System Prompt changed. Use this current '
            "version instead:\n\ny",
        ),
    ],
    ids=["catalog-entries", "catalog-frame", "emptied", "filled", "unheaded"],
)
def test_a_changed_block_is_announced_by_its_entries_or_its_current_text(
    before: RenderedBlock, after: RenderedBlock, text: str
) -> None:
    pin = PromptBlockPin.start({"tool:x": before})

    change = plan_prompt_block_change(pin, [], {"tool:x": after})

    assert change is not None and change.text == text


def test_the_plan_counts_only_the_pinned_epochs_notes() -> None:
    pin = PromptBlockPin.start({"tool:x": _catalog(("a", "- a"))})
    live = {"tool:x": _catalog(("a", "- a"), ("b", "- b"))}
    told = plan_prompt_block_change(pin, [], live)
    assert told is not None
    note = ChatMessage.note(told.note_content())
    other_epoch = PromptBlockPin.start(pin.blocks)
    stale = plan_prompt_block_change(other_epoch, [], live)
    assert stale is not None
    stale_note = ChatMessage.note(stale.note_content())
    malformed = ChatMessage.note(PROMPT_BLOCK_CHANGE_NOTE_PREFIX + "{")
    user = ChatMessage.user("hi")

    assert plan_prompt_block_change(pin, [note], live) is None
    assert plan_prompt_block_change(pin, [stale_note], live) == told
    assert plan_prompt_block_change(pin, [note, stale_note], {}) is None
    assert without_other_epoch_prompt_block_changes(
        [user, stale_note, note, malformed], pin.epoch
    ) == [user, note]


def test_a_block_shown_first_mid_epoch_joins_the_pin_and_pinned_texts_stay() -> None:
    live = {"tool:x": RenderedBlock("x1")}
    store = _PinStore()

    def render(pinned: Collection[str]) -> dict[str, RenderedBlock]:
        return {block_id: block for block_id, block in live.items() if block_id not in pinned}

    first = ensure_prompt_block_pin(cast(Any, store), cast(Any, None), render)
    live.update({"tool:x": RenderedBlock("x2"), "extension:y": RenderedBlock("y1")})
    second = ensure_prompt_block_pin(cast(Any, store), cast(Any, None), render)

    assert first.texts() == {"tool:x": "x1"}
    assert second.epoch == first.epoch
    assert second.texts() == {"tool:x": "x1", "extension:y": "y1"}
    assert PromptBlockPin.from_payload(store.value) == second
