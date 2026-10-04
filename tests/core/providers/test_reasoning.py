"""Tests for shared provider reasoning helpers."""

from __future__ import annotations

import logging
from collections.abc import Callable
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
_LADDER = ("none", "low", "medium", "high")
_ACTIVE_LADDER = ("low", "medium", "high")
_ZERO_REASONING_USAGE = {"completion_tokens_details": {"reasoning_tokens": 0}}


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


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]


def test_reasoning_vocabularies_are_pinned() -> None:
    """The replay-policy axis and the intent kinds are deliberate contracts."""
    assert REASONING_REPLAY_POLICIES == ("none", "current_run", "tool_turns", "full_history")
    assert REASONING_INTENT_KINDS == ("default", "off", "effort", "budget", "on")


@pytest.mark.parametrize(
    ("effort", "supported", "expected"),
    [
        pytest.param("minimal", {"low", "medium", "high"}, "low", id="up-to-nearest"),
        pytest.param("max", {"low", "medium", "high"}, "high", id="down-to-nearest"),
        pytest.param("low", {"none", "high"}, "high", id="none-is-not-an-active-level"),
        pytest.param("medium", {"low", "high"}, "low", id="tie-prefers-lower-cost"),
        pytest.param("none", {"low", "medium", "high"}, None, id="none-omitted-when-unsupported"),
    ],
)
def test_closest_supported_effort_snaps_to_the_nearest_known_level(
    effort: str, supported: set[str], expected: str | None
) -> None:
    assert closest_supported_effort(effort, supported) == expected


_Accessor = Callable[[Callable[[str], Model | None] | None, str], Any]
_ACCESSORS: tuple[_Accessor, ...] = (
    model_reasoning_supported,
    model_reasoning_levels,
    model_reasoning_control,
    model_reasoning_budget_max,
)


@pytest.mark.parametrize(
    ("accessor", "reasoning", "expected"),
    [
        pytest.param(
            model_reasoning_supported, ReasoningCapabilities(supported=False), False, id="supported"
        ),
        pytest.param(
            model_reasoning_levels,
            ReasoningCapabilities(supported=True, control="levels", levels=("high", "xhigh")),
            ("high", "xhigh"),
            id="levels",
        ),
        pytest.param(
            model_reasoning_levels,
            ReasoningCapabilities(supported=True),
            None,
            id="levels-empty-ladder-falls-back",
        ),
        pytest.param(
            model_reasoning_control,
            ReasoningCapabilities(supported=True, control=REASONING_CONTROL_BUDGET),
            REASONING_CONTROL_BUDGET,
            id="control",
        ),
        pytest.param(
            model_reasoning_budget_max,
            ReasoningCapabilities(
                supported=True, control=REASONING_CONTROL_BUDGET, budget_max=24576
            ),
            24576,
            id="budget-max",
        ),
    ],
)
def test_model_reasoning_accessors_read_the_catalog_model_without_connection_suffix(
    accessor: _Accessor, reasoning: ReasoningCapabilities, expected: Any
) -> None:
    looked_up: list[str] = []

    def model_lookup(model_id: str) -> Model | None:
        looked_up.append(model_id)
        return _model_with_reasoning(reasoning)

    assert accessor(model_lookup, "vendor/model::api-key") == expected
    assert looked_up == ["vendor/model"]


@pytest.mark.parametrize("accessor", _ACCESSORS)
def test_model_reasoning_accessors_are_unknown_without_lookup_or_model(
    accessor: _Accessor,
) -> None:
    assert accessor(None, "anything") is None
    assert accessor(lambda _model_id: None, "missing") is None


# ---------------------------------------------------------------------------
# effort_to_budget — the single effort→token-budget policy (D1/D3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("budget_max", "expected"),
    [
        pytest.param(
            None,
            {
                "minimal": 1024,
                "low": 4096,
                "medium": 8192,
                "high": 16384,
                "xhigh": 24576,
                "max": 32768,
            },
            id="absolute-ladder-without-ceiling",
        ),
        pytest.param(
            100_000,
            {"low": 25000, "medium": 50000, "high": 75000, "max": 100000},
            id="proportional-to-ceiling",
        ),
    ],
)
def test_effort_to_budget_maps_each_effort(
    budget_max: int | None, expected: dict[str, int]
) -> None:
    assert {effort: effort_to_budget(effort, budget_max=budget_max) for effort in expected} == (
        expected
    )


@pytest.mark.parametrize(
    ("effort", "budget_max", "max_tokens", "expected"),
    [
        # 0.10 * 5000 == 500, below the floor.
        pytest.param("minimal", 5000, None, BUDGET_FLOOR_TOKENS, id="lifted-to-floor"),
        pytest.param("max", 20000, None, 20000, id="capped-at-ceiling"),
        pytest.param("max", None, 10000, 9999, id="strictly-under-max-tokens"),
        pytest.param("high", None, BUDGET_FLOOR_TOKENS, None, id="floor-does-not-fit"),
        pytest.param("none", 50000, None, None, id="none-effort"),
        pytest.param(None, None, None, None, id="no-effort"),
    ],
)
def test_effort_to_budget_bounds(
    effort: str | None, budget_max: int | None, max_tokens: int | None, expected: int | None
) -> None:
    assert effort_to_budget(effort, budget_max=budget_max, max_tokens=max_tokens) == expected


# ---------------------------------------------------------------------------
# resolve_reasoning_intent — the single decision layer (D1/D2/D3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("supported", "control", "levels", "effort", "extra", "expected"),
    [
        pytest.param(
            False,
            REASONING_CONTROL_LEVELS,
            _LADDER,
            "high",
            {},
            ReasoningIntent(REASONING_INTENT_OFF),
            id="unsupported-is-off",
        ),
        pytest.param(
            True,
            REASONING_CONTROL_LEVELS,
            _LADDER,
            "bogus",
            {},
            ReasoningIntent(REASONING_INTENT_DEFAULT),
            id="no-known-effort-is-default",
        ),
        pytest.param(
            True,
            REASONING_CONTROL_LEVELS,
            _LADDER,
            "none",
            {},
            ReasoningIntent(REASONING_INTENT_OFF, effort_level="none"),
            id="none-on-levels-with-none-rung-carries-it",
        ),
        pytest.param(
            True,
            REASONING_CONTROL_LEVELS,
            _ACTIVE_LADDER,
            "none",
            {},
            ReasoningIntent(REASONING_INTENT_OFF),
            id="none-on-levels-without-none-rung-is-bare-off",
        ),
        pytest.param(
            True,
            REASONING_CONTROL_ON_OFF,
            _LADDER,
            "none",
            {},
            ReasoningIntent(REASONING_INTENT_OFF),
            id="none-on-native-control-is-bare-off",
        ),
        pytest.param(
            True,
            REASONING_CONTROL_LEVELS,
            _ACTIVE_LADDER,
            "high",
            {},
            ReasoningIntent(REASONING_INTENT_EFFORT, effort_level="high"),
            id="levels-keeps-a-known-level",
        ),
        pytest.param(
            True,
            REASONING_CONTROL_LEVELS,
            _ACTIVE_LADDER,
            "max",
            {},
            ReasoningIntent(REASONING_INTENT_EFFORT, effort_level="high"),
            id="levels-snaps-an-unknown-level",
        ),
        pytest.param(
            True,
            REASONING_CONTROL_LEVELS,
            (),
            "high",
            {},
            ReasoningIntent(REASONING_INTENT_DEFAULT),
            id="levels-default-when-nothing-snaps",
        ),
        pytest.param(
            True,
            None,
            _ACTIVE_LADDER,
            "high",
            {},
            ReasoningIntent(REASONING_INTENT_EFFORT, effort_level="high"),
            id="unknown-control-takes-levels-path",
        ),
        pytest.param(
            True,
            REASONING_CONTROL_ON_OFF,
            _ACTIVE_LADDER,
            "max",
            {},
            ReasoningIntent(REASONING_INTENT_ON, effort_level="high"),
            id="on-off-is-on-with-snapped-level",
        ),
        pytest.param(
            True,
            REASONING_CONTROL_BUDGET,
            _ACTIVE_LADDER,
            "high",
            {"budget_max": None},
            ReasoningIntent(REASONING_INTENT_BUDGET, effort_level="high", budget_tokens=16384),
            id="budget-without-ceiling-uses-absolute-ladder",
        ),
        pytest.param(
            True,
            REASONING_CONTROL_BUDGET,
            _ACTIVE_LADDER,
            "medium",
            {"budget_max": 40000},
            ReasoningIntent(REASONING_INTENT_BUDGET, effort_level="medium", budget_tokens=20000),
            id="budget-with-ceiling-is-proportional",
        ),
        pytest.param(
            True,
            REASONING_CONTROL_BUDGET,
            _ACTIVE_LADDER,
            "high",
            {"budget_max": None, "max_tokens": 500},
            ReasoningIntent(REASONING_INTENT_ON, effort_level="high"),
            id="budget-degrades-to-on-when-no-budget-fits",
        ),
    ],
)
def test_resolve_reasoning_intent(
    supported: bool,
    control: str | None,
    levels: tuple[str, ...],
    effort: str,
    extra: dict[str, Any],
    expected: ReasoningIntent,
) -> None:
    intent = resolve_reasoning_intent(
        supported=supported, control=control, levels=levels, effort=effort, **extra
    )

    assert intent == expected


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status_code", "detail", "warns"),
    [
        pytest.param(400, "400 invalid value for 'reasoning_effort': 'ultra'", True, id="field"),
        pytest.param(400, "Unsupported reasoning effort: ultra", True, id="prose"),
        pytest.param(400, "400 model is overloaded", False, id="unrelated-400"),
        pytest.param(400, "", False, id="empty-detail"),
        pytest.param(500, "500 invalid value for 'reasoning_effort'", False, id="not-400"),
    ],
)
def test_warn_rejected_effort_only_for_a_400_naming_the_effort(
    caplog: pytest.LogCaptureFixture, status_code: int, detail: str, warns: bool
) -> None:
    with caplog.at_level(logging.WARNING, logger=_REASONING_LOGGER):
        warn_rejected_effort(
            status_code=status_code,
            detail=detail,
            model_id="gpt-5.2",
            selected_effort="max",
        )

    warnings = _warnings(caplog)
    if not warns:
        assert warnings == []
        return
    assert len(warnings) == 1
    assert "gpt-5.2" in warnings[0]
    assert "max" in warnings[0]


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        pytest.param({"completion_tokens_details": {"reasoning_tokens": 7}}, 7, id="chat"),
        pytest.param({"output_tokens_details": {"reasoning_tokens": 0}}, 0, id="responses"),
        pytest.param({"output_tokens_details": {"thinking_tokens": 5}}, 5, id="thinking"),
        pytest.param(None, None, id="no-usage"),
        pytest.param({}, None, id="no-details"),
        pytest.param({"completion_tokens_details": {}}, None, id="no-counter"),
        # A boolean is not a token count.
        pytest.param({"completion_tokens_details": {"reasoning_tokens": True}}, None, id="boolean"),
    ],
)
def test_reasoning_token_count(usage: dict[str, Any] | None, expected: int | None) -> None:
    assert reasoning_token_count(usage) == expected


_EFFORT_HIGH = ReasoningIntent(REASONING_INTENT_EFFORT, effort_level="high")


@pytest.mark.parametrize(
    ("rendered", "usage", "returned_reasoning", "expected_label"),
    [
        pytest.param(_EFFORT_HIGH, _ZERO_REASONING_USAGE, False, "high", id="effort"),
        pytest.param(
            ReasoningIntent(REASONING_INTENT_BUDGET, effort_level="high", budget_tokens=16384),
            _ZERO_REASONING_USAGE,
            False,
            "budget:16384",
            id="budget",
        ),
        pytest.param(
            ReasoningIntent(REASONING_INTENT_ON, effort_level="high"),
            _ZERO_REASONING_USAGE,
            False,
            "on",
            id="on",
        ),
        pytest.param(
            _EFFORT_HIGH,
            {"completion_tokens_details": {"reasoning_tokens": 42}},
            False,
            None,
            id="silent-with-reasoning-tokens",
        ),
        # Off (including a catalog non-reasoning strip) and default expect no reasoning.
        pytest.param(
            ReasoningIntent(REASONING_INTENT_OFF),
            _ZERO_REASONING_USAGE,
            False,
            None,
            id="silent-when-off",
        ),
        pytest.param(
            ReasoningIntent(REASONING_INTENT_DEFAULT),
            _ZERO_REASONING_USAGE,
            False,
            None,
            id="silent-when-default",
        ),
        # A zero counter cannot deny Reasoning the response actually returned.
        pytest.param(
            _EFFORT_HIGH, _ZERO_REASONING_USAGE, True, None, id="silent-when-reasoning-returned"
        ),
        # Sparse usage (no reasoning-token counter) is unknown, not swallowed.
        pytest.param(
            _EFFORT_HIGH,
            {"prompt_tokens": 10, "completion_tokens": 5},
            False,
            None,
            id="silent-when-count-unknown",
        ),
    ],
)
def test_warn_effort_swallowed_only_when_requested_reasoning_yields_zero_tokens(
    caplog: pytest.LogCaptureFixture,
    rendered: ReasoningIntent,
    usage: dict[str, Any],
    returned_reasoning: bool,
    expected_label: str | None,
) -> None:
    with caplog.at_level(logging.WARNING, logger=_REASONING_LOGGER):
        warn_effort_swallowed(
            rendered=rendered,
            usage=usage,
            returned_reasoning=returned_reasoning,
            model_id="gpt-5.2",
        )

    warnings = _warnings(caplog)
    if expected_label is None:
        assert warnings == []
        return
    assert len(warnings) == 1
    assert "gpt-5.2" in warnings[0]
    assert f"rendered_reasoning={expected_label}" in warnings[0]
