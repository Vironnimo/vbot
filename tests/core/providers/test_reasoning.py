"""Tests for shared provider reasoning helpers."""

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.reasoning import (
    REASONING_REPLAY_POLICIES,
    closest_supported_effort,
    model_reasoning_supported,
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
