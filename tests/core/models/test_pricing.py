"""Per-call pricing preserves provider amounts and honest unknown coverage."""

from __future__ import annotations

import math

import pytest

from core.models.pricing import TokenPricing, nonnegative_amount, price_usage, project_cost


def pricing(**rates):
    result = TokenPricing.from_cost(rates, source="models.dev:fixture/model")
    assert result is not None
    return result


def test_cache_is_in_input_and_reasoning_is_in_output():
    price = pricing(input=2, output=10, cache_read=0.2, cache_write=2.5)
    result = price_usage(
        {
            "input_tokens": 1000,
            "output_tokens": 100,
            "cache_read_tokens": 600,
            "cache_write_tokens": 200,
            "reasoning_tokens": 70,
        },
        price,
    )
    assert result["amount_usd"] == pytest.approx(0.00202)
    assert result["source"] == "catalog"
    assert TokenPricing.from_dict(result["pricing"]) == price


def test_separate_reasoning_price_replaces_normal_output_rate():
    result = price_usage(
        {"input_tokens": 0, "output_tokens": 100, "reasoning_tokens": 80},
        pricing(input=1, output=2, reasoning=4),
    )
    assert result["amount_usd"] == pytest.approx(0.00036)


def test_tiers_apply_per_call_and_use_full_input_including_cache():
    price = pricing(
        input=1,
        output=2,
        cache_read=0.1,
        tiers=[{"tier": {"type": "context", "size": 1000}, "input": 3, "output": 6}],
    )
    small = {"input_tokens": 1000, "output_tokens": 100}
    large = {"input_tokens": 1001, "output_tokens": 100, "cache_read_tokens": 900}
    assert price_usage(small, price)["amount_usd"] == pytest.approx(0.0012)
    assert price_usage(large, price)["amount_usd"] == pytest.approx(0.000993)
    assert TokenPricing.from_dict(price.to_dict()) == price
    assert (
        price_usage({"input_tokens": 2000, "output_tokens": 200}, price)["amount_usd"]
        != 2 * price_usage(small, price)["amount_usd"]
    )


def test_explicit_tiers_take_precedence_over_legacy_200k():
    price = pricing(
        input=1,
        output=1,
        context_over_200k={"input": 99},
        tiers=[{"tier": {"type": "context", "size": 272000}, "input": 2}],
    )
    assert price_usage({"input_tokens": 250000, "output_tokens": 0}, price)["amount_usd"] == 0.25


@pytest.mark.parametrize("amount", [0, 0.002])
def test_reported_amount_takes_precedence_even_without_tokens(amount):
    assert price_usage({"reported_cost_usd": amount}, None) == {
        "amount_usd": amount,
        "source": "provider",
    }


@pytest.mark.parametrize("value", [True, -1, "1.2", math.inf, math.nan, 10**1000, None])
def test_invalid_money_is_not_reported_as_real(value):
    assert nonnegative_amount(value) is None
    assert price_usage({"reported_cost_usd": value}, None)["source"] == "unknown"


@pytest.mark.parametrize(
    ("usage", "rates", "reason"),
    [
        ({"input_tokens": 2}, {"input": 1}, "missing_usage"),
        ({"input_tokens": 2, "output_tokens": 1}, {}, "missing_price"),
        (
            {"input_tokens": 2, "output_tokens": 1, "cache_read_tokens": 1},
            {"input": 1, "output": 1},
            "missing_bucket_price",
        ),
        (
            {"input_tokens": 2, "output_tokens": 1, "cache_read_tokens": 3},
            {"input": 1, "output": 1},
            "invalid_cache",
        ),
        (
            {"input_tokens": 2, "output_tokens": 1, "reasoning_tokens": 2},
            {"input": 1, "output": 1},
            "invalid_reasoning",
        ),
        (
            {"input_tokens": 2, "output_tokens": 1},
            {"input": 1, "output": 1, "reasoning": 2},
            "missing_reasoning_usage",
        ),
        (
            {"input_tokens": 2, "output_tokens": 1},
            {"input": 1, "output": 1, "tiers": [{"tier": {"type": "output", "size": 100}}]},
            "unsupported_tier",
        ),
    ],
)
def test_unpriced_calls_explain_the_missing_information(usage, rates, reason):
    result = price_usage(usage, TokenPricing.from_cost(rates, source="models.dev:test/model"))
    assert result == {"amount_usd": None, "source": "unknown", "reason": reason}


def test_zero_rate_is_free_but_absent_used_rate_is_unknown():
    assert (
        price_usage({"input_tokens": 10, "output_tokens": 10}, pricing(input=0, output=0))[
            "amount_usd"
        ]
        == 0
    )
    assert (
        price_usage({"input_tokens": 10, "output_tokens": 0}, pricing(input=0))["amount_usd"] == 0
    )


def test_projection_keeps_only_accounting_fields():
    snapshot = price_usage(
        {
            "input_tokens": 100,
            "output_tokens": 10,
            "input_tokens_estimated": True,
            "estimated": True,
        },
        pricing(input=1, output=2),
    )
    assert snapshot["estimated_tokens"] is True
    snapshot["private_extra"] = "not indexed"
    snapshot["pricing"]["rates"]["private_extra"] = "not indexed"
    projected = project_cost(snapshot)
    assert projected is not None
    assert "private_extra" not in str(projected)


@pytest.mark.parametrize("source", [[], {}, None, 0, "invalid"])
def test_malformed_saved_cost_does_not_break_statistics(source):
    assert project_cost({"source": source, "amount_usd": 0, "reason": {"private": "data"}}) == {
        "amount_usd": None,
        "source": "unknown",
        "reason": "missing_price",
    }
