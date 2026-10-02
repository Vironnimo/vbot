"""Reasoning dialects: how a planned reasoning decision is spelled on the wire.

``ReasoningWire.plan`` decides what the next request asks of a Model's
reasoning (a :class:`ReasoningIntent`). A dialect only spells that decision:
:func:`render_reasoning` writes the request fields, and
:func:`describe_reasoning` reports the decision the rendered request actually
carries, so ``/status`` and the swallowed-effort diagnostics describe exactly
what was sent. A dialect that cannot express part of an intent (for example a
budget on an effort-only wire) degrades it, and its description says so.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.providers.reasoning import (
    REASONING_INTENT_BUDGET,
    REASONING_INTENT_DEFAULT,
    REASONING_INTENT_EFFORT,
    REASONING_INTENT_OFF,
    REASONING_INTENT_ON,
    ReasoningIntent,
)
from core.providers.wire_profile import ReasoningDialect, ReasoningWire

__all__ = ["describe_reasoning", "render_reasoning"]

_SENDS_NOTHING = ReasoningIntent(REASONING_INTENT_DEFAULT)
_ACTIVE_KINDS = (REASONING_INTENT_EFFORT, REASONING_INTENT_ON, REASONING_INTENT_BUDGET)


@dataclass(frozen=True)
class _Dialect:
    render: Callable[[ReasoningWire, ReasoningIntent, dict[str, Any]], None]
    describe: Callable[[ReasoningWire, ReasoningIntent], ReasoningIntent]


def render_reasoning(wire: ReasoningWire, intent: ReasoningIntent, payload: dict[str, Any]) -> None:
    """Write ``intent`` onto ``payload`` in the profile's reasoning dialect."""

    _dialect(wire.dialect).render(wire, intent, payload)


def describe_reasoning(wire: ReasoningWire, intent: ReasoningIntent) -> ReasoningIntent:
    """Return the reasoning decision a request rendered from ``intent`` carries."""

    return _dialect(wire.dialect).describe(wire, intent)


def _dialect(name: ReasoningDialect) -> _Dialect:
    dialect = _DIALECTS.get(name)
    if dialect is None:
        raise NotImplementedError(f"reasoning dialect {name!r} has no renderer on this wire")
    return dialect


# -- none ---------------------------------------------------------------------


def _render_nothing(wire: ReasoningWire, intent: ReasoningIntent, payload: dict[str, Any]) -> None:
    del wire, intent, payload


def _describe_nothing(wire: ReasoningWire, intent: ReasoningIntent) -> ReasoningIntent:
    del wire, intent
    return _SENDS_NOTHING


# -- reasoning_effort -----------------------------------------------------------
# Top-level ``reasoning_effort``. The wire has no toggle or budget field, so an
# ``on``/``budget`` decision degrades to its snapped level, and off is spelled
# only as the ``none`` level.


def _effort_level(intent: ReasoningIntent) -> str | None:
    if intent.kind in _ACTIVE_KINDS or intent.kind == REASONING_INTENT_OFF:
        return intent.effort_level
    return None


def _render_reasoning_effort(
    wire: ReasoningWire, intent: ReasoningIntent, payload: dict[str, Any]
) -> None:
    del wire
    level = _effort_level(intent)
    if level is not None:
        payload["reasoning_effort"] = level


def _describe_reasoning_effort(wire: ReasoningWire, intent: ReasoningIntent) -> ReasoningIntent:
    del wire
    level = _effort_level(intent)
    if level is None:
        return _SENDS_NOTHING
    if intent.kind == REASONING_INTENT_OFF:
        return ReasoningIntent(REASONING_INTENT_OFF, effort_level=level)
    return ReasoningIntent(REASONING_INTENT_EFFORT, effort_level=level)


# -- nous_reasoning -------------------------------------------------------------
# ``reasoning: {enabled: true[, effort]}``; disabled reasoning is spelled by
# omission, and budgets degrade to the snapped level.


def _render_nous_reasoning(
    wire: ReasoningWire, intent: ReasoningIntent, payload: dict[str, Any]
) -> None:
    del wire
    if intent.kind not in _ACTIVE_KINDS:
        return
    reasoning: dict[str, Any] = {"enabled": True}
    if intent.effort_level is not None:
        reasoning["effort"] = intent.effort_level
    payload["reasoning"] = reasoning


def _describe_nous_reasoning(wire: ReasoningWire, intent: ReasoningIntent) -> ReasoningIntent:
    del wire
    if intent.kind not in _ACTIVE_KINDS:
        return _SENDS_NOTHING
    if intent.effort_level is None:
        return ReasoningIntent(REASONING_INTENT_ON)
    return ReasoningIntent(REASONING_INTENT_EFFORT, effort_level=intent.effort_level)


_DIALECTS: dict[str, _Dialect] = {
    "none": _Dialect(_render_nothing, _describe_nothing),
    "reasoning_effort": _Dialect(_render_reasoning_effort, _describe_reasoning_effort),
    "nous_reasoning": _Dialect(_render_nous_reasoning, _describe_nous_reasoning),
}
