"""Value native Decision usage at its task price, independently of Chat."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.models.pricing import TokenPricing, nonnegative_amount, price_usage, project_cost


def pricing_for_decision(model: Any | None) -> TokenPricing | None:
    """Read a task price, or the exact price of a Decisions-only catalog Model.

    Task facts use the ordinary TokenPricing format. Loaded facts are frozen,
    so their tuple-backed Context tiers are restored to the parser's JSON shape.
    A Chat Model's general price never establishes its Decisions endpoint price.
    """
    capabilities = getattr(model, "capabilities", None)
    tasks = getattr(capabilities, "task_options", {})
    facts = tasks.get("decision", {}) if isinstance(tasks, Mapping) else {}
    if isinstance(facts, Mapping) and "pricing" in facts:
        return TokenPricing.from_dict(_json_shape(facts["pricing"]))
    if "decisions" in getattr(capabilities, "output_modalities", ()):
        pricing = getattr(model, "pricing", None)
        if isinstance(pricing, TokenPricing):
            return pricing
    return None


def price_decision_usage(usage: Mapping[str, Any], pricing: TokenPricing | None) -> dict[str, Any]:
    """Copy measured Usage with an explicit, honestly sourced cost snapshot.

    Both raw observers and validated results use this boundary: native cache
    counters are included before valuing a result that later fails validation.
    Unknown task prices explicitly prevent the ledger's general Chat-price
    fallback. A finite Provider-reported charge remains authoritative.
    """
    result = dict(usage)
    details = usage.get("input_tokens_details")
    if isinstance(details, Mapping):
        for wire_name, canonical_name in (
            ("cached_tokens", "cache_read_tokens"),
            ("cache_write_tokens", "cache_write_tokens"),
        ):
            if canonical_name not in result and wire_name in details:
                result[canonical_name] = details[wire_name]
    reported = nonnegative_amount(result.get("reported_cost_usd"))
    if reported is None:
        reported = nonnegative_amount(result.get("cost"))
    if reported is not None:
        result["reported_cost_usd"] = reported
    elif (existing := project_cost(result.get("cost"))) is not None and existing[
        "source"
    ] == "provider":
        result["cost"] = existing
        return result
    result["cost"] = price_usage(result, pricing)
    return result


def _json_shape(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _json_shape(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_shape(item) for item in value]
    return value
