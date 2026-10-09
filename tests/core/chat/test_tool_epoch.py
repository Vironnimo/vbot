"""The prompt epoch's Tool knowledge: pin payloads, folded announcements and planned changes."""

from __future__ import annotations

import json
from typing import Any

import pytest

from core.chat import ChatMessage
from core.chat._step_outcomes import (
    TOOL_FINALIZATION_DISABLED_FAILURE_CODE,
    TOOL_ITERATION_LIMIT_FAILURE_CODE,
    _terminal_tool_failure,
)
from core.chat._tool_epoch import (
    LiveToolCatalog,
    ToolChange,
    ToolEpochPin,
    ToolEpochView,
    called_tool_names,
    definition_source,
    tool_change_from_note,
    without_other_epoch_tool_changes,
)
from core.chat.messages import ToolCall, ToolCallRejection
from core.chat.tool_dispatch import TOOL_REMOVED_ERROR_CODE
from core.providers.adapter import TERMINAL_OUTCOME_OUTPUT_TRUNCATED
from core.tools import ToolDefinitionChangeNote, tool_failure, tool_success

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
    on_demand: dict[str, str] | None = None,
) -> LiveToolCatalog:
    return LiveToolCatalog(
        usable=frozenset({*usable, *(str(definition["name"]) for definition in offered)}),
        offered=offered,
        sources={str(definition["name"]): definition_source(definition) for definition in offered},
        change_notes=change_notes or {},
        on_demand=on_demand or {},
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
    pin = ToolEpochPin.start(
        _catalog(
            _definition("read"),
            _definition("write", zeta={"type": "string"}, alpha={}),
            _definition("zip"),
            _definition("fetch"),
            on_demand={"zip": "Pack files.", "fetch": "Fetch a page."},
        )
    )
    payload = pin.to_payload()
    # The Session store sorts the keys of the JSON objects it persists.
    stored = json.loads(json.dumps(payload, sort_keys=True))

    restored = ToolEpochPin.from_payload(stored)
    assert restored == pin
    assert restored is not None and json.dumps(restored.definitions) == json.dumps(pin.definitions)
    # On-demand Tools are pinned by name and summary only, as the System Prompt lists them.
    assert pin.names == ("read", "write")
    assert payload["on_demand"] == [["fetch", "Fetch a page."], ["zip", "Pack files."]]
    # A pin without On-demand Tools keeps the payload shape of earlier versions.
    assert "on_demand" not in _pin(_definition("read")).to_payload()
    for broken in (
        {**payload, "on_demand": {"zip": "Pack files."}},
        {**payload, "on_demand": [["zip"]]},
        {**payload, "on_demand": [["", "Nameless."]]},
        {**payload, "on_demand": [["zip", None]]},
        # A Tool is either listed with its definition or loaded on demand.
        {**payload, "on_demand": [["read", "Read files."]]},
        {**payload, "on_demand": [["zip", "Pack."], ["zip", "Pack again."]]},
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


def test_on_demand_tools_are_known_by_name_until_a_silent_load_records_the_definition() -> None:
    read, fetch = _definition("read"), _definition("fetch")
    search = _definition("search", query={"type": "string"})
    on_demand = {"search": "Search the web.", "fetch": "Fetch a page."}
    catalog = _catalog(read, search, fetch, on_demand=on_demand)
    view = ToolEpochView(pin=ToolEpochPin.start(catalog))

    # Callable before loading; dispatch has no definition the Model was given yet.
    assert view.allowed_names == ("read", "fetch", "search")
    assert view.loadable_names == ("fetch", "search")
    assert view.definitions() == [read]
    assert _plan(view, catalog) == []

    loads = view.loads(catalog)
    assert sorted(loads) == ["fetch", "search"]
    note = ChatMessage.note(loads["search"].note_content())
    assert tool_change_from_note(note) == loads["search"]

    view = ToolEpochView.fold(view.pin, [note])
    assert view.loadable_names == ("fetch",)
    assert view.definitions() == [read, search]
    assert view.request_tools(list_announced=False) == [read]
    assert view.request_tools(list_announced=True) == [read, search]
    assert _plan(view, catalog) == []

    # Removed and enabled again: the loaded Tool returns with its definition, so a route that
    # lists announced Tools keeps listing it; the Tool known by name only is announced for
    # loading again.
    view = view.with_changes(view.plan(_catalog(read), unlisted_tool_calls=False))
    assert view.allowed_names == ("read",)
    assert _plan(view, catalog, unlisted=False) == [
        ("on_demand", "fetch", False, False),
        ("added", "search", True, False),
    ]
    view = view.with_changes(view.plan(catalog, unlisted_tool_calls=False))
    assert view.loadable_names == ("fetch",)
    assert view.request_tools(list_announced=True) == [read, search]


def test_a_tool_enabled_mid_epoch_is_announced_for_loading_when_the_agent_loads_it_on_demand() -> (
    None
):
    read, loader = _definition("read"), _definition("load_tools")
    view = ToolEpochView(pin=ToolEpochPin.start(_catalog(read)))
    catalog = _catalog(
        read,
        _definition("search"),
        _definition("fetch"),
        loader,
        on_demand={"search": "Search the web", "fetch": ""},
    )

    changes = view.plan(catalog, unlisted_tool_calls=False)

    # load_tools arrives with the first Tool to load, ahead of the notes that name it.
    assert [(change.change, change.tool, change.definition) for change in changes] == [
        ("added", "load_tools", loader),
        ("on_demand", "search", None),
        ("on_demand", "fetch", None),
    ]
    restored = [
        tool_change_from_note(ChatMessage.note(change.note_content())) for change in changes
    ]
    assert restored == list(changes)
    view = view.with_changes(changes)
    assert view.loadable_names == ("search", "fetch")
    assert view.request_tools(list_announced=True) == [read, loader]
    assert _plan(view, catalog) == []
    # A removal of a Tool known by name only is announced like any other; load_tools stays
    # while a usable Tool is on demand.
    gone = _catalog(read, _definition("search"), loader, on_demand={"search": "Search."})
    assert _plan(view, gone) == [("removed", "fetch", False, False)]


def test_switching_on_demand_loading_mid_epoch_keeps_every_tool_the_model_knows() -> None:
    read, search = _definition("read"), _definition("search")
    loader = _definition("load_tools")
    view = ToolEpochView(
        pin=ToolEpochPin.start(_catalog(read, search, loader, on_demand={"search": "Search."}))
    )

    # Switched off: load_tools goes, the Tools known by name only arrive with their definition.
    off = _catalog(read, search)
    assert _plan(view, off) == [
        ("removed", "load_tools", False, False),
        ("added", "search", False, False),
    ]
    view = view.with_changes(view.plan(off, unlisted_tool_calls=True))
    assert view.loadable_names == ()
    assert view.definitions() == [read, search]

    # Switched on again: the pinned load_tools returns, and Tools with a known definition stay
    # as they are.
    on = _catalog(read, search, loader, on_demand={"search": "Search."})
    assert _plan(view, on) == [("added", "load_tools", True, True)]


def test_a_new_epoch_keeps_listing_the_tools_kept_through_compaction() -> None:
    catalog = _catalog(
        _definition("read"),
        _definition("search"),
        _definition("fetch"),
        _definition("load_tools"),
        on_demand={"search": "Search.", "fetch": "Fetch."},
    )

    pin = ToolEpochPin.start(catalog, keep=frozenset({"search", "unknown"}))

    assert pin.names == ("read", "search", "load_tools")
    assert pin.on_demand == (("fetch", "Fetch."),)

    # With every On-demand Tool kept, nothing is left to load: load_tools is neither listed
    # nor announced later.
    pin = ToolEpochPin.start(catalog, keep=frozenset({"search", "fetch"}))

    assert pin.names == ("read", "search", "fetch")
    assert pin.on_demand == ()
    assert _plan(ToolEpochView(pin=pin), catalog) == []


def test_called_tool_names_counts_the_calls_of_the_current_agent_whose_tool_ran() -> None:
    rejection = ToolCallRejection(code="unknown_tool", message="Unknown Tool.", fingerprint="f")
    # The codes of the Results that dispatch and the Run write for calls that never ran.
    not_run = [
        "tool_not_found",
        "tool_not_allowed",
        TOOL_REMOVED_ERROR_CODE,
        TOOL_FINALIZATION_DISABLED_FAILURE_CODE,
        TOOL_ITERATION_LIMIT_FAILURE_CODE,
        _terminal_tool_failure(TERMINAL_OUTCOME_OUTPUT_TRUNCATED)[0],
        _terminal_tool_failure(None)[0],
    ]

    def calls(*names: str, rejected: str | None = None) -> ChatMessage:
        return ChatMessage.assistant(
            model="model",
            content="",
            tool_calls=[
                ToolCall(
                    id=f"call-{name}",
                    name=name,
                    rejection=rejection if name == rejected else None,
                )
                for name in names
            ],
        )

    def result(name: str, envelope: JsonObject) -> ChatMessage:
        return ChatMessage.tool(
            tool_call_id=f"call-{name}", name=name, content=json.dumps(envelope)
        )

    refused = [f"refused-{index}" for index in range(len(not_run))]
    messages = [
        calls("old"),
        ChatMessage.compaction_checkpoint(
            summary="Summary.", projection=[], compacted_token_count=0
        ),
        # The Agent that handed the Session over called this one.
        calls("previous"),
        result("previous", tool_success({})),
        ChatMessage.agent_takeover(from_address="coder", to_address="writer"),
        calls("search", "fetch", "edit", "pending", rejected="fetch"),
        result("search", tool_success({})),
        # Invalid arguments reached the Tool's contract: the Agent uses that Tool.
        result("edit", tool_failure("invalid_arguments", "Fix the arguments.")),
        ChatMessage.user("Next."),
        calls(*refused),
        *(
            result(name, tool_failure(code, "Not run."))
            for name, code in zip(refused, not_run, strict=True)
        ),
    ]

    assert called_tool_names(messages) == frozenset({"search", "edit", "pending"})
