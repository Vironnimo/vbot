"""Shared reasoning-effort normalization helpers for provider adapters."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any, Literal

from core.models.models import (
    REASONING_CONTROL_BUDGET,
    REASONING_CONTROL_ON_OFF,
)
from core.utils.logging import get_logger

_LOGGER = get_logger("providers.reasoning")

THINKING_EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
THINKING_EFFORT_RANKS = {effort: rank for rank, effort in enumerate(THINKING_EFFORT_ORDER)}

# Substrings that, in an HTTP 400 response detail, conservatively identify the
# rejected control as a reasoning/effort field. Strict providers (e.g. direct
# OpenAI) return 400 when an effort value is invalid for the model; the wording
# varies, so we keep the match conservative — a missed signal (no warning) is
# acceptable, a wrong reclassification is not. Detection only emits a warning;
# it never changes status classification or retry policy.
_BAD_EFFORT_DETAIL_SUBSTRINGS = ("reasoning_effort", "reasoning effort", "reasoning.effort")

# Where OpenAI-compatible ``usage`` reports the model's reasoning-token count.
_REASONING_TOKEN_DETAILS_KEYS = ("completion_tokens_details", "output_tokens_details")
_REASONING_TOKENS_KEY = "reasoning_tokens"

# HTTP status a strict provider returns when a reasoning effort is invalid.
_BAD_EFFORT_STATUS_CODE = 400
# The one effort value that means "do not reason"; never flagged as swallowed.
_NONE_EFFORT = "none"

ReasoningReplayPolicy = Literal["none", "current_run", "full_history"]
"""How persisted assistant ``reasoning``/``reasoning_meta`` replays into provider requests.

- ``none`` — assistant request entries never carry reasoning fields, not even
  the live in-run continuation turn.
- ``current_run`` — only the active run's assistant turns keep their reasoning
  fields; history from earlier runs is stripped (the historical default).
- ``full_history`` — assistant entries whose persisted model passes the chat
  layer's same-model gate keep their reasoning fields across runs.
"""

REASONING_REPLAY_NONE: ReasoningReplayPolicy = "none"
REASONING_REPLAY_CURRENT_RUN: ReasoningReplayPolicy = "current_run"
REASONING_REPLAY_FULL_HISTORY: ReasoningReplayPolicy = "full_history"
REASONING_REPLAY_POLICIES: tuple[ReasoningReplayPolicy, ...] = (
    REASONING_REPLAY_NONE,
    REASONING_REPLAY_CURRENT_RUN,
    REASONING_REPLAY_FULL_HISTORY,
)


def normalize_thinking_effort(value: Any) -> str:
    """Return a canonical vBot thinking effort or an empty string."""

    if not isinstance(value, str):
        return ""
    normalized = value.strip().lower()
    return normalized if normalized in THINKING_EFFORT_RANKS else ""


def closest_supported_effort(value: Any, supported_efforts: Iterable[str]) -> str | None:
    """Map a vBot thinking effort to the nearest provider-supported effort.

    If two provider levels are equally close, the lower level wins so vBot does
    not silently increase reasoning cost beyond the selected level.
    """

    effort = normalize_thinking_effort(value)
    if not effort:
        return None

    supported = tuple(
        dict.fromkeys(
            supported_effort
            for raw_effort in supported_efforts
            if (supported_effort := normalize_thinking_effort(raw_effort))
        )
    )
    if effort == "none":
        return "none" if "none" in supported else None

    active_supported = tuple(
        supported_effort for supported_effort in supported if supported_effort != "none"
    )
    if not active_supported:
        return None
    if effort in active_supported:
        return effort

    target_rank = THINKING_EFFORT_RANKS[effort]
    return min(
        active_supported,
        key=lambda supported_effort: (
            abs(THINKING_EFFORT_RANKS[supported_effort] - target_rank),
            THINKING_EFFORT_RANKS[supported_effort],
        ),
    )


def model_reasoning_supported(
    model_lookup: Callable[[str], Any] | None,
    model_id: str,
) -> bool | None:
    """Return catalog reasoning support for a provider-local model when known."""

    if model_lookup is None:
        return None

    catalog_model_id = model_id.split("::", 1)[0]
    model = model_lookup(catalog_model_id)
    if model is None:
        return None
    supported = model.capabilities.reasoning.supported
    return supported if isinstance(supported, bool) else None


def model_reasoning_levels(
    model_lookup: Callable[[str], Any] | None,
    model_id: str,
) -> tuple[str, ...] | None:
    """Return the model's effective reasoning-effort ladder, or ``None`` when absent.

    The ladder is the merged-at-load ``capabilities.reasoning.levels`` projected
    from the models.dev feed (see ``stuff/HANDOFF-model-db.md`` → "Merge beim
    Laden"). Adapters snap a selected effort against this per-model ladder instead
    of a provider-global constant; the adapter constant is only the floor when a
    model has no feed ladder, which this function signals by returning ``None``.

    Returns ``None`` (no ladder — caller falls back to the adapter floor) when
    there is no ``model_lookup``, the model is unknown, or its ``levels`` is empty
    (e.g. a budget-only model, or opencode-go whose hand override currently
    clobbers the ladder — a Phase-5 concern). Returns the non-empty ladder tuple
    otherwise. The connection-pin suffix is stripped before lookup, mirroring
    :func:`model_reasoning_supported`.
    """

    if model_lookup is None:
        return None

    catalog_model_id = model_id.split("::", 1)[0]
    model = model_lookup(catalog_model_id)
    if model is None:
        return None
    levels = model.capabilities.reasoning.levels
    if not levels:
        return None
    return tuple(levels)


def model_reasoning_control(
    model_lookup: Callable[[str], Any] | None,
    model_id: str,
) -> str | None:
    """Return the model's wire reasoning ``control`` kind, or ``None`` when unknown.

    Mirrors :func:`model_reasoning_levels`: strips the connection-pin suffix
    before lookup and returns ``None`` when there is no lookup or the model is
    unknown. The value is one of ``REASONING_CONTROLS`` (``levels`` / ``on_off``
    / ``budget``) or ``None`` (no control projected yet); the resolver treats an
    unknown control like ``levels`` (effort path).
    """

    if model_lookup is None:
        return None
    model = model_lookup(model_id.split("::", 1)[0])
    if model is None:
        return None
    control = model.capabilities.reasoning.control
    return control if isinstance(control, str) else None


def model_reasoning_budget_max(
    model_lookup: Callable[[str], Any] | None,
    model_id: str,
) -> int | None:
    """Return the model's max thinking-token budget, or ``None`` when unknown.

    Mirrors :func:`model_reasoning_levels`. ``None`` means the budget ceiling is
    not known (every Anthropic budget Claude today, until hand-seeded in the
    override layer); :func:`effort_to_budget` then uses its absolute fallback
    ladder instead of a proportional fraction.
    """

    if model_lookup is None:
        return None
    model = model_lookup(model_id.split("::", 1)[0])
    if model is None:
        return None
    budget_max = model.capabilities.reasoning.budget_max
    return budget_max if isinstance(budget_max, int) and not isinstance(budget_max, bool) else None


# ---------------------------------------------------------------------------
# Reasoning intent — one policy, many renders
# ---------------------------------------------------------------------------
#
# A provider-neutral description of what the *next request* should ask of the
# model's reasoning. ``resolve_reasoning_intent`` is the single place that turns
# ``(model control, agent effort)`` into one of these; each adapter only renders
# the intent into its own wire vocabulary. Adding a future provider or control
# kind is a new render, never new policy.

REASONING_INTENT_DEFAULT = "default"
REASONING_INTENT_OFF = "off"
REASONING_INTENT_EFFORT = "effort"
REASONING_INTENT_BUDGET = "budget"
REASONING_INTENT_ON = "on"
REASONING_INTENT_KINDS = (
    REASONING_INTENT_DEFAULT,
    REASONING_INTENT_OFF,
    REASONING_INTENT_EFFORT,
    REASONING_INTENT_BUDGET,
    REASONING_INTENT_ON,
)

# The lower bound for any thinking-token budget. A budget below this is not worth
# spending a thinking turn on, so it is the floor of the clamp range and the
# threshold below which a budget cannot be formed (see ``effort_to_budget``).
BUDGET_FLOOR_TOKENS = 1024

# D1 — proportional mapping used when the model publishes a ``budget_max``: the
# vBot effort picks a fraction of that ceiling.
_EFFORT_BUDGET_FRACTIONS = {
    "minimal": 0.10,
    "low": 0.25,
    "medium": 0.50,
    "high": 0.75,
    "xhigh": 0.90,
    "max": 1.0,
}

# D1 — absolute fallback ladder used when ``budget_max`` is unknown (every
# Anthropic budget Claude today). Each rung is a concrete token budget.
_EFFORT_BUDGET_ABSOLUTE = {
    "minimal": 1024,
    "low": 4096,
    "medium": 8192,
    "high": 16384,
    "xhigh": 24576,
    "max": 32768,
}


@dataclass(frozen=True)
class ReasoningIntent:
    """Provider-neutral description of the reasoning request for one turn.

    ``kind`` is one of :data:`REASONING_INTENT_KINDS`:

    * ``default`` — no effort selected; leave the provider default untouched
      (adapters omit every reasoning field).
    * ``off`` — do not reason. ``effort_level`` is set to ``"none"`` only when
      the wire spells *off* as an effort value (``levels``/unknown control with a
      ``none`` rung), so OpenAI/OpenRouter can reproduce that exact shape; for a
      native toggle/budget wire it stays ``None`` and the adapter sends its own
      off-shape.
    * ``effort`` — reason at ``effort_level`` (already snapped to the model's
      ladder).
    * ``budget`` — reason within ``budget_tokens``; ``effort_level`` also carries
      the snapped effort so an adapter without a native budget field can degrade
      to an effort.
    * ``on`` — reason (binary toggle on); ``effort_level`` carries the snapped
      effort for adapters that degrade ``on`` to an effort.
    """

    kind: str
    effort_level: str | None = None
    budget_tokens: int | None = None


def effort_to_budget(
    effort: Any,
    budget_max: int | None = None,
    max_tokens: int | None = None,
) -> int | None:
    """Map a vBot effort to a thinking-token budget, or ``None`` when none fits.

    The single home of the effort→budget policy (D1/D3):

    * With a positive ``budget_max`` the budget is that ceiling times the
      effort's fraction; without one it reads the absolute fallback ladder.
    * The result is clamped into ``[BUDGET_FLOOR_TOKENS, budget_max]`` and kept
      strictly under a positive ``max_tokens`` (the budget is part of the output
      allowance).
    * Returns ``None`` when the effort is empty/``none`` or when even
      ``BUDGET_FLOOR_TOKENS`` cannot fit under ``max_tokens`` (D3 skip) — the
      caller then falls back to a plain *on* or warns.
    """

    normalized = normalize_thinking_effort(effort)
    if not normalized or normalized == _NONE_EFFORT:
        return None

    ceiling = (
        budget_max
        if isinstance(budget_max, int) and not isinstance(budget_max, bool) and budget_max > 0
        else None
    )
    if ceiling is not None:
        fraction = _EFFORT_BUDGET_FRACTIONS.get(normalized)
        if fraction is None:
            return None
        budget = round(ceiling * fraction)
    else:
        absolute = _EFFORT_BUDGET_ABSOLUTE.get(normalized)
        if absolute is None:
            return None
        budget = absolute

    budget = max(budget, BUDGET_FLOOR_TOKENS)
    if ceiling is not None and ceiling >= BUDGET_FLOOR_TOKENS:
        budget = min(budget, ceiling)

    if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) and max_tokens > 0:
        if max_tokens <= BUDGET_FLOOR_TOKENS:
            return None
        budget = min(budget, max_tokens - 1)

    return budget


def resolve_reasoning_intent(
    *,
    supported: bool | None,
    control: str | None,
    levels: Iterable[str],
    effort: Any,
    budget_max: int | None = None,
    max_tokens: int | None = None,
) -> ReasoningIntent:
    """Turn ``(model control, agent effort)`` into a provider-neutral intent.

    The single decision layer (D1/D2/D3 live here, nowhere else):

    * ``supported is False`` → ``off``.
    * no effort selected → ``default``.
    * effort ``none`` → ``off`` (carrying the snapped ``"none"`` for an
      effort-spelled-off wire; bare for a native toggle/budget wire).
    * ``on_off`` control → ``on``.
    * ``budget`` control → ``budget`` with :func:`effort_to_budget`, or ``on``
      when no budget can be formed.
    * ``levels``/unknown control → ``effort`` snapped to ``levels`` (or
      ``default`` when nothing snaps).

    ``levels`` is the *effective* ladder the caller wants snapping against — the
    model's feed ladder, or the adapter's floor when it has none — so the snap
    always runs against a concrete ladder.
    """

    if supported is False:
        return ReasoningIntent(REASONING_INTENT_OFF)

    normalized = normalize_thinking_effort(effort)
    if not normalized:
        return ReasoningIntent(REASONING_INTENT_DEFAULT)

    snapped = closest_supported_effort(normalized, levels)

    if normalized == _NONE_EFFORT:
        if control in (REASONING_CONTROL_ON_OFF, REASONING_CONTROL_BUDGET):
            return ReasoningIntent(REASONING_INTENT_OFF)
        return ReasoningIntent(REASONING_INTENT_OFF, effort_level=snapped)

    if control == REASONING_CONTROL_ON_OFF:
        return ReasoningIntent(REASONING_INTENT_ON, effort_level=snapped)

    if control == REASONING_CONTROL_BUDGET:
        budget = effort_to_budget(normalized, budget_max, max_tokens)
        if budget is None:
            return ReasoningIntent(REASONING_INTENT_ON, effort_level=snapped)
        return ReasoningIntent(
            REASONING_INTENT_BUDGET,
            effort_level=snapped,
            budget_tokens=budget,
        )

    if snapped is None:
        return ReasoningIntent(REASONING_INTENT_DEFAULT)
    return ReasoningIntent(REASONING_INTENT_EFFORT, effort_level=snapped)


def remove_reasoning_kwargs(
    kwargs: MutableMapping[str, Any],
    *parameter_names: str,
) -> None:
    """Remove provider reasoning controls from a mutable request kwargs map."""

    for parameter_name in parameter_names:
        kwargs.pop(parameter_name, None)


# ---------------------------------------------------------------------------
# Observability — surface reasoning feedback signals providers return
# ---------------------------------------------------------------------------


def detail_names_rejected_effort(detail: str) -> bool:
    """Return whether an HTTP 400 detail names a rejected reasoning/effort control.

    Conservative substring match against known reasoning-effort field spellings.
    A false negative (no match) is acceptable; the caller must never let a match
    change status classification or retry policy — it only gates a warning.
    """

    if not detail:
        return False
    lowered = detail.lower()
    return any(token in lowered for token in _BAD_EFFORT_DETAIL_SUBSTRINGS)


def warn_rejected_effort(
    *,
    status_code: int,
    detail: str,
    model_id: str,
    selected_effort: str,
    provider_logger: Any | None = None,
) -> None:
    """Log a structured warning when a 400 rejected the request's reasoning effort.

    Emits only when *status_code* is 400 and *detail* conservatively names a
    reasoning/effort control. Does not raise and does not change classification:
    the caller still classifies the status exactly as before, so 400 stays fatal
    and non-retryable. Secrets are never part of *detail* on this path (the body
    is a provider validation message), and no token values are logged.
    """

    if status_code != _BAD_EFFORT_STATUS_CODE:
        return
    if not detail_names_rejected_effort(detail):
        return
    logger = provider_logger if provider_logger is not None else _LOGGER
    effort = normalize_thinking_effort(selected_effort) or "(unspecified)"
    logger.warning(
        "Provider rejected reasoning effort with HTTP 400 "
        "(model=%s, selected_effort=%s); effort was not applied",
        model_id,
        effort,
    )


def reasoning_token_count(usage: Mapping[str, Any] | None) -> int | None:
    """Return the reasoning-token count from a normalized-or-raw ``usage`` mapping.

    Reads the OpenAI-compatible ``completion_tokens_details.reasoning_tokens``
    (or Responses-style ``output_tokens_details.reasoning_tokens``). Returns
    ``None`` when the counter is absent or not an int — "unknown", not "zero" —
    so callers do not treat a sparse usage block as a swallowed effort.
    """

    if not isinstance(usage, Mapping):
        return None
    for details_key in _REASONING_TOKEN_DETAILS_KEYS:
        details = usage.get(details_key)
        if not isinstance(details, Mapping):
            continue
        value = details.get(_REASONING_TOKENS_KEY)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
    return None


def warn_effort_swallowed(
    *,
    selected_effort: str,
    usage: Mapping[str, Any] | None,
    model_id: str,
    provider_logger: Any | None = None,
) -> None:
    """Log a structured warning when a non-``none`` effort yielded 0 reasoning tokens.

    The selected effort asked the model to think, but the response's reasoning
    token counter came back as exactly ``0`` — the effort was effectively
    swallowed. Stays silent when no effort was selected, when the effort was
    ``none``, or when the reasoning-token count is unknown (sparse usage) or
    non-zero. No token values beyond the count are logged.
    """

    effort = normalize_thinking_effort(selected_effort)
    if not effort or effort == _NONE_EFFORT:
        return
    reasoning_tokens = reasoning_token_count(usage)
    if reasoning_tokens != 0:
        return
    logger = provider_logger if provider_logger is not None else _LOGGER
    logger.warning(
        "Reasoning effort was swallowed: response reported 0 reasoning tokens "
        "(model=%s, selected_effort=%s)",
        model_id,
        effort,
    )
