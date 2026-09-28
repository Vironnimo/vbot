"""The prompt epoch's Tool knowledge: pin payloads, folded announcements and planned changes."""

from __future__ import annotations

from typing import Any

import pytest

from core.chat import ChatMessage
from core.chat._tool_epoch import (
    LiveToolCatalog,
    ToolChange,
    ToolEpochPin,
    ToolEpochView,
    definition_source,
    without_other_epoch_tool_changes,
)

JsonObject = dict[str, Any]


def _definition(name: str, description: str = "Probe.", **properties: Any) -> JsonObject:
    return {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": properties},
    }


def _catalog(*offered: JsonObject, usable: tuple[str, ...] = ()) -> LiveToolCatalog:
    return LiveToolCatalog(
        usable=frozenset({*usable, *(str(definition["name"]) for definition in offered)}),
        offered=offered,
        sources={str(definition["name"]): definition_source(definition) for definition in offered},
    )


def _pin(*definitions: JsonObject) -> ToolEpochPin:
    return ToolEpochPin.start(_catalog(*definitions))


def _plan(view: ToolEpochView, catalog: LiveToolCatalog, *, unlisted: bool = True) -> list[Any]:
    return [
        (change.change, change.tool, change.listed, change.pinned)
        for change in view.plan(catalog, unlisted_tool_calls=unlisted)
    ]


def test_pin_payload_round_trips_and_an_unreadable_pin_counts_as_missing() -> None:
    pin = _pin(_definition("read"), _definition("write"))
    payload = pin.to_payload()

    assert ToolEpochPin.from_payload(payload) == pin
    assert pin.names == ("read", "write")
    for broken in (
        None,
        {**payload, "v": 2},
        {**payload, "epoch": ""},
        {**payload, "definitions": [_definition("read"), _definition("read")]},
        {**payload, "definitions": [{"name": "read", "description": "Read."}]},
        {**payload, "sources": {"read": 1}},
    ):
        assert ToolEpochPin.from_payload(broken) is None


def test_plan_announces_additions_removals_and_schema_changes_once() -> None:
    kept, removed = _definition("kept"), _definition("removed")
    view = ToolEpochView(pin=_pin(kept, removed))
    added = _definition("added", path={"type": "string"})
    catalog = _catalog(
        _definition("kept", "New description."),
        added,
        # Usable but not offered (not ready or route-gated): nothing to announce.
        usable=("dormant",),
    )

    # A description-only change stays silent; the capability decides the listed flag.
    assert _plan(view, catalog) == [
        ("removed", "removed", False, False),
        ("added", "added", False, False),
    ]
    assert _plan(view, catalog, unlisted=False)[1] == ("added", "added", True, False)

    view = view.with_changes(view.plan(catalog, unlisted_tool_calls=True))
    assert _plan(view, catalog) == []
    assert view.allowed_names == ("kept", "added")
    assert view.removed_names == frozenset({"removed"})
    assert view.announced_names == ("added",)
    assert view.definitions() == [kept, added]
    assert view.request_tools(list_announced=False) == [kept, removed]
    assert view.request_tools(list_announced=True) == [kept, removed, added]

    changed = _definition("kept", count={"type": "integer"})
    back = _catalog(changed, removed, added)
    assert _plan(view, back) == [("changed", "kept", True, True), ("added", "removed", True, True)]
    view = view.with_changes(view.plan(back, unlisted_tool_calls=True))
    assert view.definitions() == [changed, removed, added]
    assert _plan(view, back) == []


def test_fold_keeps_only_the_announcements_of_the_pinned_epoch() -> None:
    pin = _pin(_definition("kept"))
    current = ToolChange("added", "late", pin.epoch, source="s", definition=_definition("late"))
    other = ToolChange("added", "stale", "other-epoch", source="s", definition=_definition("stale"))
    messages = [
        ChatMessage.user("Hello"),
        ChatMessage.note(other.note_content()),
        ChatMessage.note(current.note_content()),
    ]

    view = ToolEpochView.fold(pin, messages)

    assert view.allowed_names == ("kept", "late")
    assert without_other_epoch_tool_changes(messages, pin.epoch) == [messages[0], messages[2]]


@pytest.mark.parametrize("unlisted", [True, False])
def test_a_pinned_tool_enabled_again_keeps_its_last_announced_definition(unlisted: bool) -> None:
    probe = _definition("probe", a={"type": "string"})
    view = ToolEpochView(pin=_pin(probe))
    changed = _definition("probe", b={"type": "integer"})
    view = view.with_changes(view.plan(_catalog(changed), unlisted_tool_calls=unlisted))
    view = view.with_changes(view.plan(_catalog(usable=()), unlisted_tool_calls=unlisted))
    assert view.removed_names == frozenset({"probe"})

    assert _plan(view, _catalog(changed), unlisted=unlisted) == [("added", "probe", True, True)]
    view = view.with_changes(view.plan(_catalog(changed), unlisted_tool_calls=unlisted))
    assert view.definitions() == [changed]
    assert view.request_tools(list_announced=True) == [probe]
