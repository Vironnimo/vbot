"""Prompt-epoch thinking effort: the effort the System Prompt shows and the changes since.

The runtime block of the System Prompt names the Agent's thinking effort. So that a
changed effort leaves the System Prompt unchanged, the epoch's first request pins
the effort it used (:class:`EffortPin`), and the runtime block shows that value for
the whole epoch; a successful Compaction starts the next epoch with the effort then
in effect. The Model is not told about a change.

A change is persisted as one ``[effort-change]`` note at the request boundary where
it takes effect, and every later request uses the effort of the epoch's latest note.
The note renders as an effort update at its place in the request history
(``core.providers.effort_updates``), so a route that can switch the effort
mid-conversation keeps its prompt cache; every other route sends the effort now in
effect as the request's effort.

A change between Runs takes effect at the next Run's first request. A change during
a Run takes effect at its next request, except one that turns reasoning on or off
(to or from ``none`` or the provider default): Models reject or ignore that inside a
Tool loop, so it waits for the next Run.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, cast

from core.chat.messages import ChatMessage, JsonObject
from core.prompts.pinned_context import PINNED_THINKING_EFFORT_SLOT
from core.providers.reasoning import normalize_thinking_effort
from core.sessions import EFFORT_CHANGE_NOTE_PREFIX, is_effort_change_note

if TYPE_CHECKING:
    from core.sessions import ChatSessionManager, SessionAddress

EFFORT_PIN_VERSION = 1
EFFORT_CHANGE_NOTE_VERSION = 1

# Efforts that may leave a Model without Reasoning: off, and the provider default.
_REASONING_SWITCHES = frozenset({"", "none"})


def thinking_effort(agent: object) -> str:
    """Return *agent*'s thinking effort as a canonical value (``""``: provider default)."""

    return normalize_thinking_effort(getattr(agent, "thinking_effort", None))


@dataclass(frozen=True)
class EffortPin:
    """The thinking effort the System Prompt shows for one prompt epoch.

    ``epoch`` keys the change notes of this epoch; notes of another epoch are
    ignored.
    """

    epoch: str
    effort: str

    @classmethod
    def start(cls, effort: str) -> EffortPin:
        """Start a new epoch at *effort*."""

        return cls(epoch=uuid.uuid4().hex, effort=effort)

    def to_payload(self) -> JsonObject:
        """Return the persisted pin value."""

        return {"v": EFFORT_PIN_VERSION, "epoch": self.epoch, "effort": self.effort}

    @classmethod
    def from_payload(cls, payload: object) -> EffortPin | None:
        """Parse a persisted pin; ``None`` when it is unreadable or of another version."""

        if not isinstance(payload, dict) or payload.get("v") != EFFORT_PIN_VERSION:
            return None
        epoch = payload.get("epoch")
        effort = payload.get("effort")
        if not isinstance(epoch, str) or not epoch or not isinstance(effort, str):
            return None
        return cls(epoch=epoch, effort=normalize_thinking_effort(effort))


@dataclass(frozen=True)
class EffortChange:
    """The thinking effort in effect from one point of a prompt epoch on."""

    epoch: str
    effort: str

    def note_content(self) -> str:
        """Return the note content that persists this change."""

        payload: JsonObject = {
            "v": EFFORT_CHANGE_NOTE_VERSION,
            "epoch": self.epoch,
            "effort": self.effort,
        }
        return EFFORT_CHANGE_NOTE_PREFIX + json.dumps(payload, separators=(",", ":"))


def effort_change_from_note(message: ChatMessage) -> EffortChange | None:
    """Parse an ``[effort-change]`` note; ``None`` for other or malformed notes."""

    if not is_effort_change_note(message):
        return None
    content = cast(str, message.content)
    try:
        payload = json.loads(content.removeprefix(EFFORT_CHANGE_NOTE_PREFIX))
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("v") != EFFORT_CHANGE_NOTE_VERSION:
        return None
    epoch = payload.get("epoch")
    effort = payload.get("effort")
    if not isinstance(epoch, str) or not epoch or not isinstance(effort, str):
        return None
    return EffortChange(epoch=epoch, effort=normalize_thinking_effort(effort))


def without_other_epoch_effort_changes(
    messages: Iterable[ChatMessage], epoch: str
) -> list[ChatMessage]:
    """Drop effort change notes that do not belong to *epoch* (or cannot be read).

    Such notes changed another Agent's or an earlier epoch's effort (after a
    Takeover, a cross-scope move or a lost pin); the current pin starts from the
    effort in effect then.
    """

    return [
        message
        for message in messages
        if not is_effort_change_note(message)
        or getattr(effort_change_from_note(message), "epoch", None) == epoch
    ]


@dataclass(frozen=True)
class EffortState:
    """The thinking effort of one Session's requests: the epoch's pin and the effort now."""

    pin: EffortPin
    current: str

    @classmethod
    def fold(cls, pin: EffortPin, messages: Iterable[ChatMessage]) -> EffortState:
        """Return the effort in effect after *pin* and this epoch's notes in *messages*."""

        current = pin.effort
        for message in messages:
            change = effort_change_from_note(message)
            if change is not None and change.epoch == pin.epoch:
                current = change.effort
        return cls(pin=pin, current=current)

    def plan(self, live: str, *, within_run: bool) -> EffortChange | None:
        """Return the change that brings the requests to the *live* effort, if any.

        ``within_run`` is set at a request boundary after the Run's first
        request: a change that turns Reasoning on or off waits for the next Run.
        """

        if live == self.current:
            return None
        if within_run and (live in _REASONING_SWITCHES or self.current in _REASONING_SWITCHES):
            return None
        return EffortChange(epoch=self.pin.epoch, effort=live)

    def adopt(self, change: EffortChange) -> EffortState:
        """Return this state after *change*."""

        return replace(self, current=change.effort)


def ensure_effort_pin(
    sessions: ChatSessionManager, address: SessionAddress, effort: str
) -> EffortPin:
    """Return the Session's effort pin, pinning *effort* for an epoch that has none.

    A concurrent request may have pinned a readable value meanwhile; every
    request then uses it.
    """

    stored = EffortPin.from_payload(sessions.prompt_pin(address, PINNED_THINKING_EFFORT_SLOT))
    if stored is not None:
        return stored
    pin = EffortPin.start(effort)
    pinned = sessions.ensure_prompt_pin(
        address,
        PINNED_THINKING_EFFORT_SLOT,
        pin.to_payload(),
        lambda current: EffortPin.from_payload(current) is not None,
    )
    return EffortPin.from_payload(pinned) or pin
