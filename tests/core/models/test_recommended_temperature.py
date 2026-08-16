"""Tests for the optional ``recommended_temperature`` model fact.

Covers the dataclass field, the ``_model_from_record`` coercion path, and the
assembly behavior when the field is present, absent, or invalid in an override.
"""

from __future__ import annotations

from typing import Any

import pytest

from core.models.models import (
    Capabilities,
    Model,
    ReasoningCapabilities,
    _coerce_recommended_temperature,
)

_NO_REASONING: dict[str, Any] = {
    "vision": False,
    "tools": True,
    "json_mode": False,
    "reasoning": {"supported": False},
}


def _record(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "name": "Test Model",
        "capabilities": dict(_NO_REASONING),
        "context_window": 32000,
        "max_output_tokens": 4096,
    }
    record.update(overrides)
    return record


class TestCoerceRecommendedTemperature:
    def test_none_stays_none(self):
        assert _coerce_recommended_temperature(None) is None

    def test_valid_float(self):
        assert _coerce_recommended_temperature(1.0) == 1.0

    def test_int_is_coerced_to_float(self):
        result = _coerce_recommended_temperature(1)
        assert result == 1.0
        assert isinstance(result, float)

    def test_zero_is_valid(self):
        assert _coerce_recommended_temperature(0.0) == 0.0

    def test_max_boundary_is_valid(self):
        assert _coerce_recommended_temperature(2.0) == 2.0

    def test_below_range_is_ignored(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level("WARNING", logger="vbot.models"):
            assert _coerce_recommended_temperature(-0.1) is None
        assert "outside" in caplog.text

    def test_above_range_is_ignored(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level("WARNING", logger="vbot.models"):
            assert _coerce_recommended_temperature(2.1) is None
        assert "outside" in caplog.text

    def test_non_numeric_is_ignored(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level("WARNING", logger="vbot.models"):
            assert _coerce_recommended_temperature("hot") is None
        assert "not a number" in caplog.text

    def test_bool_is_ignored(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level("WARNING", logger="vbot.models"):
            assert _coerce_recommended_temperature(True) is None
        assert "not a number" in caplog.text

    def test_nan_is_ignored(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level("WARNING", logger="vbot.models"):
            assert _coerce_recommended_temperature(float("nan")) is None
        assert "not finite" in caplog.text

    def test_infinity_is_ignored(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level("WARNING", logger="vbot.models"):
            assert _coerce_recommended_temperature(float("inf")) is None
        assert "not finite" in caplog.text


class TestModelDataclass:
    def test_default_is_none(self):
        model = Model(
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
        )
        assert model.recommended_temperature is None

    def test_set_to_float(self):
        model = Model(
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
            recommended_temperature=1.0,
        )
        assert model.recommended_temperature == 1.0
