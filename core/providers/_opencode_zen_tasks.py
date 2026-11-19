"""Specialized OpenCode Console catalog records and native endpoint bases."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.models.pricing import TokenPricing, nonnegative_amount
from core.providers.errors import CatalogEntrySkipped


def zen_api_base(base_url: str, api: str) -> str:
    """Keep the configured origin. Never send a credential to a host from the feed."""
    base = urlsplit(base_url)
    return urlunsplit((base.scheme, base.netloc, f"/inference/{api}", "", ""))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _limit(value: Any) -> int | None:
    return value if type(value) is int and value > 0 else None


def _rates(direction: str, price: Mapping[str, Any]) -> dict[str, float | None]:
    rates = {direction: nonnegative_amount(price.get("price"))}
    if direction == "input" and (cache := _mapping(price.get("cache"))):
        rates["cache_read"] = nonnegative_amount(cache.get("read"))
        # Usage carries one aggregate cache-write count. Distinct retention
        # prices cannot be valued unless the Provider reports the actual cost.
        writes = cache.get("write")
        if isinstance(writes, Mapping):
            values = {nonnegative_amount(value) for value in writes.values()}
            rates["cache_write"] = values.pop() if len(values) == 1 else None
        else:
            rates["cache_write"] = nonnegative_amount(writes)
    return rates


def _decision_pricing(raw: Mapping[str, Any], model_id: str) -> TokenPricing | None:
    prices = _mapping(_mapping(raw.get("cost")).get("text"))
    rates: dict[str, Any] = {}
    tiers: dict[int, dict[str, Any]] = {}
    for direction in ("input", "output"):
        price = _mapping(prices.get(direction))
        rates.update(_rates(direction, price))
        overrides = price.get("overrides", [])
        if not isinstance(overrides, list):
            return None
        for override in overrides:
            when = _mapping(_mapping(override).get("when"))
            context = _mapping(when.get("context"))
            threshold = _limit(context.get("gt"))
            if set(when) != {"context"} or set(context) != {"gt"} or threshold is None:
                # A price condition outside vBot's context-tier format cannot
                # be safely estimated. Reported usage costs are authoritative.
                return None
            tiers.setdefault(threshold, {}).update(_rates(direction, override))
    if tiers:
        rates["tiers"] = [
            {**values, "tier": {"type": "context", "size": threshold}}
            for threshold, values in tiers.items()
        ]
    return TokenPricing.from_cost(rates, source=f"opencode-zen:{model_id}")


def task_model(raw: Mapping[str, Any]) -> Model | None:
    """Recognize executable Console task protocols before applying chat defaults."""
    kind = raw.get("type")
    if kind in (None, "chat"):
        return None
    model_id = raw.get("id")
    if not isinstance(model_id, str) or not model_id.strip():
        raise CatalogEntrySkipped("OpenCode task Model has no id")
    limits = _mapping(raw.get("limit"))
    api = _mapping(raw.get("api"))
    options: dict[str, Any] = {}
    metadata: dict[str, Any] = {}
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    if kind == "decision" and "systemone" in api:
        task, inputs, outputs = "decision", ("text",), ("decisions",)
        if maximum := _limit(limits.get("input")):
            options[task] = {"limits": {"max_input_tokens": maximum}}
    elif kind == "image" and "images" in api:
        image_sdk = _mapping(_mapping(api["images"]).get("sdk")).get("aisdk")
        if image_sdk not in ("@ai-sdk/google", "@ai-sdk/openai", "@ai-sdk/xai"):
            raise CatalogEntrySkipped("OpenCode image route has no supported SDK")
        metadata["opencode_zen"] = {"image_sdk": image_sdk}
        task, inputs, outputs = "image_generation", ("text", "image"), ("image",)
    else:
        raise CatalogEntrySkipped("OpenCode task Model has no supported native route")
    return Model(
        model_id=model_id,
        name=str(raw.get("name") or model_id),
        capabilities=Capabilities(
            vision=False,
            tools=False,
            json_mode=False,
            reasoning=ReasoningCapabilities(supported=False),
            input_modalities=inputs,
            output_modalities=outputs,
            task_types=(task,),
            task_options=options,
        ),
        metadata=metadata,
        pricing=_decision_pricing(raw, model_id) if task == "decision" else None,
        context_window=_limit(limits.get("context")),
        max_output_tokens=_limit(limits.get("output")),
    )
