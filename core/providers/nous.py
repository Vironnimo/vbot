"""Nous Portal provider policy on the shared OpenAI-compatible transport."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import httpx

from core.models.models import Model, ReasoningCapabilities
from core.providers.errors import CatalogEntrySkipped, ProviderError
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.reasoning import (
    REASONING_INTENT_BUDGET,
    REASONING_INTENT_EFFORT,
    REASONING_INTENT_ON,
    ReasoningIntent,
)

NOUS_MAX_OUTPUT_TOKENS = 32_000
NOUS_HERMES_MODEL_MARKER = "hermes"
NOUS_UNDOCUMENTED_SAMPLING_PARAMETERS = frozenset(
    {
        "frequency_penalty",
        "logit_bias",
        "logprobs",
        "n",
        "presence_penalty",
        "seed",
        "stop",
        "top_k",
        "top_logprobs",
        "top_p",
    }
)
NOUS_OUTPUT_PARAMETER_NAMES = ("max_tokens", "max_completion_tokens", "max_output_tokens")


class NousAdapter(OpenAICompatibleAdapter):
    """OpenAI Chat Completions plus Nous Portal's documented gateway limits."""

    @classmethod
    def normalize_catalog_entry(
        cls,
        raw: Mapping[str, Any],
        defaults: Mapping[str, Any] | None = None,
    ) -> Model:
        """Project the mixed Portal catalog without inventing agent capabilities."""

        model = super().normalize_catalog_entry(raw, defaults)
        if NOUS_HERMES_MODEL_MARKER in model.model_id.casefold():
            # Nous explicitly describes its Hermes family as chat/reasoning
            # models, not models for an agentic Tool loop. Keep the live entry
            # in the raw audit, but do not offer it as a vBot Model.
            raise CatalogEntrySkipped(
                f"Nous model '{model.model_id}' is not recommended for agentic Tool use"
            )

        supported_parameters = _catalog_string_set(raw.get("supported_parameters"))
        capabilities = replace(
            model.capabilities,
            tools=(
                "tools" in supported_parameters
                if supported_parameters
                else _catalog_capability(raw, "tools")
            ),
            json_mode=(
                bool({"response_format", "structured_outputs"} & supported_parameters)
                if supported_parameters
                else _catalog_capability(raw, "json_mode")
            ),
            reasoning=ReasoningCapabilities(
                supported=(
                    bool(
                        {"reasoning", "include_reasoning", "reasoning_effort"}
                        & supported_parameters
                    )
                    if supported_parameters
                    else _catalog_capability(raw, "reasoning")
                )
            ),
        )
        return replace(
            model,
            capabilities=capabilities,
            max_output_tokens=min(
                model.max_output_tokens or NOUS_MAX_OUTPUT_TOKENS,
                NOUS_MAX_OUTPUT_TOKENS,
            ),
        )

    def wire_media_support(self, model_id: str) -> frozenset[str]:
        """Declare no native media until the Portal wire documents multipart content."""

        del model_id
        return frozenset()

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        payload = super()._build_payload(messages, model_id, **kwargs)
        for parameter in NOUS_UNDOCUMENTED_SAMPLING_PARAMETERS:
            payload.pop(parameter, None)

        temperature = payload.get("temperature")
        if temperature is not None and (
            isinstance(temperature, bool)
            or not isinstance(temperature, int | float)
            or not 0 <= float(temperature) <= 2
        ):
            raise ProviderError(
                "Nous temperature must be a number between 0 and 2",
                retryable=False,
            )

        output_limits = [
            int(payload[name])
            for name in NOUS_OUTPUT_PARAMETER_NAMES
            if isinstance(payload.get(name), int)
            and not isinstance(payload.get(name), bool)
            and int(payload[name]) > 0
        ]
        for name in NOUS_OUTPUT_PARAMETER_NAMES:
            payload.pop(name, None)
        payload["max_tokens"] = min(
            output_limits or [NOUS_MAX_OUTPUT_TOKENS],
            default=NOUS_MAX_OUTPUT_TOKENS,
        )
        payload["max_tokens"] = min(payload["max_tokens"], NOUS_MAX_OUTPUT_TOKENS)
        return payload

    def _render_reasoning(
        self,
        payload: dict[str, Any],
        intent: ReasoningIntent,
        *,
        reasoning_supported: bool | None,
    ) -> None:
        """Render Nous' reasoning object; disabled reasoning is represented by omission."""

        del reasoning_supported
        if intent.kind not in (
            REASONING_INTENT_BUDGET,
            REASONING_INTENT_EFFORT,
            REASONING_INTENT_ON,
        ):
            return
        reasoning: dict[str, Any] = {"enabled": True}
        if intent.effort_level is not None:
            reasoning["effort"] = intent.effort_level
        payload["reasoning"] = reasoning

    def _classify_http_status(
        self,
        status_code: int,
        *,
        detail: str,
        response_headers: httpx.Headers,
    ) -> None:
        if status_code == 402:
            raise ProviderError(
                f"Nous Portal payment or subscription entitlement required: {detail}",
                retryable=False,
            )
        super()._classify_http_status(
            status_code,
            detail=detail,
            response_headers=response_headers,
        )


def _catalog_string_set(value: Any) -> frozenset[str]:
    if not isinstance(value, list):
        return frozenset()
    return frozenset(item for item in value if isinstance(item, str) and item)


def _catalog_capability(raw: Mapping[str, Any], name: str) -> bool:
    capabilities = raw.get("capabilities")
    if isinstance(capabilities, Mapping) and capabilities.get(name) is True:
        return True
    direct_names = {
        "tools": ("supports_tools", "supports_tool_calls", "supports_function_calling"),
        "json_mode": ("supports_json_mode", "supports_structured_outputs"),
        "reasoning": ("supports_reasoning",),
    }
    return any(raw.get(key) is True for key in direct_names[name])
