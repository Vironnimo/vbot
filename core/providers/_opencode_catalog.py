"""OpenCode Go and Zen catalog records and protocol-specific API bases."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from core.models.models import (
    Capabilities,
    Model,
    ReasoningCapabilities,
    text_embedding_capabilities,
)
from core.models.pricing import TokenPricing, nonnegative_amount
from core.providers.errors import CatalogEntrySkipped
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.reasoning import normalize_thinking_effort


def opencode_api_base(base_url: str, api: str) -> str:
    """Select a sibling protocol on the configured gateway and billing route."""
    base = urlsplit(base_url)
    root = base.path.rstrip("/")
    for suffix in ("/openai/v1", "/anthropic/v1", "/google/v1beta", "/xai/v1", "/v1"):
        if root.endswith(suffix):
            root = root.removesuffix(suffix)
            break
    return urlunsplit((base.scheme, base.netloc, f"{root}/{api}", "", ""))


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


def _text_pricing(raw: Mapping[str, Any], provider_id: str, model_id: str) -> TokenPricing | None:
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
    return TokenPricing.from_cost(rates, source=f"{provider_id}:{model_id}")


_CHAT_PROTOCOLS = {
    "chat-completions": "chat_completions",
    "responses": "responses",
    "messages": "messages",
    "generate-content": "gemini",
}
_SAMPLING = ("temperature", "top_p", "top_k")


def _chat_api(raw: Mapping[str, Any]) -> tuple[str | None, Mapping[str, Any]]:
    api = _mapping(raw.get("api"))
    selected = raw.get("recommended_api")
    if isinstance(selected, str) and selected in _CHAT_PROTOCOLS and (not api or selected in api):
        return _CHAT_PROTOCOLS[selected], _mapping(api.get(selected))
    if selected == "interactions" and "generate-content" in api:
        return "gemini", _mapping(api["generate-content"])
    for name in api:
        if name in _CHAT_PROTOCOLS:
            return _CHAT_PROTOCOLS[name], _mapping(api[name])
    if api or selected:
        raise CatalogEntrySkipped("OpenCode chat Model has no supported native route")
    return None, {}


def _chat_modalities(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    result = []
    for item in value:
        if isinstance(item, str):
            kind = "pdf" if item == "application/pdf" else item.partition("/")[0]
            if kind in ("text", "image", "audio", "video", "pdf") and kind not in result:
                result.append(kind)
    return tuple(result)


def _chat_reasoning(
    raw: Mapping[str, Any], fallback: ReasoningCapabilities
) -> ReasoningCapabilities:
    supported = raw.get("supported")
    if not isinstance(supported, bool):
        return fallback
    if supported:
        efforts = raw.get("effort")
        if isinstance(efforts, list):
            levels = tuple(level for item in efforts if (level := normalize_thinking_effort(item)))
            if levels:
                return ReasoningCapabilities(True, control="levels", levels=levels)
        budget = raw.get("budget")
        if isinstance(budget, Mapping):
            return ReasoningCapabilities(
                True, control="budget", budget_max=_limit(budget.get("max"))
            )
        if raw.get("toggle") is True:
            return ReasoningCapabilities(True, control="on_off")
    return ReasoningCapabilities(supported)


def _chat_model(
    raw: Mapping[str, Any], defaults: Mapping[str, Any] | None, provider_id: str
) -> Model:
    model = OpenAICompatibleAdapter.normalize_catalog_entry(raw, defaults)
    protocol, api = _chat_api(raw)
    facts: dict[str, Any] = {}
    if protocol:
        facts["catalog_protocol"] = protocol
    field = _mapping(api.get("compatibility")).get("reasoning_field")
    if isinstance(field, str) and field:
        facts["interleaved_field"] = field

    limits = _mapping(raw.get("limit"))
    if input_limit := _limit(limits.get("input")):
        facts["max_input_tokens"] = input_limit
    caps = _mapping(raw.get("capabilities"))
    tools = _mapping(caps.get("tools")).get("supported")
    if tools is False:
        facts["tool_calls"] = False
    structured = caps.get("structured_output")
    sampling = {key: caps[key] for key in _SAMPLING if isinstance(caps.get(key), bool)}
    if sampling:
        facts["sampling"] = sampling
    parameters = set(model.capabilities.supported_parameters)
    for key, supported in {**sampling, "tools": tools, "response_format": structured}.items():
        if supported is True:
            parameters.add(key)
        elif supported is False:
            parameters.discard(key)
    if isinstance(structured, bool):
        facts["structured_output"] = structured
    modalities = _mapping(raw.get("modalities"))
    inputs = _chat_modalities(modalities.get("input"))
    outputs = _chat_modalities(modalities.get("output"))
    # Projection may widen a legacy endpoint's text-only default from models.dev.
    # These published lists describe the gateway's actual inputs and outputs.
    exact = {
        f"{direction}_modalities": values
        for direction in ("input", "output")
        if isinstance(values := modalities.get(direction), list)
        and all(isinstance(value, str) and value for value in values)
    }
    if exact:
        facts["reported_modalities"] = exact
    family = raw.get("family")
    return replace(
        model,
        family=family if isinstance(family, str) and family else model.family,
        metadata={provider_id.replace("-", "_"): facts} if facts else {},
        context_window=_limit(limits.get("context")) or model.context_window,
        max_output_tokens=_limit(limits.get("output")) or model.max_output_tokens,
        capabilities=replace(
            model.capabilities,
            vision="image" in inputs if inputs else model.capabilities.vision,
            input_modalities=inputs or model.capabilities.input_modalities,
            output_modalities=outputs or model.capabilities.output_modalities,
            supported_parameters=tuple(parameters),
            task_types=(),
            tools=tools if isinstance(tools, bool) else model.capabilities.tools,
            json_mode=structured if isinstance(structured, bool) else model.capabilities.json_mode,
            reasoning=_chat_reasoning(
                _mapping(caps.get("reasoning")), model.capabilities.reasoning
            ),
        ),
    )


def catalog_model(
    raw: Mapping[str, Any], defaults: Mapping[str, Any] | None, provider_id: str
) -> Model:
    """Prefer the gateway's exact chat API and expose only executable task protocols."""
    kind = raw.get("type")
    if kind in (None, "chat"):
        return _chat_model(raw, defaults, provider_id)
    model_id = raw.get("id")
    if not isinstance(model_id, str) or not model_id.strip():
        raise CatalogEntrySkipped("OpenCode task Model has no id")
    limits = _mapping(raw.get("limit"))
    api = _mapping(raw.get("api"))
    options: dict[str, Any] = {}
    metadata: dict[str, Any] = {}
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    if kind == "embedding" and "embeddings" in api:
        return Model(
            model_id=model_id,
            name=str(raw.get("name") or model_id),
            capabilities=text_embedding_capabilities(),
            pricing=_text_pricing(raw, provider_id, model_id),
            context_window=_limit(limits.get("input")) or _limit(limits.get("context")),
            max_output_tokens=None,
        )
    if kind == "decision" and "systemone" in api:
        task, inputs, outputs = "decision", ("text",), ("decisions",)
        if maximum := _limit(limits.get("input")):
            options[task] = {"limits": {"max_input_tokens": maximum}}
    elif provider_id == "opencode-zen" and kind == "image" and "images" in api:
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
        pricing=_text_pricing(raw, provider_id, model_id) if task == "decision" else None,
        context_window=_limit(limits.get("context")),
        max_output_tokens=_limit(limits.get("output")),
    )
