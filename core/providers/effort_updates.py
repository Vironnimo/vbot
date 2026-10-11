"""Effort updates: a thinking effort change at its place in the request history.

A request-level effort change gives a request a new prompt prefix on most Providers,
which costs the whole prompt cache. Some wires can instead change the effort
mid-conversation (Anthropic Messages' per-message ``output_config``, OpenAI Responses'
``configuration_update`` item): the request keeps the effort of its prompt prefix and an
update at the point of the change switches the Model to the new effort from there on.

Chat marks every such point of a request history with an effort update message
(:func:`effort_update_message`, carrying the vBot thinking effort in effect from there
on) and sends the effort it started from as the request keyword argument
:data:`EFFORT_BASE_KWARG`, together with ``thinking_effort``, the effort now in effect.
Only a route whose Adapter renders updates (``ProviderAdapter.effort_updates``) receives
them; Chat removes them for every other route, which gets ``thinking_effort`` alone.

An Adapter that renders updates plans them with :func:`apply_effort_updates` (built on
:func:`plan_effort_updates`), which keeps each update its wire can express and starts
the request from a later effort where it cannot. Every later request of the
conversation makes the same choices, so the prefix stays the same.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.providers.reasoning import REASONING_INTENT_EFFORT

if TYPE_CHECKING:
    from core.providers.wire_profile import ReasoningWire

EFFORT_UPDATE_ROLE = "effort_update"
"""The role of a request-only effort update message; no Provider wire knows it."""

EFFORT_BASE_KWARG = "effort_base"
"""The request keyword argument naming the effort a history with updates started from."""

type WireLevel = Callable[[str], str | None]
"""Return the wire value a route can switch to mid-conversation for a vBot thinking
effort, or ``None`` when the route cannot switch to or from that effort that way."""


def effort_levels(wire: ReasoningWire, *, unexpressed: Collection[str] = ()) -> WireLevel:
    """Return the :data:`WireLevel` of a wire that switches between effort levels.

    An effort the wire profile plans as a level reaches that level; every other
    plan (off, a budget, Reasoning without a level, nothing) and each level in
    *unexpressed* cannot be switched to or from mid-conversation.
    """

    def level(effort: str) -> str | None:
        intent = wire.plan(effort)
        if intent.kind != REASONING_INTENT_EFFORT or not intent.effort_level:
            return None
        return None if intent.effort_level in unexpressed else intent.effort_level

    return level


def effort_update_message(effort: str) -> dict[str, Any]:
    """Return the request message that switches to *effort* (``""``: provider default)."""

    return {"role": EFFORT_UPDATE_ROLE, "effort": effort}


def is_effort_update(message: Mapping[str, Any]) -> bool:
    """Whether *message* is an effort update message."""

    return message.get("role") == EFFORT_UPDATE_ROLE


def without_effort_updates(messages: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return *messages* without their effort updates."""

    return [message for message in messages if not is_effort_update(message)]


@dataclass(frozen=True)
class EffortUpdatePlan:
    """How one request carries its history's effort changes.

    ``request_effort`` is the vBot thinking effort the request-level setting
    renders. ``messages`` keeps every update the wire expresses, its ``effort``
    replaced with the wire value, and no other update.
    """

    request_effort: str
    messages: list[dict[str, Any]]


def plan_effort_updates(
    messages: Iterable[dict[str, Any]], base: str, wire_level: WireLevel
) -> EffortUpdatePlan:
    """Plan how a request whose history started at *base* carries its effort updates.

    An update is expressed when the wire can switch both from the effort before it
    and to its own effort. The request starts from the effort after the last update
    that is not expressed (or *base*), and drops the updates up to it: those
    requests start from another prefix anyway. Of the later updates, one that does
    not change the wire value is dropped, and of two updates next to each other
    only the second stays (OpenAI rejects adjacent updates).
    """

    history = list(messages)
    efforts = [base]
    efforts.extend(_update_effort(message) for message in history if is_effort_update(message))
    levels = [wire_level(effort) for effort in efforts]
    start = 0
    for index in range(1, len(efforts)):
        if levels[index] is None or levels[index - 1] is None:
            start = index
    planned: list[dict[str, Any]] = []
    update_index = 0
    current = levels[start]
    for message in history:
        if not is_effort_update(message):
            planned.append(message)
            continue
        update_index += 1
        level = levels[update_index]
        if update_index <= start or level is None:
            continue
        if planned and is_effort_update(planned[-1]):
            planned.pop()
            current = _previous_level(planned, levels[start])
        if level != current:
            planned.append(effort_update_message(level))
            current = level
    return EffortUpdatePlan(request_effort=efforts[start], messages=planned)


def apply_effort_updates(
    messages: list[dict[str, Any]],
    request_kwargs: dict[str, Any],
    wire_level: WireLevel | None,
) -> list[dict[str, Any]]:
    """Plan a request's effort updates; consumes :data:`EFFORT_BASE_KWARG`.

    *wire_level* is ``None`` when the Model takes no effort updates now (a
    learned rejection can turn them off between two attempts of one request):
    the updates are dropped and ``thinking_effort``, the effort now in effect,
    stays the request effort. Otherwise the planned request effort replaces
    ``thinking_effort`` and the returned messages carry the planned updates.
    """

    base = request_kwargs.pop(EFFORT_BASE_KWARG, None)
    if not any(is_effort_update(message) for message in messages):
        return messages
    if not isinstance(base, str) or wire_level is None:
        return without_effort_updates(messages)
    plan = plan_effort_updates(messages, base, wire_level)
    request_kwargs["thinking_effort"] = plan.request_effort
    return plan.messages


def _update_effort(message: Mapping[str, Any]) -> str:
    effort = message.get("effort")
    return effort if isinstance(effort, str) else ""


def _previous_level(planned: list[dict[str, Any]], start_level: str | None) -> str | None:
    """Return the wire value in effect at the end of *planned*."""

    for message in reversed(planned):
        if is_effort_update(message):
            return _update_effort(message)
    return start_level
