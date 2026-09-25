"""Catalog token prices and per-request USD estimates owned by the Model DB.

Canonical input includes cache reads and writes; reasoning is part of output.
Prices describe API-equivalent usage, never a subscription invoice. A missing
rate for a used bucket is unknown, and an explicit zero is a real zero.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any


def nonnegative_amount(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return float(value) if math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


@dataclass(frozen=True)
class TokenRates:
    input: float | None = None
    output: float | None = None
    cache_read: float | None = None
    cache_write: float | None = None
    reasoning: float | None = None

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> TokenRates:
        return cls(**{key: nonnegative_amount(raw.get(key)) for key in cls.__dataclass_fields__})


@dataclass(frozen=True)
class PriceTier:
    above_tokens: int
    rates: TokenRates


@dataclass(frozen=True)
class TokenPricing:
    source: str
    rates: TokenRates
    tiers: tuple[PriceTier, ...] = ()
    supported: bool = True

    @classmethod
    def from_dict(cls, raw: Any) -> TokenPricing | None:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("rates"), Mapping):
            return None
        source = raw.get("source")
        if not isinstance(source, str) or not source:
            return None
        return cls.from_cost(raw["rates"], source=source)

    @classmethod
    def from_cost(cls, cost: Any, *, source: str) -> TokenPricing | None:
        if not isinstance(cost, Mapping):
            return None
        rates = TokenRates.parse(cost)
        if rates.input is None and rates.output is None:
            return None
        tiers: list[PriceTier] = []
        supported = True
        raw_tiers = cost.get("tiers")
        if raw_tiers is None and isinstance(cost.get("context_over_200k"), Mapping):
            raw_tiers = [{**cost["context_over_200k"], "tier": {"type": "context", "size": 200000}}]
        if raw_tiers is not None:
            if not isinstance(raw_tiers, list):
                supported = False
            else:
                for entry in raw_tiers:
                    tier = entry.get("tier") if isinstance(entry, Mapping) else None
                    size = _count(tier.get("size")) if isinstance(tier, Mapping) else None
                    if (
                        not isinstance(tier, Mapping)
                        or size is None
                        or tier.get("type") != "context"
                    ):
                        supported = False
                        continue
                    tiers.append(PriceTier(size, TokenRates.parse({**cost, **entry})))
        return cls(
            source, rates, tuple(sorted(tiers, key=lambda tier: tier.above_tokens)), supported
        )

    def to_dict(self) -> dict[str, Any]:
        rates = {key: value for key, value in asdict(self.rates).items() if value is not None}
        if self.tiers:
            rates["tiers"] = [
                {
                    **{
                        key: value for key, value in asdict(tier.rates).items() if value is not None
                    },
                    "tier": {"type": "context", "size": tier.above_tokens},
                }
                for tier in self.tiers
            ]
        if not self.supported:
            # Preserve an unsupported schedule as unknown through serialization.
            rates["tiers"] = "unsupported"
        return {"source": self.source, "rates": rates}


def project_cost(raw: Any) -> dict[str, Any] | None:
    """Retain only validated accounting fields in derived projections."""
    if not isinstance(raw, Mapping):
        return None
    source = raw.get("source")
    amount = nonnegative_amount(raw.get("amount_usd"))
    if not isinstance(source, str) or source not in {"provider", "catalog"} or amount is None:
        reason = raw.get("reason")
        if not isinstance(reason, str) or reason not in {
            "missing_usage",
            "missing_price",
            "unsupported_tier",
            "invalid_cache",
            "invalid_reasoning",
            "missing_reasoning_usage",
            "missing_bucket_price",
        }:
            reason = "missing_price"
        return {
            "amount_usd": None,
            "source": "unknown",
            "reason": reason,
        }
    result: dict[str, Any] = {"amount_usd": amount, "source": source}
    pricing = TokenPricing.from_dict(raw.get("pricing"))
    if source == "catalog":
        result["estimated_tokens"] = bool(raw.get("estimated_tokens", False))
        if pricing:
            result["pricing"] = pricing.to_dict()
    return result


def price_usage(usage: Mapping[str, Any] | None, pricing: TokenPricing | None) -> dict[str, Any]:
    """Snapshot a cost for one canonical Usage; never sum request tiers first."""
    usage = usage or {}
    reported = nonnegative_amount(usage.get("reported_cost_usd"))
    if reported is not None:
        return {"amount_usd": reported, "source": "provider"}

    unknown: dict[str, Any] = {"amount_usd": None, "source": "unknown"}
    input_tokens = _count(usage.get("input_tokens"))
    output_tokens = _count(usage.get("output_tokens"))
    if input_tokens is None or output_tokens is None:
        return {**unknown, "reason": "missing_usage"}
    if pricing is None:
        return {**unknown, "reason": "missing_price"}
    if not pricing.supported:
        return {**unknown, "reason": "unsupported_tier"}

    rates = pricing.rates
    for tier in pricing.tiers:
        if input_tokens > tier.above_tokens:
            rates = tier.rates
    read = _count(usage.get("cache_read_tokens", 0))
    write = _count(usage.get("cache_write_tokens", 0))
    if read is None or write is None or read + write > input_tokens:
        return {**unknown, "reason": "invalid_cache"}
    reasoning = _count(usage.get("reasoning_tokens", 0))
    if reasoning is None or reasoning > output_tokens:
        return {**unknown, "reason": "invalid_reasoning"}
    if rates.reasoning is not None and "reasoning_tokens" not in usage:
        return {**unknown, "reason": "missing_reasoning_usage"}
    buckets = [
        (input_tokens - read - write, rates.input),
        (read, rates.cache_read),
        (write, rates.cache_write),
        (output_tokens - reasoning if rates.reasoning is not None else output_tokens, rates.output),
        (reasoning if rates.reasoning is not None else 0, rates.reasoning),
    ]
    if any(tokens and rate is None for tokens, rate in buckets):
        return {**unknown, "reason": "missing_bucket_price"}
    amount = sum(
        (
            Decimal(tokens) * Decimal(str(rate)) / Decimal(1_000_000)
            for tokens, rate in buckets
            if tokens
        ),
        Decimal(0),
    )
    estimated = usage.get("estimated") is True
    return {
        "amount_usd": float(amount),
        "source": "catalog",
        "pricing": pricing.to_dict(),
        "estimated_tokens": estimated,
    }
