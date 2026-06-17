"""Tests for shared provider reasoning helpers."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from core.models.models import (
    REASONING_CONTROL_BUDGET,
    REASONING_CONTROL_LEVELS,
    REASONING_CONTROL_ON_OFF,
    Capabilities,
    Model,
    ReasoningCapabilities,
)
from core.providers.reasoning import (
    BUDGET_FLOOR_TOKENS,
    REASONING_INTENT_BUDGET,
    REASONING_INTENT_DEFAULT,
    REASONING_INTENT_EFFORT,
    REASONING_INTENT_KINDS,
    REASONING_INTENT_OFF,
    REASONING_INTENT_ON,
    REASONING_REPLAY_POLICIES,
    ReasoningIntent,
    closest_supported_effort,
    detail_names_rejected_effort,
    effort_to_budget,
    model_reasoning_budget_max,
    model_reasoning_control,
    model_reasoning_levels,
    model_reasoning_supported,
    reasoning_token_count,
    resolve_reasoning_intent,
    warn_effort_swallowed,
    warn_rejected_effort,
)

_REASONING_LOGGER = "vbot.providers.reasoning"


def _model_with_reasoning(reasoning: ReasoningCapabilities) -> Model:
    return Model(
        model_id="m",
        name="m",
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=True,
            reasoning=reasoning,
        ),
        context_window=128000,
        max_output_tokens=4096,
    )


def test_reasoning_replay_policy_axis_is_pinned() -> None:
    """The replay-policy axis is a deliberate three-value contract."""
    assert REASONING_REPLAY_POLICIES == ("none", "current_run", "full_history")


def test_closest_supported_effort_maps_to_nearest_known_level() -> None:
    assert closest_supported_effort("minimal", {"low", "medium", "high"}) == "low"
    assert closest_supported_effort("max", {"low", "medium", "high"}) == "high"
    assert closest_supported_effort("low", {"none", "high"}) == "high"


def test_closest_supported_effort_prefers_lower_cost_on_tie() -> None:
    assert closest_supported_effort("medium", {"low", "high"}) == "low"


def test_closest_supported_effort_omits_none_when_unsupported() -> None:
    assert closest_supported_effort("none", {"low", "medium", "high"}) is None


def test_model_reasoning_supported_strips_connection_suffix() -> None:
    def model_lookup(model_id: str) -> Model | None:
        assert model_id == "gpt-4o"
        return Model(
            model_id=model_id,
            name=model_id,
            capabilities=Capabilities(
                vision=False,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(supported=False),
            ),
            context_window=128000,
            max_output_tokens=4096,
        )

    assert model_reasoning_supported(model_lookup, "gpt-4o::api-key") is False


# ---------------------------------------------------------------------------
# Effective per-model reasoning ladder
# ---------------------------------------------------------------------------


def test_model_reasoning_levels_returns_effective_ladder() -> None:
    """A model with a feed ladder returns it as a tuple, suffix-stripped."""

    def model_lookup(model_id: str) -> Model | None:
        assert model_id == "deepseek/deepseek-v4-pro"
        return _model_with_reasoning(
            ReasoningCapabilities(supported=True, control="levels", levels=("high", "xhigh"))
        )

    assert model_reasoning_levels(model_lookup, "deepseek/deepseek-v4-pro::api-key") == (
        "high",
        "xhigh",
    )


def test_model_reasoning_levels_none_without_ladder() -> None:
    """No lookup, unknown model, or empty ladder all signal 'fall back to floor'."""
    assert model_reasoning_levels(None, "anything") is None
    assert model_reasoning_levels(lambda _model_id: None, "missing") is None
    empty_ladder_lookup = lambda _model_id: _model_with_reasoning(  # noqa: E731
        ReasoningCapabilities(supported=True)
    )
    assert model_reasoning_levels(empty_ladder_lookup, "budget-only") is None


# ---------------------------------------------------------------------------
# Reasoning control / budget_max accessors
# ---------------------------------------------------------------------------


def test_model_reasoning_control_reads_control_and_strips_suffix() -> None:
    def model_lookup(model_id: str) -> Model | None:
        assert model_id == "anthropic/claude-opus-4-1"
        return _model_with_reasoning(
            ReasoningCapabilities(supported=True, control=REASONING_CONTROL_BUDGET)
        )

    assert (
        model_reasoning_control(model_lookup, "anthropic/claude-opus-4-1::api-key")
        == REASONING_CONTROL_BUDGET
    )


def test_model_reasoning_control_none_without_lookup_or_model() -> None:
    assert model_reasoning_control(None, "anything") is None
    assert model_reasoning_control(lambda _model_id: None, "missing") is None


def test_model_reasoning_budget_max_reads_value_and_strips_suffix() -> None:
    def model_lookup(model_id: str) -> Model | None:
        assert model_id == "google/gemini-2.5-pro"
        return _model_with_reasoning(
            ReasoningCapabilities(
                supported=True, control=REASONING_CONTROL_BUDGET, budget_max=24576
            )
        )

    assert model_reasoning_budget_max(model_lookup, "google/gemini-2.5-pro::api-key") == 24576


def test_model_reasoning_budget_max_none_without_lookup_or_model() -> None:
    assert model_reasoning_budget_max(None, "anything") is None
    assert model_reasoning_budget_max(lambda _model_id: None, "missing") is None


# ---------------------------------------------------------------------------
# effort_to_budget — the single effort→token-budget policy (D1/D3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("effort", "expected"),
    [
        ("minimal", 1024),
        ("low", 4096),
        ("medium", 8192),
        ("high", 16384),
        ("xhigh", 24576),
        ("max", 32768),
    ],
)
def test_effort_to_budget_uses_absolute_ladder_without_ceiling(effort: str, expected: int) -> None:
    """No ``budget_max`` → the absolute fallback ladder (one rung per effort)."""
    assert effort_to_budget(effort, budget_max=None) == expected


@pytest.mark.parametrize(
    ("effort", "expected"),
    [
        ("low", 25000),  # 0.25 * 100_000
        ("medium", 50000),
        ("high", 75000),
        ("max", 100000),
    ],
)
def test_effort_to_budget_is_proportional_to_ceiling(effort: str, expected: int) -> None:
    """A positive ``budget_max`` → that ceiling times the effort fraction."""
    assert effort_to_budget(effort, budget_max=100_000) == expected


def test_effort_to_budget_applies_floor() -> None:
    """A tiny ceiling fraction is lifted to the budget floor."""
    # 0.10 * 5000 == 500, below the 1024 floor.
    assert effort_to_budget("minimal", budget_max=5000) == BUDGET_FLOOR_TOKENS


def test_effort_to_budget_caps_at_ceiling() -> None:
    assert effort_to_budget("max", budget_max=20000) == 20000


def test_effort_to_budget_clamps_under_max_tokens() -> None:
    """The budget must stay strictly under a positive ``max_tokens``."""
    assert effort_to_budget("max", budget_max=None, max_tokens=10000) == 9999


def test_effort_to_budget_skips_when_floor_does_not_fit() -> None:
    """When even the floor cannot fit under ``max_tokens`` no budget is formed."""
    assert effort_to_budget("high", budget_max=None, max_tokens=BUDGET_FLOOR_TOKENS) is None
    assert effort_to_budget("high", budget_max=None, max_tokens=500) is None


def test_effort_to_budget_none_for_empty_or_none_effort() -> None:
    assert effort_to_budget("", budget_max=50000) is None
    assert effort_to_budget("none", budget_max=50000) is None
    assert effort_to_budget(None) is None


# ---------------------------------------------------------------------------
# resolve_reasoning_intent — the single decision layer (D1/D2/D3)
# ---------------------------------------------------------------------------

_LADDER = ("none", "low", "medium", "high")
_ACTIVE_LADDER = ("low", "medium", "high")


def test_reasoning_intent_kinds_are_pinned() -> None:
    assert REASONING_INTENT_KINDS == ("default", "off", "effort", "budget", "on")


def test_resolve_intent_off_when_reasoning_unsupported() -> None:
    intent = resolve_reasoning_intent(
        supported=False,
        control=REASONING_CONTROL_LEVELS,
        levels=_LADDER,
        effort="high",
    )
    assert intent == ReasoningIntent(REASONING_INTENT_OFF)


@pytest.mark.parametrize("effort", ["", None, "bogus"])
def test_resolve_intent_default_when_no_effort_selected(effort: Any) -> None:
    intent = resolve_reasoning_intent(
        supported=True,
        control=REASONING_CONTROL_LEVELS,
        levels=_LADDER,
        effort=effort,
    )
    assert intent == ReasoningIntent(REASONING_INTENT_DEFAULT)


def test_resolve_intent_none_on_levels_with_none_rung_carries_snapped_none() -> None:
    """An effort-spelled-off wire (``none`` rung) keeps ``effort_level='none'``."""
    intent = resolve_reasoning_intent(
        supported=True,
        control=REASONING_CONTROL_LEVELS,
        levels=_LADDER,
        effort="none",
    )
    assert intent == ReasoningIntent(REASONING_INTENT_OFF, effort_level="none")


def test_resolve_intent_none_on_levels_without_none_rung_is_bare_off() -> None:
    intent = resolve_reasoning_intent(
        supported=True,
        control=REASONING_CONTROL_LEVELS,
        levels=_ACTIVE_LADDER,
        effort="none",
    )
    assert intent == ReasoningIntent(REASONING_INTENT_OFF, effort_level=None)


@pytest.mark.parametrize("control", [REASONING_CONTROL_ON_OFF, REASONING_CONTROL_BUDGET])
def test_resolve_intent_none_on_native_control_is_bare_off(control: str) -> None:
    """A native toggle/budget wire spells off itself — no carried effort level."""
    intent = resolve_reasoning_intent(
        supported=True,
        control=control,
        levels=_LADDER,
        effort="none",
    )
    assert intent == ReasoningIntent(REASONING_INTENT_OFF)


@pytest.mark.parametrize(
    ("effort", "expected_level"),
    [("low", "low"), ("medium", "medium"), ("high", "high"), ("max", "high")],
)
def test_resolve_intent_levels_snaps_active_effort(effort: str, expected_level: str) -> None:
    intent = resolve_reasoning_intent(
        supported=True,
        control=REASONING_CONTROL_LEVELS,
        levels=_ACTIVE_LADDER,
        effort=effort,
    )
    assert intent == ReasoningIntent(REASONING_INTENT_EFFORT, effort_level=expected_level)


def test_resolve_intent_levels_default_when_nothing_snaps() -> None:
    """An empty ladder cannot snap an active effort → leave the provider default."""
    intent = resolve_reasoning_intent(
        supported=True,
        control=REASONING_CONTROL_LEVELS,
        levels=(),
        effort="high",
    )
    assert intent == ReasoningIntent(REASONING_INTENT_DEFAULT)


def test_resolve_intent_unknown_control_takes_levels_path() -> None:
    intent = resolve_reasoning_intent(
        supported=True,
        control=None,
        levels=_ACTIVE_LADDER,
        effort="high",
    )
    assert intent == ReasoningIntent(REASONING_INTENT_EFFORT, effort_level="high")


@pytest.mark.parametrize("effort", ["low", "medium", "high", "max"])
def test_resolve_intent_on_off_is_on_for_any_active_effort(effort: str) -> None:
    intent = resolve_reasoning_intent(
        supported=True,
        control=REASONING_CONTROL_ON_OFF,
        levels=_ACTIVE_LADDER,
        effort=effort,
    )
    assert intent.kind == REASONING_INTENT_ON


def test_resolve_intent_budget_without_ceiling_uses_absolute_ladder() -> None:
    intent = resolve_reasoning_intent(
        supported=True,
        control=REASONING_CONTROL_BUDGET,
        levels=_ACTIVE_LADDER,
        effort="high",
        budget_max=None,
    )
    assert intent == ReasoningIntent(
        REASONING_INTENT_BUDGET, effort_level="high", budget_tokens=16384
    )


def test_resolve_intent_budget_with_ceiling_is_proportional() -> None:
    intent = resolve_reasoning_intent(
        supported=True,
        control=REASONING_CONTROL_BUDGET,
        levels=_ACTIVE_LADDER,
        effort="medium",
        budget_max=40000,
    )
    assert intent.kind == REASONING_INTENT_BUDGET
    assert intent.budget_tokens == 20000


def test_resolve_intent_budget_falls_back_to_on_when_no_budget_fits() -> None:
    """When even the floor cannot fit ``max_tokens`` budget degrades to a plain on."""
    intent = resolve_reasoning_intent(
        supported=True,
        control=REASONING_CONTROL_BUDGET,
        levels=_ACTIVE_LADDER,
        effort="high",
        budget_max=None,
        max_tokens=500,
    )
    assert intent.kind == REASONING_INTENT_ON


# ---------------------------------------------------------------------------
# Observability — rejected reasoning effort (HTTP 400)
# ---------------------------------------------------------------------------


def test_detail_names_rejected_effort_matches_known_field_spellings() -> None:
    assert detail_names_rejected_effort("400 invalid value for 'reasoning_effort'") is True
    assert detail_names_rejected_effort("Unsupported reasoning effort: ultra") is True


def test_detail_names_rejected_effort_is_conservative() -> None:
    assert detail_names_rejected_effort("400 model is overloaded") is False
    assert detail_names_rejected_effort("") is False


def test_warn_rejected_effort_emits_on_400_naming_effort(caplog: Any) -> None:
    """A 400 whose body names a rejected effort emits a structured warning."""
    # Arrange / Act
    with caplog.at_level(logging.WARNING, logger=_REASONING_LOGGER):
        warn_rejected_effort(
            status_code=400,
            detail="400 invalid value for 'reasoning_effort': 'ultra'",
            model_id="gpt-5.2",
            selected_effort="max",
        )

    # Assert
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "gpt-5.2" in message
    assert "max" in message


def test_warn_rejected_effort_silent_when_status_is_not_400(caplog: Any) -> None:
    with caplog.at_level(logging.WARNING, logger=_REASONING_LOGGER):
        warn_rejected_effort(
            status_code=500,
            detail="500 invalid value for 'reasoning_effort'",
            model_id="gpt-5.2",
            selected_effort="max",
        )

    assert [record for record in caplog.records if record.levelno == logging.WARNING] == []


def test_warn_rejected_effort_silent_when_detail_unrelated(caplog: Any) -> None:
    with caplog.at_level(logging.WARNING, logger=_REASONING_LOGGER):
        warn_rejected_effort(
            status_code=400,
            detail="400 context length exceeded",
            model_id="gpt-5.2",
            selected_effort="max",
        )

    assert [record for record in caplog.records if record.levelno == logging.WARNING] == []


# ---------------------------------------------------------------------------
# Observability — swallowed reasoning effort (0 reasoning tokens)
# ---------------------------------------------------------------------------


def test_reasoning_token_count_reads_openai_and_responses_shapes() -> None:
    assert reasoning_token_count({"completion_tokens_details": {"reasoning_tokens": 7}}) == 7
    assert reasoning_token_count({"output_tokens_details": {"reasoning_tokens": 0}}) == 0


def test_reasoning_token_count_unknown_when_absent_or_malformed() -> None:
    assert reasoning_token_count(None) is None
    assert reasoning_token_count({}) is None
    assert reasoning_token_count({"completion_tokens_details": {}}) is None
    # A boolean is not a token count.
    assert reasoning_token_count({"completion_tokens_details": {"reasoning_tokens": True}}) is None


def test_warn_effort_swallowed_emits_on_nonzero_effort_with_zero_tokens(caplog: Any) -> None:
    """Effort sent but 0 reasoning tokens back emits a structured warning."""
    # Arrange / Act
    with caplog.at_level(logging.WARNING, logger=_REASONING_LOGGER):
        warn_effort_swallowed(
            selected_effort="high",
            usage={"completion_tokens_details": {"reasoning_tokens": 0}},
            model_id="gpt-5.2",
        )

    # Assert
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "gpt-5.2" in message
    assert "high" in message


def test_warn_effort_swallowed_silent_when_reasoning_tokens_nonzero(caplog: Any) -> None:
    with caplog.at_level(logging.WARNING, logger=_REASONING_LOGGER):
        warn_effort_swallowed(
            selected_effort="high",
            usage={"completion_tokens_details": {"reasoning_tokens": 42}},
            model_id="gpt-5.2",
        )

    assert [record for record in caplog.records if record.levelno == logging.WARNING] == []


def test_warn_effort_swallowed_silent_for_none_effort(caplog: Any) -> None:
    with caplog.at_level(logging.WARNING, logger=_REASONING_LOGGER):
        warn_effort_swallowed(
            selected_effort="none",
            usage={"completion_tokens_details": {"reasoning_tokens": 0}},
            model_id="gpt-5.2",
        )

    assert [record for record in caplog.records if record.levelno == logging.WARNING] == []


def test_warn_effort_swallowed_silent_when_token_count_unknown(caplog: Any) -> None:
    """Sparse usage (no reasoning-token counter) is unknown, not swallowed."""
    with caplog.at_level(logging.WARNING, logger=_REASONING_LOGGER):
        warn_effort_swallowed(
            selected_effort="high",
            usage={"prompt_tokens": 10, "completion_tokens": 5},
            model_id="gpt-5.2",
        )

    assert [record for record in caplog.records if record.levelno == logging.WARNING] == []
