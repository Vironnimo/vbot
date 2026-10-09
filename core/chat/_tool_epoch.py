"""Prompt-epoch Tool catalog: the pinned Tool list and the Tool changes announced since.

Within one prompt epoch a Session sends the same Tool definitions on every
request, so the Provider prompt cache survives Tool changes. The first request
of an epoch pins its definitions (:class:`ToolEpochPin`); a successful
Compaction starts the next epoch with a fresh pin. A later change reaches the
Model as a ``[tool-change]`` note instead: a System Reminder that names the Tool
and, when the Model cannot read it from its Tool list, carries its definition.
The note stores a JSON payload, not text, so :class:`ToolEpochView` rebuilds
what the Model knows (dispatch allowlist, contracts, request Tools) from the
pin and Session history, and the wording is rendered at request time with the
Tool's Model-facing name. :meth:`ToolEpochView.plan` compares that knowledge
with the :class:`LiveToolCatalog` and returns the changes to announce.

While the Agent loads Tools on demand (``core.tools.on_demand``), the pin lists
only the Tools its Tool list keeps; its On-demand Tools are known by name and
summary only (the System Prompt lists them) until a ``load_tools`` call returns
a definition, which a silent ``loaded`` note records.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field, replace
from functools import cached_property
from typing import Any, Literal, cast

from core.chat.messages import ChatMessage, JsonObject
from core.sessions import TOOL_CHANGE_NOTE_PREFIX, is_tool_change_note
from core.tools import ToolDefinitionChangeNote, model_tool_name
from core.tools.on_demand import LOAD_TOOLS_TOOL_NAME
from core.utils.logging import get_logger

_LOGGER = get_logger("chat")

TOOL_CHANGE_NOTE_VERSION = 1
TOOL_EPOCH_PIN_VERSION = 2

ToolChangeKind = Literal["added", "removed", "changed", "loaded", "on_demand"]
_TOOL_CHANGE_KINDS: frozenset[str] = frozenset(
    {"added", "removed", "changed", "loaded", "on_demand"}
)
# Kinds whose note carries the definition the Model was given.
_DEFINED_KINDS: frozenset[str] = frozenset({"added", "changed", "loaded"})

_ADDED_UNLISTED = (
    "The Tool {name} was enabled for you in this Session. Your Tool list does not show it "
    "because the list stays unchanged until the conversation is compacted. Call {name} by "
    "name with a normal Tool call."
)
_ADDED_LISTED = (
    "The Tool {name} was enabled for you in this Session and now appears in your Tool list."
)
_AVAILABLE_AGAIN = "The Tool {name} is available to you again."
_REMOVED = (
    "The Tool {name} was removed from your Tools in this Session. Calls to {name} fail; "
    "use your other Tools instead."
)
_CHANGED_PINNED = (
    "The Tool {name} changed in this Session. Your Tool list still shows its previous "
    "definition until the conversation is compacted; call it with this definition instead."
)
_CHANGED = "The Tool {name} changed in this Session. Call {name} with this definition instead."
_ON_DEMAND = (
    "Tool {name} is now available: {summary} Load its definition with `{loader}` before you "
    "call it."
)
_ON_DEMAND_UNSUMMARIZED = (
    "Tool {name} is now available. Load its definition with `{loader}` before you call it."
)
_SENTENCE_ENDS = (".", "!", "?", "…")
_CHANGE_DETAIL = "\nChange: {detail}"
_DEFINITION = "\nDescription: {description}\nParameters (JSON Schema): {schema}"


@dataclass(frozen=True)
class ToolChange:
    """One announced change to the Tools of a prompt epoch.

    ``tool`` is the registry name. ``definition`` is the Provider definition the
    Model is told to use (absent for a removal and an ``on_demand`` change).
    ``loaded`` records, silently, the definition a ``load_tools`` call returned;
    ``on_demand`` announces a Tool the Agent may now load, by its ``summary``.
    ``listed`` says whether the
    Tool appears in the request's Tool list: always for a pinned Tool, and for
    an addition on a route that drops calls to unlisted Tools. ``pinned`` marks a
    Tool of the epoch's pinned list: an addition makes it available again after
    a removal, and a change leaves its pinned definition in the Tool list.
    ``source`` fingerprints the Tool's registered definition so a
    later change is recognized; ``detail`` is optional text a ``changed`` note
    carries from the Tool's ``definition_change_note``.
    """

    change: ToolChangeKind
    tool: str
    epoch: str
    source: str | None = None
    definition: JsonObject | None = None
    listed: bool = False
    pinned: bool = False
    detail: str | None = None
    summary: str | None = None

    def note_content(self) -> str:
        """Return the note content that persists this change."""

        definition = self.definition or {}
        payload: JsonObject = {
            "v": TOOL_CHANGE_NOTE_VERSION,
            "epoch": self.epoch,
            "change": self.change,
            "tool": self.tool,
            "source": self.source,
            "description": definition.get("description"),
            "parameters": definition.get("parameters"),
            "listed": self.listed,
            "pinned": self.pinned,
            "detail": self.detail,
        }
        if self.summary is not None:
            payload["summary"] = self.summary
        return TOOL_CHANGE_NOTE_PREFIX + json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        )


def tool_change_from_note(message: ChatMessage) -> ToolChange | None:
    """Parse a ``[tool-change]`` note; ``None`` for other or malformed notes."""

    if not is_tool_change_note(message):
        return None
    content = cast(str, message.content)
    try:
        payload = json.loads(content.removeprefix(TOOL_CHANGE_NOTE_PREFIX))
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("v") != TOOL_CHANGE_NOTE_VERSION:
        return None
    change = payload.get("change")
    tool = payload.get("tool")
    epoch = payload.get("epoch")
    if (
        change not in _TOOL_CHANGE_KINDS
        or not isinstance(tool, str)
        or not tool
        or not isinstance(epoch, str)
        or not epoch
        or not _optional_string(payload.get("source"))
        or not _optional_string(payload.get("detail"))
        or not _optional_string(payload.get("summary"))
        or not isinstance(payload.get("listed"), bool)
        or not isinstance(payload.get("pinned"), bool)
        or (change == "on_demand" and payload.get("summary") is None)
    ):
        return None
    description = payload.get("description")
    parameters = payload.get("parameters")
    definition: JsonObject | None = None
    if change in _DEFINED_KINDS:
        if not isinstance(description, str) or not isinstance(parameters, dict):
            return None
        definition = {"name": tool, "description": description, "parameters": parameters}
    return ToolChange(
        change=cast(ToolChangeKind, change),
        tool=tool,
        epoch=epoch,
        source=payload.get("source"),
        definition=definition,
        listed=payload["listed"],
        pinned=payload["pinned"],
        detail=payload.get("detail"),
        summary=payload.get("summary"),
    )


def render_tool_change(change: ToolChange) -> str:
    """Return the System Reminder text announcing ``change`` to the Model.

    A ``loaded`` change is silent: the ``load_tools`` result already showed
    the definition, so it renders as ``""`` and no request carries it.
    """

    name = model_tool_name(change.tool)
    if change.change == "loaded":
        return ""
    if change.change == "on_demand":
        loader = model_tool_name(LOAD_TOOLS_TOOL_NAME)
        summary = (change.summary or "").strip()
        if not summary:
            return _ON_DEMAND_UNSUMMARIZED.format(name=name, loader=loader)
        if not summary.endswith(_SENTENCE_ENDS):
            summary += "."
        return _ON_DEMAND.format(name=name, summary=summary, loader=loader)
    if change.change == "removed":
        return _REMOVED.format(name=name)
    if change.change == "added" and change.pinned:
        return _AVAILABLE_AGAIN.format(name=name)
    if change.change == "added" and change.listed:
        return _ADDED_LISTED.format(name=name)
    definition = change.definition or {}
    described = _DEFINITION.format(
        description=definition.get("description", ""),
        schema=compact_schema(definition.get("parameters", {})),
    )
    if change.change == "added":
        return _ADDED_UNLISTED.format(name=name) + described
    detail = _CHANGE_DETAIL.format(detail=change.detail) if change.detail else ""
    changed = _CHANGED_PINNED if change.pinned else _CHANGED
    return changed.format(name=name) + detail + described


def compact_schema(parameters: Any) -> str:
    """Serialize a parameter schema compactly, keeping its key order."""

    return json.dumps(parameters, separators=(",", ":"), ensure_ascii=False)


def _json_copy(value: Any) -> Any:
    """Return *value* as it reads back from its compact JSON, key order kept.

    Definitions the Model is told about pass through here before their first
    use, so the request that pins or announces them sends exactly the bytes
    every later request parses from the persisted pin or note.
    """

    return json.loads(compact_schema(value))


def definition_source(definition: Mapping[str, Any]) -> str:
    """Fingerprint one registered Provider definition, before route projection."""

    payload = json.dumps(definition, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LiveToolCatalog:
    """The Tools a Session's Agent may use now, measured on the Run's primary route.

    ``usable`` names every Tool the Agent may call now, ready or not: a Tool the
    Model knows that is missing here was removed. ``offered`` holds the
    definitions a new prompt epoch would pin: the ready Tools after the route
    gates, in request order. ``sources`` fingerprints each usable Tool's
    registered definition. ``session_tool_grants`` are the Session's current
    grants of session-scoped Tools. ``change_notes`` holds the
    ``definition_change_note`` of each usable Tool that declares one.
    ``on_demand`` maps each offered Tool the Agent's Tool list leaves to load
    on demand to its summary, in the order the System Prompt lists them.
    """

    usable: frozenset[str]
    offered: tuple[JsonObject, ...]
    sources: Mapping[str, str]
    session_tool_grants: tuple[str, ...] = ()
    change_notes: Mapping[str, ToolDefinitionChangeNote] = field(default_factory=dict)
    on_demand: Mapping[str, str] = field(default_factory=dict)

    @cached_property
    def offered_by_name(self) -> dict[str, JsonObject]:
        return {str(definition["name"]): definition for definition in self.offered}


@dataclass(frozen=True)
class ToolEpochPin:
    """The Provider Tool definitions one prompt epoch sends on every request.

    ``definitions`` are the exact post-route definitions of the epoch's first
    request, in order, under registry names. They persist as one compact JSON
    string that keeps each definition's key order, because the Session store
    sorts the keys of the JSON objects it stores; the first request sends them
    as they read back, so every request of the epoch sends the same bytes.
    ``sources`` fingerprints each Tool's registered definition so a later
    schema change is recognized. ``epoch`` keys the Tool-change notes of this
    epoch; notes of another epoch are ignored. ``on_demand`` holds the
    ``(registry name, summary)`` of each On-demand Tool the System Prompt of
    this epoch lists instead, sorted by Model-facing name.
    """

    epoch: str
    definitions: tuple[JsonObject, ...]
    sources: Mapping[str, str] = field(default_factory=dict)
    on_demand: tuple[tuple[str, str], ...] = ()

    @classmethod
    def start(cls, catalog: LiveToolCatalog, *, keep: Collection[str] = ()) -> ToolEpochPin:
        """Start a new epoch that lists the Tools *catalog* offers now.

        The On-demand Tools stay out of the Tool list, except those in *keep*
        (the Tools the Agent called in the epoch a Compaction ended).
        """

        left_out = {name for name in catalog.on_demand if name not in keep}
        offered = [
            definition for definition in catalog.offered if definition["name"] not in left_out
        ]
        names = {str(definition["name"]) for definition in offered}
        return cls(
            epoch=uuid.uuid4().hex,
            definitions=tuple(_json_copy(offered)),
            sources={name: catalog.sources[name] for name in names if name in catalog.sources},
            on_demand=tuple(
                sorted(
                    ((name, catalog.on_demand[name]) for name in left_out),
                    key=lambda entry: (model_tool_name(entry[0]), entry[0]),
                )
            ),
        )

    @cached_property
    def names(self) -> tuple[str, ...]:
        return tuple(str(definition["name"]) for definition in self.definitions)

    def to_payload(self) -> JsonObject:
        """Return the persisted pin value."""

        payload: JsonObject = {
            "v": TOOL_EPOCH_PIN_VERSION,
            "epoch": self.epoch,
            "definitions": compact_schema(list(self.definitions)),
            "sources": dict(self.sources),
        }
        if self.on_demand:
            payload["on_demand"] = [[name, summary] for name, summary in self.on_demand]
        return payload

    @classmethod
    def from_payload(cls, payload: object) -> ToolEpochPin | None:
        """Parse a persisted pin; ``None`` when it is unreadable or of another version."""

        if not isinstance(payload, dict) or payload.get("v") != TOOL_EPOCH_PIN_VERSION:
            return None
        epoch = payload.get("epoch")
        serialized = payload.get("definitions")
        sources = payload.get("sources")
        if not isinstance(epoch, str) or not epoch:
            return None
        if not isinstance(serialized, str) or not isinstance(sources, dict):
            return None
        try:
            definitions = json.loads(serialized)
        except ValueError:
            return None
        if not isinstance(definitions, list):
            return None
        names: set[str] = set()
        for definition in definitions:
            if not isinstance(definition, dict):
                return None
            name = definition.get("name")
            if (
                not isinstance(name, str)
                or not name
                or name in names
                or not isinstance(definition.get("description"), str)
                or not isinstance(definition.get("parameters"), dict)
            ):
                return None
            names.add(name)
        if not all(
            isinstance(name, str) and isinstance(source, str) for name, source in sources.items()
        ):
            return None
        on_demand: list[tuple[str, str]] = []
        entries = payload.get("on_demand", [])
        if not isinstance(entries, list):
            return None
        for entry in entries:
            if (
                not isinstance(entry, list | tuple)
                or len(entry) != 2
                or not isinstance(entry[0], str)
                or not entry[0]
                or not isinstance(entry[1], str)
                or entry[0] in names
            ):
                return None
            names.add(entry[0])
            on_demand.append((entry[0], entry[1]))
        return cls(
            epoch=epoch,
            definitions=tuple(definitions),
            sources=dict(sources),
            on_demand=tuple(on_demand),
        )


@dataclass(frozen=True)
class _KnownTool:
    """One Tool as the Model knows it in the current epoch."""

    available: bool
    # The definition the Model was last told to use; ``None`` for an
    # On-demand Tool known by name only (not loaded in this epoch).
    definition: JsonObject | None
    source: str | None


@dataclass(frozen=True)
class ToolEpochView:
    """What the Model knows about its Tools: the epoch's pin plus its announced changes."""

    pin: ToolEpochPin
    changes: tuple[ToolChange, ...] = ()

    @classmethod
    def fold(cls, pin: ToolEpochPin, messages: Iterable[ChatMessage]) -> ToolEpochView:
        """Collect the Tool changes *messages* announced in *pin*'s epoch, in order."""

        changes = tuple(
            change
            for message in messages
            if (change := tool_change_from_note(message)) is not None and change.epoch == pin.epoch
        )
        return cls(pin=pin, changes=changes)

    def with_changes(self, changes: Iterable[ToolChange]) -> ToolEpochView:
        """Return this view after announcing *changes*."""

        return replace(self, changes=(*self.changes, *changes))

    @cached_property
    def _known(self) -> dict[str, _KnownTool]:
        known = {
            str(definition["name"]): _KnownTool(
                available=True,
                definition=definition,
                source=self.pin.sources.get(str(definition["name"])),
            )
            for definition in self.pin.definitions
        }
        for name, _summary in self.pin.on_demand:
            known.setdefault(name, _KnownTool(available=True, definition=None, source=None))
        for change in self.changes:
            current = known.get(change.tool)
            if change.change == "removed":
                if current is not None:
                    known[change.tool] = replace(current, available=False)
            elif change.change == "on_demand":
                known[change.tool] = _KnownTool(available=True, definition=None, source=None)
            elif change.definition is not None:
                known[change.tool] = _KnownTool(
                    available=(
                        True
                        if change.change in ("added", "loaded") or current is None
                        else current.available
                    ),
                    definition=change.definition,
                    source=change.source,
                )
        return known

    @cached_property
    def known_names(self) -> tuple[str, ...]:
        """Tools the Model was told about in this epoch, also those announced as removed."""

        return tuple(self._known)

    @cached_property
    def allowed_names(self) -> tuple[str, ...]:
        """Tools the Model may call: pinned and announced Tools not announced as removed."""

        return tuple(name for name, known in self._known.items() if known.available)

    @cached_property
    def removed_names(self) -> frozenset[str]:
        """Tools announced as removed and not enabled again."""

        return frozenset(name for name, known in self._known.items() if not known.available)

    @cached_property
    def announced_names(self) -> tuple[str, ...]:
        """Available Tools the pin does not list: announced additions and On-demand Tools."""

        pinned = set(self.pin.names)
        return tuple(name for name in self.allowed_names if name not in pinned)

    @cached_property
    def loadable_names(self) -> tuple[str, ...]:
        """Available On-demand Tools the Model knows by name only: what ``load_tools`` loads."""

        return tuple(
            name
            for name, known in self._known.items()
            if known.available and known.definition is None
        )

    def definitions(self) -> list[JsonObject]:
        """The definitions dispatch validates against: what the Model was last told."""

        return [
            definition
            for name in self.allowed_names
            if (definition := self._known[name].definition) is not None
        ]

    def request_tools(self, *, list_announced: bool) -> list[JsonObject]:
        """Return the request's Tool list: the pin, plus announced additions when listed.

        A route that drops calls to Tools outside the request list, and every
        fallback route, lists each Tool announced as added or loaded in this
        epoch, in order of its first announcement, with the definition the Model
        was last told to use: that of its latest addition, load or change.
        """

        tools = [dict(definition) for definition in self.pin.definitions]
        if not list_announced:
            return tools
        pinned = set(self.pin.names)
        added = dict.fromkeys(
            change.tool
            for change in self.changes
            if change.change in ("added", "loaded")
            and change.tool not in pinned
            and change.definition is not None
        )
        return [
            *tools,
            *(
                dict(definition)
                for name in added
                if (definition := self._known[name].definition) is not None
            ),
        ]

    def plan(
        self, catalog: LiveToolCatalog, *, unlisted_tool_calls: bool
    ) -> tuple[ToolChange, ...]:
        """Return the changes that bring the Model's knowledge up to *catalog*.

        A Tool the Model may call that is no longer usable is removed; readiness
        alone never removes one, and a route gate never removes a Tool the Model
        already knows. A Tool the Model may not call is added once it is usable,
        ready and passes the route gates: a pinned Tool is available again, any
        other Tool is announced with its definition, listed in the request when
        the route (``unlisted_tool_calls`` false) drops calls to unlisted Tools.
        A known Tool whose registered definition and parameters both changed is
        announced as changed; a change that leaves the parameters as they are is
        announced only when the Tool's ``definition_change_note`` returns text,
        which the note carries as its detail.

        An On-demand Tool the Model knows by name only is removed like any
        other; while it stays usable nothing is announced (its definition
        reaches the Model when it is loaded), unless the Agent stopped loading
        it on demand: then it is added with its definition. A Tool that becomes
        usable as an On-demand Tool is announced by name and summary
        (``on_demand``), for the Model to load.
        """

        known = self._known
        pinned = set(self.pin.names)
        names = list(dict.fromkeys([*known, *catalog.offered_by_name]))
        planned: list[ToolChange] = []
        for name in names:
            current = known.get(name)
            offered = catalog.offered_by_name.get(name)
            source = catalog.sources.get(name)
            if current is not None and current.available:
                if name not in catalog.usable:
                    planned.append(ToolChange(change="removed", tool=name, epoch=self.pin.epoch))
                elif current.definition is None:
                    if offered is not None and name not in catalog.on_demand:
                        planned.append(self._added(name, offered, source, unlisted_tool_calls))
                elif offered is not None and (
                    changed := self._changed(
                        name, current, offered, source, catalog, pinned=name in pinned
                    )
                ):
                    planned.append(changed)
                continue
            if offered is None:
                continue
            if name in pinned:
                assert current is not None
                planned.append(
                    ToolChange(
                        change="added",
                        tool=name,
                        epoch=self.pin.epoch,
                        source=current.source,
                        definition=current.definition,
                        listed=True,
                        pinned=True,
                    )
                )
                if changed := self._changed(name, current, offered, source, catalog, pinned=True):
                    planned.append(changed)
                continue
            if name in catalog.on_demand:
                planned.append(
                    ToolChange(
                        change="on_demand",
                        tool=name,
                        epoch=self.pin.epoch,
                        summary=catalog.on_demand[name],
                    )
                )
                continue
            planned.append(self._added(name, offered, source, unlisted_tool_calls))
        return tuple(planned)

    def _added(
        self, name: str, offered: JsonObject, source: str | None, unlisted_tool_calls: bool
    ) -> ToolChange:
        """Return the note announcing *name* with its definition, listed when the route must."""

        return ToolChange(
            change="added",
            tool=name,
            epoch=self.pin.epoch,
            source=source,
            definition=_model_definition(offered),
            listed=not unlisted_tool_calls,
        )

    def loads(self, catalog: LiveToolCatalog) -> dict[str, ToolChange]:
        """Return the ``loaded`` change a ``load_tools`` call records, per loadable Tool.

        Covers the Tools the Model knows by name only that *catalog* offers
        now, each with its definition as this route shows it.
        """

        return {
            name: ToolChange(
                change="loaded",
                tool=name,
                epoch=self.pin.epoch,
                source=catalog.sources.get(name),
                definition=_model_definition(offered),
            )
            for name in self.loadable_names
            if (offered := catalog.offered_by_name.get(name)) is not None
        }

    def _changed(
        self,
        name: str,
        known: _KnownTool,
        offered: JsonObject,
        source: str | None,
        catalog: LiveToolCatalog,
        *,
        pinned: bool,
    ) -> ToolChange | None:
        """Return the ``changed`` note for *known* now registered as *offered*, if any."""

        if source is None or source == known.source or known.definition is None:
            return None
        definition = _model_definition(offered)
        detail = _change_detail(name, catalog.change_notes.get(name), known.definition, definition)
        if detail is None and offered.get("parameters") == known.definition.get("parameters"):
            return None
        return ToolChange(
            change="changed",
            tool=name,
            epoch=self.pin.epoch,
            source=source,
            definition=definition,
            listed=pinned,
            pinned=pinned,
            detail=detail,
        )


def without_other_epoch_tool_changes(
    messages: Iterable[ChatMessage], epoch: str
) -> list[ChatMessage]:
    """Drop Tool-change notes that do not belong to *epoch*.

    Such notes describe another Agent's or an earlier epoch's Tools (after a
    Takeover, a cross-scope move or a lost pin); the current pin already lists
    what they announced.
    """

    return [
        message
        for message in messages
        if not is_tool_change_note(message)
        or ((change := tool_change_from_note(message)) is not None and change.epoch == epoch)
    ]


def called_tool_names(messages: Iterable[ChatMessage]) -> frozenset[str]:
    """Return the Tools Assistant turns of the current epoch called and dispatch accepted.

    Only calls after the last Compaction checkpoint count; a call rejected
    before it ran (unknown, not allowed or removed Tool) does not.
    """

    called: set[str] = set()
    for message in messages:
        if message.role == "compaction_checkpoint":
            called.clear()
        elif message.role == "assistant":
            called.update(call.name for call in message.tool_calls or () if call.rejection is None)
    return frozenset(called)


def _change_detail(
    name: str,
    change_note: ToolDefinitionChangeNote | None,
    known: JsonObject,
    current: JsonObject,
) -> str | None:
    """Ask a Tool what changed between two of its definitions; ``None`` when silent."""

    if change_note is None:
        return None
    try:
        detail = change_note(copy.deepcopy(known), copy.deepcopy(current))
    except Exception:
        _LOGGER.warning("definition_change_note of Tool %s failed", name, exc_info=True)
        return None
    if not isinstance(detail, str) or not detail.strip():
        return None
    return detail.strip()


def _model_definition(definition: JsonObject) -> JsonObject:
    return cast(
        JsonObject,
        _json_copy(
            {
                "name": definition["name"],
                "description": definition.get("description", ""),
                "parameters": definition.get("parameters", {}),
            }
        ),
    )


def _optional_string(value: object) -> bool:
    return value is None or isinstance(value, str)
