"""The prompt epoch's Tool knowledge: pin payloads, folded announcements and planned changes."""

from __future__ import annotations

import json
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
from core.tools import ToolDefinitionChangeNote

JsonObject = dict[str, Any]


def _definition(name: str, description: str = "Probe.", **properties: Any) -> JsonObject:
    return {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": properties},
    }


def _catalog(
    *offered: JsonObject,
    usable: tuple[str, ...] = (),
    change_notes: dict[str, ToolDefinitionChangeNote] | None = None,
) -> LiveToolCatalog:
    return LiveToolCatalog(
        usable=frozenset({*usable, *(str(definition["name"]) for definition in offered)}),
        offered=offered,
        sources={str(definition["name"]): definition_source(definition) for definition in offered},
        change_notes=change_notes or {},
    )


def _pin(*definitions: JsonObject) -> ToolEpochPin:
    return ToolEpochPin.start(_catalog(*definitions))


def _plan(view: ToolEpochView, catalog: LiveToolCatalog, *, unlisted: bool = True) -> list[Any]:
    return [
        (change.change, change.tool, change.listed, change.pinned)
        for change in view.plan(catalog, unlisted_tool_calls=unlisted)
    ]


def test_pin_payload_round_trips_and_an_unreadable_pin_counts_as_missing() -> None:
    # Neither the definitions nor their schemas list their keys alphabetically.
    pin = _pin(_definition("read"), _definition("write", zeta={"type": "string"}, alpha={}))
    payload = pin.to_payload()
    # The Session store sorts the keys of the JSON objects it persists.
    stored = json.loads(json.dumps(payload, sort_keys=True))

    restored = ToolEpochPin.from_payload(stored)
    assert restored == pin
    assert restored is not None and json.dumps(restored.definitions) == json.dumps(pin.definitions)
    assert pin.names == ("read", "write")
    for broken in (
        None,
        {**payload, "v": 1},
        {**payload, "epoch": ""},
        {**payload, "definitions": [_definition("read")]},
        {**payload, "definitions": "[{"},
        {**payload, "definitions": json.dumps([_definition("read"), _definition("read")])},
        {**payload, "definitions": json.dumps([{"name": "read", "description": "Read."}])},
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

    # A changed Tool is pinned only when the pinned list shows its previous definition.
    changed = _definition("kept", count={"type": "integer"})
    reshaped = _definition("added", path={"type": "integer"})
    back = _catalog(changed, removed, reshaped)
    assert _plan(view, back) == [
        ("changed", "kept", True, True),
        ("added", "removed", True, True),
        ("changed", "added", False, False),
    ]
    view = view.with_changes(view.plan(back, unlisted_tool_calls=True))
    assert view.definitions() == [changed, removed, reshaped]
    # Pinned Tools keep their pinned bytes; a listed announced Tool shows its change.
    assert view.request_tools(list_announced=True) == [kept, removed, reshaped]
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


def _describe_change(old: JsonObject, new: JsonObject) -> str | None:
    if (old["description"], new["description"]) != ("Lists a and b.", "Lists a, b and c."):
        return "unexpected definitions"
    return " Remote Tool c was added. "


def _failing_change_note(_old: JsonObject, _new: JsonObject) -> str | None:
    raise RuntimeError("broken")


@pytest.mark.parametrize(
    ("change_note", "detail"),
    [
        (None, None),
        (lambda _old, _new: None, None),
        (lambda _old, _new: "  ", None),
        (_failing_change_note, None),
        (_describe_change, "Remote Tool c was added."),
    ],
    ids=["no-note", "silent", "blank", "failing", "described"],
)
def test_a_change_that_keeps_the_parameters_is_announced_only_with_the_tools_text(
    change_note: ToolDefinitionChangeNote | None, detail: str | None
) -> None:
    view = ToolEpochView(pin=_pin(_definition("probe", "Lists a and b.")))
    notes = {} if change_note is None else {"probe": change_note}
    described = _catalog(_definition("probe", "Lists a, b and c."), change_notes=notes)
    reshaped = _catalog(
        _definition("probe", "Lists a, b and c.", count={"type": "integer"}), change_notes=notes
    )

    def planned(catalog: LiveToolCatalog) -> list[tuple[str, str | None]]:
        return [
            (change.change, change.detail)
            for change in view.plan(catalog, unlisted_tool_calls=True)
        ]

    assert planned(described) == ([] if detail is None else [("changed", detail)])
    # A schema change is announced either way; the Tool's text rides along.
    assert planned(reshaped) == [("changed", detail)]
