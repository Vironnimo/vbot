"""Tests for shared provider reasoning helpers."""

from __future__ import annotations

import logging
from typing import Any

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.reasoning import (
    REASONING_REPLAY_POLICIES,
    closest_supported_effort,
    detail_names_rejected_effort,
    model_reasoning_levels,
    model_reasoning_supported,
    reasoning_token_count,
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

    assert model_reasoning_levels(
        model_lookup, "deepseek/deepseek-v4-pro::api-key"
    ) == ("high", "xhigh")


def test_model_reasoning_levels_none_without_ladder() -> None:
    """No lookup, unknown model, or empty ladder all signal 'fall back to floor'."""
    assert model_reasoning_levels(None, "anything") is None
    assert model_reasoning_levels(lambda _model_id: None, "missing") is None
    empty_ladder_lookup = lambda _model_id: _model_with_reasoning(  # noqa: E731
        ReasoningCapabilities(supported=True)
    )
    assert model_reasoning_levels(empty_ladder_lookup, "budget-only") is None


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
