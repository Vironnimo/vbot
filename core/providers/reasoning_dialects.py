"""Reasoning dialects: how a planned reasoning decision is spelled on the wire.

``ReasoningWire.plan`` decides what the next request asks of a Model's
reasoning (a :class:`ReasoningIntent`). A dialect only spells that decision:
:func:`render_reasoning` writes the request fields, and
:func:`describe_reasoning` reports the decision the rendered request actually
carries, so ``/status`` and the swallowed-effort diagnostics describe exactly
what was sent. A dialect that cannot express part of an intent (for example a
budget on an effort-only wire) degrades it, and its description says so.
:func:`dialect_request_fields` names the request fields a dialect writes, so a
codec can keep caller-supplied copies of them away from a Model that cannot
reason.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.models.models import REASONING_CONTROL_LEVELS, REASONING_CONTROL_ON_OFF
from core.providers._responses_values import REASONING_ENCRYPTED_CONTENT_INCLUDE
from core.providers.reasoning import (
    REASONING_INTENT_BUDGET,
    REASONING_INTENT_DEFAULT,
    REASONING_INTENT_EFFORT,
    REASONING_INTENT_OFF,
    REASONING_INTENT_ON,
    ReasoningIntent,
)
from core.providers.wire_profile import ReasoningDialect, ReasoningWire
from core.utils.logging import get_logger

__all__ = ["describe_reasoning", "dialect_request_fields", "render_reasoning"]

_LOGGER = get_logger("providers.reasoning_dialects")

_SENDS_NOTHING = ReasoningIntent(REASONING_INTENT_DEFAULT)
_ACTIVE_KINDS = (REASONING_INTENT_EFFORT, REASONING_INTENT_ON, REASONING_INTENT_BUDGET)

_Render = Callable[[ReasoningWire, ReasoningIntent, dict[str, Any], int | None], None]


@dataclass(frozen=True)
class _Dialect:
    render: _Render
    describe: Callable[[ReasoningWire, ReasoningIntent], ReasoningIntent]
    fields: tuple[str, ...] = ()


def render_reasoning(
    wire: ReasoningWire,
    intent: ReasoningIntent,
    payload: dict[str, Any],
    *,
    output_allowance: int | None = None,
) -> None:
    """Write ``intent`` onto ``payload`` in the profile's reasoning dialect.

    ``output_allowance`` is the request's resolved output-token limit; dialects
    that spell a thinking budget keep it below that limit.
    """

    _dialect(wire.dialect).render(wire, intent, payload, output_allowance)


def describe_reasoning(wire: ReasoningWire, intent: ReasoningIntent) -> ReasoningIntent:
    """Return the reasoning decision a request rendered from ``intent`` carries."""

    return _dialect(wire.dialect).describe(wire, intent)


def dialect_request_fields(dialect: ReasoningDialect) -> tuple[str, ...]:
    """The top-level request fields ``dialect`` may write."""

    return _dialect(dialect).fields


def _dialect(name: ReasoningDialect) -> _Dialect:
    dialect = _DIALECTS.get(name)
    if dialect is None:
        raise NotImplementedError(f"reasoning dialect {name!r} has no renderer on this wire")
    return dialect


def _describe_as_planned(wire: ReasoningWire, intent: ReasoningIntent) -> ReasoningIntent:
    del wire
    return intent


# -- none ---------------------------------------------------------------------


def _render_nothing(
    wire: ReasoningWire,
    intent: ReasoningIntent,
    payload: dict[str, Any],
    output_allowance: int | None,
) -> None:
    del wire, intent, payload, output_allowance


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
    wire: ReasoningWire,
    intent: ReasoningIntent,
    payload: dict[str, Any],
    output_allowance: int | None,
) -> None:
    del wire, output_allowance
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


# -- responses_reasoning ----------------------------------------------------------
# Responses ``reasoning: {effort, summary: "auto"}``. Like ``reasoning_effort``,
# ``on``/``budget`` degrade to their snapped level and off is spelled only as
# the ``none`` level. ``reasoning.options.context`` adds ``reasoning.context``
# to every request (a cross-turn reasoning scope such as ``all_turns``). A Model
# known to reason also asks for its encrypted reasoning items (``include``),
# which stateless replay returns on the next request, even when no effort is
# sent.

_RESPONSES_REASONING_SUMMARY = "auto"


def _render_responses_reasoning(
    wire: ReasoningWire,
    intent: ReasoningIntent,
    payload: dict[str, Any],
    output_allowance: int | None,
) -> None:
    del output_allowance
    level = _effort_level(intent)
    if level is not None:
        payload["reasoning"] = {"effort": level, "summary": _RESPONSES_REASONING_SUMMARY}
    context = wire.options.get("context")
    if isinstance(context, str) and context:
        payload.setdefault("reasoning", {})["context"] = context
    if wire.supported is True:
        include = payload.setdefault("include", [])
        if REASONING_ENCRYPTED_CONTENT_INCLUDE not in include:
            include.append(REASONING_ENCRYPTED_CONTENT_INCLUDE)


# -- openrouter_reasoning ---------------------------------------------------------
# OpenRouter's ``reasoning`` object plus ``include_reasoning: true``. An effort
# (and the level a budget snaps to) is ``reasoning: {effort}``: OpenRouter maps
# an effort onto an upstream token budget itself, so no budget is sent. A plain
# ``on``, and every active decision for an on/off Model, is ``reasoning:
# {enabled: true}``. Off is ``{effort: "none"}`` when the decision carries the
# ``none`` level and ``{enabled: false}`` otherwise; an off request never asks
# for the reasoning text, so a caller's ``include_reasoning`` is removed.


def _openrouter_reasoning(wire: ReasoningWire, intent: ReasoningIntent) -> dict[str, Any] | None:
    """The ``reasoning`` object a decision is spelled as (``None``: nothing is sent)."""

    if intent.kind in _ACTIVE_KINDS:
        if intent.kind == REASONING_INTENT_ON or wire.control == REASONING_CONTROL_ON_OFF:
            return {"enabled": True}
        if intent.effort_level is not None:
            return {"effort": intent.effort_level}
        return None
    if intent.kind == REASONING_INTENT_OFF:
        return {"effort": "none"} if intent.effort_level == "none" else {"enabled": False}
    return None


def _render_openrouter_reasoning(
    wire: ReasoningWire,
    intent: ReasoningIntent,
    payload: dict[str, Any],
    output_allowance: int | None,
) -> None:
    del output_allowance
    reasoning = _openrouter_reasoning(wire, intent)
    if reasoning is None:
        return
    payload["reasoning"] = reasoning
    if intent.kind == REASONING_INTENT_OFF:
        payload.pop("include_reasoning", None)
    else:
        payload["include_reasoning"] = True


def _describe_openrouter_reasoning(wire: ReasoningWire, intent: ReasoningIntent) -> ReasoningIntent:
    reasoning = _openrouter_reasoning(wire, intent)
    if reasoning is None:
        return _SENDS_NOTHING
    if intent.kind == REASONING_INTENT_OFF:
        return ReasoningIntent(REASONING_INTENT_OFF, effort_level=reasoning.get("effort"))
    if "effort" in reasoning:
        return ReasoningIntent(REASONING_INTENT_EFFORT, effort_level=reasoning["effort"])
    return ReasoningIntent(REASONING_INTENT_ON)


# -- nous_reasoning -------------------------------------------------------------
# ``reasoning: {enabled: true[, effort]}``; disabled reasoning is spelled by
# omission, and budgets degrade to the snapped level.


def _render_nous_reasoning(
    wire: ReasoningWire,
    intent: ReasoningIntent,
    payload: dict[str, Any],
    output_allowance: int | None,
) -> None:
    del wire, output_allowance
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


# -- thinking_toggle -------------------------------------------------------------
# ``thinking: {type: enabled|disabled}``: every active decision is the same
# switch (levels and budgets cannot be expressed). ``reasoning.options.keep``
# adds ``keep`` to the enabled switch (Kimi's history retention).


def _thinking_enabled(wire: ReasoningWire) -> dict[str, Any]:
    thinking: dict[str, Any] = {"type": "enabled"}
    keep = wire.options.get("keep")
    if keep is not None:
        thinking["keep"] = keep
    return thinking


def _render_thinking_toggle(
    wire: ReasoningWire,
    intent: ReasoningIntent,
    payload: dict[str, Any],
    output_allowance: int | None,
) -> None:
    del output_allowance
    if intent.kind in _ACTIVE_KINDS:
        payload["thinking"] = _thinking_enabled(wire)
    elif intent.kind == REASONING_INTENT_OFF:
        payload["thinking"] = {"type": "disabled"}


def _describe_thinking_toggle(wire: ReasoningWire, intent: ReasoningIntent) -> ReasoningIntent:
    del wire
    if intent.kind in _ACTIVE_KINDS:
        return ReasoningIntent(REASONING_INTENT_ON)
    if intent.kind == REASONING_INTENT_OFF:
        return ReasoningIntent(REASONING_INTENT_OFF)
    return _SENDS_NOTHING


# -- thinking_toggle_with_effort ---------------------------------------------------
# An active decision with a level is ``reasoning_effort: <level>``; one without
# a level is the plain ``thinking`` switch, and off is ``thinking`` disabled.
# With ``reasoning.options.switch_with_effort: true`` an active decision with a
# level also carries the enabled ``thinking`` switch (wires whose effort does
# not imply thinking).


def _render_thinking_toggle_with_effort(
    wire: ReasoningWire,
    intent: ReasoningIntent,
    payload: dict[str, Any],
    output_allowance: int | None,
) -> None:
    del output_allowance
    if intent.kind in _ACTIVE_KINDS:
        if intent.effort_level is None or wire.options.get("switch_with_effort") is True:
            payload["thinking"] = _thinking_enabled(wire)
        if intent.effort_level is not None:
            payload["reasoning_effort"] = intent.effort_level
    elif intent.kind == REASONING_INTENT_OFF:
        payload["thinking"] = {"type": "disabled"}


def _describe_thinking_toggle_with_effort(
    wire: ReasoningWire, intent: ReasoningIntent
) -> ReasoningIntent:
    del wire
    if intent.kind in _ACTIVE_KINDS:
        if intent.effort_level is not None:
            return ReasoningIntent(REASONING_INTENT_EFFORT, effort_level=intent.effort_level)
        return ReasoningIntent(REASONING_INTENT_ON)
    if intent.kind == REASONING_INTENT_OFF:
        return ReasoningIntent(REASONING_INTENT_OFF)
    return _SENDS_NOTHING


# -- gemini_thinking ---------------------------------------------------------------
# Gemini ``generationConfig.thinkingConfig: {includeThoughts: true, thinkingLevel}``.
# Like ``reasoning_effort``, ``on``/``budget`` degrade to their snapped level and
# a decision without a level sends nothing.


def _render_gemini_thinking(
    wire: ReasoningWire,
    intent: ReasoningIntent,
    payload: dict[str, Any],
    output_allowance: int | None,
) -> None:
    del wire, output_allowance
    level = _effort_level(intent)
    if level is None:
        return
    generation = payload.setdefault("generationConfig", {})
    generation["thinkingConfig"] = {"includeThoughts": True, "thinkingLevel": level}


# -- minimax_split ----------------------------------------------------------------
# MiniMax M2.x reasons on every request and takes no reasoning control;
# ``reasoning_split: true`` only returns the trace as ``reasoning_details``
# instead of inline ``<think>`` text, so it is sent whatever the decision.


def _render_minimax_split(
    wire: ReasoningWire,
    intent: ReasoningIntent,
    payload: dict[str, Any],
    output_allowance: int | None,
) -> None:
    del wire, intent, output_allowance
    payload["reasoning_split"] = True


def _describe_minimax_split(wire: ReasoningWire, intent: ReasoningIntent) -> ReasoningIntent:
    del wire, intent
    return ReasoningIntent(REASONING_INTENT_ON)


# -- minimax_thinking ---------------------------------------------------------------
# MiniMax M3: ``thinking: {type: adaptive}`` for every active decision (no level
# or budget), ``thinking: {type: disabled}`` for off, plus ``reasoning_split``
# whenever the Model reasons (also by default).


def _render_minimax_thinking(
    wire: ReasoningWire,
    intent: ReasoningIntent,
    payload: dict[str, Any],
    output_allowance: int | None,
) -> None:
    del wire, output_allowance
    if intent.kind == REASONING_INTENT_OFF:
        payload["thinking"] = {"type": "disabled"}
        return
    if intent.kind in _ACTIVE_KINDS:
        payload["thinking"] = {"type": "adaptive"}
    payload["reasoning_split"] = True


def _describe_minimax_thinking(wire: ReasoningWire, intent: ReasoningIntent) -> ReasoningIntent:
    del wire
    if intent.kind in _ACTIVE_KINDS:
        return ReasoningIntent(REASONING_INTENT_ON)
    if intent.kind == REASONING_INTENT_OFF:
        return ReasoningIntent(REASONING_INTENT_OFF)
    return _SENDS_NOTHING


# -- anthropic_thinking ---------------------------------------------------------------
# Anthropic Messages ``thinking``: an effort is adaptive thinking (summarized)
# with ``output_config.effort`` above ``minimal``; a budget is ``enabled`` with
# ``budget_tokens``; a plain ``on`` is the minimum budget, skipped when it does
# not fit below the output allowance; off is ``disabled``. With
# ``reasoning.options.adaptive_on: true`` a plain ``on`` is adaptive thinking
# without an effort instead (Models that take neither an effort nor a budget).

_ANTHROPIC_MINIMAL_EFFORT = "minimal"


def _render_anthropic_thinking(
    wire: ReasoningWire,
    intent: ReasoningIntent,
    payload: dict[str, Any],
    output_allowance: int | None,
) -> None:
    if intent.kind == REASONING_INTENT_EFFORT:
        payload["thinking"] = {"type": "adaptive", "display": "summarized"}
        if intent.effort_level != _ANTHROPIC_MINIMAL_EFFORT:
            payload["output_config"] = {"effort": intent.effort_level}
    elif intent.kind == REASONING_INTENT_BUDGET:
        payload["thinking"] = {"type": "enabled", "budget_tokens": intent.budget_tokens}
    elif intent.kind == REASONING_INTENT_ON and wire.options.get("adaptive_on") is True:
        payload["thinking"] = {"type": "adaptive", "display": "summarized"}
    elif intent.kind == REASONING_INTENT_ON:
        budget = wire.budget.minimum
        if output_allowance is not None and output_allowance <= budget:
            _LOGGER.warning(
                "Skipping reasoning: the minimum thinking budget (%d) does not fit "
                "the output allowance (%d)",
                budget,
                output_allowance,
            )
            return
        payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
    elif intent.kind == REASONING_INTENT_OFF:
        payload["thinking"] = {"type": "disabled"}


def _describe_anthropic_thinking(wire: ReasoningWire, intent: ReasoningIntent) -> ReasoningIntent:
    # A budget or plain ``on`` carries only ``budget_tokens``; no level reaches the wire.
    del wire
    if intent.kind == REASONING_INTENT_BUDGET:
        return ReasoningIntent(REASONING_INTENT_BUDGET, budget_tokens=intent.budget_tokens)
    if intent.kind == REASONING_INTENT_ON:
        return ReasoningIntent(REASONING_INTENT_ON)
    return intent


# -- ollama_think -----------------------------------------------------------------
# Ollama's native ``think``: a level string for Models with a level ladder,
# otherwise a Boolean. Off is ``false``; an active decision without a level
# (on/off and budget Models) is ``true``.


def _ollama_think(wire: ReasoningWire, intent: ReasoningIntent) -> bool | str | None:
    if intent.kind == REASONING_INTENT_OFF:
        return False
    if intent.kind not in _ACTIVE_KINDS:
        return None
    if (
        intent.kind == REASONING_INTENT_EFFORT
        and wire.control == REASONING_CONTROL_LEVELS
        and intent.effort_level is not None
    ):
        return intent.effort_level
    return True


def _render_ollama_think(
    wire: ReasoningWire,
    intent: ReasoningIntent,
    payload: dict[str, Any],
    output_allowance: int | None,
) -> None:
    del output_allowance
    think = _ollama_think(wire, intent)
    if think is not None:
        payload["think"] = think


def _describe_ollama_think(wire: ReasoningWire, intent: ReasoningIntent) -> ReasoningIntent:
    think = _ollama_think(wire, intent)
    if think is None:
        return _SENDS_NOTHING
    if think is False:
        return ReasoningIntent(REASONING_INTENT_OFF)
    if think is True:
        return ReasoningIntent(REASONING_INTENT_ON)
    return ReasoningIntent(REASONING_INTENT_EFFORT, effort_level=think)


_DIALECTS: dict[str, _Dialect] = {
    "none": _Dialect(_render_nothing, _describe_nothing),
    "reasoning_effort": _Dialect(
        _render_reasoning_effort, _describe_reasoning_effort, ("reasoning_effort",)
    ),
    "responses_reasoning": _Dialect(
        _render_responses_reasoning, _describe_reasoning_effort, ("reasoning", "include")
    ),
    "openrouter_reasoning": _Dialect(
        _render_openrouter_reasoning,
        _describe_openrouter_reasoning,
        ("reasoning", "include_reasoning"),
    ),
    "nous_reasoning": _Dialect(_render_nous_reasoning, _describe_nous_reasoning, ("reasoning",)),
    "thinking_toggle": _Dialect(_render_thinking_toggle, _describe_thinking_toggle, ("thinking",)),
    "thinking_toggle_with_effort": _Dialect(
        _render_thinking_toggle_with_effort,
        _describe_thinking_toggle_with_effort,
        ("thinking", "reasoning_effort"),
    ),
    "gemini_thinking": _Dialect(
        _render_gemini_thinking, _describe_reasoning_effort, ("generationConfig",)
    ),
    "minimax_split": _Dialect(_render_minimax_split, _describe_minimax_split, ("reasoning_split",)),
    "minimax_thinking": _Dialect(
        _render_minimax_thinking, _describe_minimax_thinking, ("thinking", "reasoning_split")
    ),
    "anthropic_thinking": _Dialect(
        _render_anthropic_thinking, _describe_anthropic_thinking, ("thinking", "output_config")
    ),
    "ollama_think": _Dialect(_render_ollama_think, _describe_ollama_think, ("think",)),
}
