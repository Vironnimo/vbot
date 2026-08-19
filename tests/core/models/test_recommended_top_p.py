"""Tests for the optional ``recommended_top_p`` model fact.

Covers the dataclass field and the ``_model_from_record`` coercion path,
mirroring ``test_recommended_temperature.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from core.models.models import (
    Capabilities,
    Model,
    ReasoningCapabilities,
    _coerce_recommended_top_p,
)

_NO_REASONING: dict[str, Any] = {
    "vision": False,
    "tools": True,
    "json_mode": False,
    "reasoning": {"supported": False},
}


def _model(**overrides: Any) -> Model:
    return Model(
        model_id="test-model",
        name="Test",
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=False,
            reasoning=ReasoningCapabilities(supported=False),
        ),
        context_window=32000,
        max_output_tokens=4096,
        **overrides,
    )


class TestCoerceRecommendedTopP:
    def test_none_stays_none(self):
        assert _coerce_recommended_top_p(None) is None

    def test_valid_float(self):
        assert _coerce_recommended_top_p(0.95) == 0.95

    def test_int_is_coerced_to_float(self):
        result = _coerce_recommended_top_p(1)
        assert result == 1.0
        assert isinstance(result, float)

    def test_zero_is_valid(self):
        assert _coerce_recommended_top_p(0.0) == 0.0

    def test_max_boundary_is_valid(self):
        assert _coerce_recommended_top_p(1.0) == 1.0

    def test_below_range_is_ignored(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level("WARNING", logger="vbot.models"):
            assert _coerce_recommended_top_p(-0.1) is None
        assert "outside" in caplog.text

    def test_above_range_is_ignored(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level("WARNING", logger="vbot.models"):
            assert _coerce_recommended_top_p(1.1) is None
        assert "outside" in caplog.text

    def test_non_numeric_is_ignored(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level("WARNING", logger="vbot.models"):
            assert _coerce_recommended_top_p("hot") is None
        assert "not a number" in caplog.text

    def test_bool_is_ignored(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level("WARNING", logger="vbot.models"):
            assert _coerce_recommended_top_p(True) is None
        assert "not a number" in caplog.text

    def test_nan_is_ignored(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level("WARNING", logger="vbot.models"):
            assert _coerce_recommended_top_p(float("nan")) is None
        assert "not finite" in caplog.text

    def test_infinity_is_ignored(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level("WARNING", logger="vbot.models"):
            assert _coerce_recommended_top_p(float("inf")) is None
        assert "not finite" in caplog.text


class TestModelDataclass:
    def test_default_is_none(self):
        assert _model().recommended_top_p is None

    def test_set_to_float(self):
        assert _model(recommended_top_p=0.95).recommended_top_p == 0.95
