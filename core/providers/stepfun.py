"""StepFun direct API and Step Plan policy on the shared Chat Completions wire."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.adapter import IMAGE_WIRE_MEDIA_TYPES
from core.providers.errors import CatalogEntrySkipped, ProviderError
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.providers import ConnectionConfig

STEPFUN_DIRECT_MODE = "direct_api"
STEPFUN_PLAN_MODE = "step_plan"
STEPFUN_CONTEXT_WINDOW = 256_000
STEPFUN_ROUTER_MAX_OUTPUT_TOKENS = 250_000
STEPFUN_DEFAULT_TEMPERATURE = 0.5

_COMMON_PARAMETERS = (
    "frequency_penalty",
    "max_tokens",
    "n",
    "reasoning_format",
    "response_format",
    "stop",
    "temperature",
    "tool_choice",
    "tools",
    "top_p",
)
_UNSUPPORTED_OPENAI_PARAMETERS = frozenset(
    {
        "logit_bias",
        "logprobs",
        "parallel_tool_calls",
        "presence_penalty",
        "seed",
        "stream_options",
        "top_k",
        "top_logprobs",
    }
)
_OUTPUT_PARAMETER_NAMES = ("max_tokens", "max_completion_tokens", "max_output_tokens")


@dataclass(frozen=True)
class _StepFunModelPolicy:
    name: str
    input_modalities: tuple[str, ...]
    reasoning_levels: tuple[str, ...]
    max_output_tokens: int = STEPFUN_CONTEXT_WINDOW

    @property
    def supported_parameters(self) -> tuple[str, ...]:
        if self.reasoning_levels:
            return (*_COMMON_PARAMETERS, "reasoning_effort")
        return _COMMON_PARAMETERS


STEPFUN_MODEL_POLICIES: Mapping[str, _StepFunModelPolicy] = {
    "step-3.5-flash": _StepFunModelPolicy(
        name="Step 3.5 Flash",
        input_modalities=("text",),
        reasoning_levels=(),
    ),
    "step-3.5-flash-2603": _StepFunModelPolicy(
        name="Step 3.5 Flash 2603",
        input_modalities=("text",),
        reasoning_levels=("low", "high"),
    ),
    "step-3.7-flash": _StepFunModelPolicy(
        name="Step 3.7 Flash",
        input_modalities=("text", "image", "video"),
        reasoning_levels=("low", "medium", "high"),
    ),
    "step-router-v1": _StepFunModelPolicy(
        name="Step Router V1 (Step Plan routing)",
        input_modalities=("text",),
        reasoning_levels=("low", "medium", "high"),
        max_output_tokens=STEPFUN_ROUTER_MAX_OUTPUT_TOKENS,
    ),
}


class StepFunAdapter(OpenAICompatibleAdapter):
    """StepFun's documented Chat Completions policy and current Model allowlist."""

    @classmethod
    def accepts_discovered_model(
        cls,
        raw: Mapping[str, Any],
        connection: ConnectionConfig | None,
    ) -> bool:
        """Keep the routing Model out of non-Plan discovery projections."""

        del cls
        if raw.get("id") != "step-router-v1":
            return True
        return getattr(connection, "mode", None) == STEPFUN_PLAN_MODE

    @classmethod
    def normalize_catalog_entry(
        cls,
        raw: Mapping[str, Any],
        defaults: Mapping[str, Any] | None = None,
    ) -> Model:
        """Project only current agentic Chat Models; raw discovery keeps all entries."""

        del defaults
        model_id = raw.get("id")
        if not isinstance(model_id, str) or not model_id:
            raise ValueError("Expected 'id' to be a non-empty string")
        policy = STEPFUN_MODEL_POLICIES.get(model_id)
        if policy is None:
            raise CatalogEntrySkipped(
                f"StepFun model '{model_id}' is outside the current agentic Chat allowlist"
            )
        raw_name = raw.get("name")
        name = raw_name if isinstance(raw_name, str) and raw_name else policy.name
        return Model(
            model_id=model_id,
            name=name,
            capabilities=Capabilities(
                vision="image" in policy.input_modalities,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(
                    supported=True,
                    control="levels" if policy.reasoning_levels else None,
                    levels=policy.reasoning_levels,
                ),
                input_modalities=policy.input_modalities,
                output_modalities=("text",),
                supported_parameters=policy.supported_parameters,
            ),
            context_window=STEPFUN_CONTEXT_WINDOW,
            max_output_tokens=policy.max_output_tokens,
            metadata={
                "stepfun": {
                    "prompt_cache": "automatic",
                    "prompt_cache_min_tokens": 256,
                }
            },
        )

    def wire_media_support(self, model_id: str) -> frozenset[str]:
        """Carry StepFun's documented image formats; vBot has no video wire encoder."""

        del model_id
        return IMAGE_WIRE_MEDIA_TYPES

    def _supported_reasoning_efforts(self, model_id: str) -> tuple[str, ...]:
        policy = STEPFUN_MODEL_POLICIES.get(model_id.split("::", 1)[0])
        if policy is not None:
            return policy.reasoning_levels
        return tuple(super()._supported_reasoning_efforts(model_id))

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        model_name = model_id.split("::", 1)[0]
        if model_name == "step-router-v1" and self._connection_mode != STEPFUN_PLAN_MODE:
            raise ProviderError(
                "Step Router V1 is available only through the Step Plan connection",
                retryable=False,
            )

        unsupported = sorted(
            name
            for name in _UNSUPPORTED_OPENAI_PARAMETERS.intersection(kwargs)
            if kwargs[name] is not None
        )
        if unsupported:
            raise ProviderError(
                f"StepFun does not document request parameter(s): {', '.join(unsupported)}",
                retryable=False,
            )

        request_kwargs = dict(kwargs)
        output_limits = [
            value
            for name in _OUTPUT_PARAMETER_NAMES
            if (value := _positive_int(request_kwargs.pop(name, None))) is not None
        ]
        if output_limits:
            request_kwargs["max_tokens"] = min(output_limits)

        payload = super()._build_payload(messages, model_id, **request_kwargs)
        _validate_number(payload.get("temperature"), "temperature", minimum=0, maximum=2)
        _validate_number(
            payload.get("top_p"),
            "top_p",
            minimum=0,
            maximum=1,
            minimum_inclusive=False,
        )
        _validate_number(
            payload.get("frequency_penalty"),
            "frequency_penalty",
            minimum=-2,
            maximum=2,
        )
        n = payload.get("n")
        if n is not None and (isinstance(n, bool) or not isinstance(n, int) or n != 1):
            raise ProviderError(
                "vBot requires StepFun n to be exactly 1",
                retryable=False,
            )
        reasoning_format = payload.get("reasoning_format")
        if reasoning_format not in (None, "general", "deepseek-style"):
            raise ProviderError(
                "StepFun reasoning_format must be 'general' or 'deepseek-style'",
                retryable=False,
            )

        if model_name == "step-router-v1" and isinstance(payload.get("max_tokens"), int):
            payload["max_tokens"] = min(
                payload["max_tokens"],
                STEPFUN_ROUTER_MAX_OUTPUT_TOKENS,
            )
        return payload

    def _prepare_stream_payload(self, payload: dict[str, Any]) -> None:
        """Enable SSE without the undocumented OpenAI ``stream_options`` extension."""

        payload["stream"] = True

    def _classify_http_status(
        self,
        status_code: int,
        *,
        detail: str,
        response_headers: httpx.Headers,
    ) -> None:
        if status_code == 402:
            raise ProviderError(
                f"StepFun balance or Step Plan entitlement is insufficient: {detail}",
                retryable=False,
            )
        if status_code == 451:
            raise ProviderError(
                f"StepFun content safety review rejected the request or response: {detail}",
                retryable=False,
            )
        super()._classify_http_status(
            status_code,
            detail=detail,
            response_headers=response_headers,
        )


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _validate_number(
    value: Any,
    name: str,
    *,
    minimum: float,
    maximum: float,
    minimum_inclusive: bool = True,
) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int | float):
        valid = False
    else:
        valid = value >= minimum if minimum_inclusive else value > minimum
        valid = valid and value <= maximum
    if not valid:
        left = "[" if minimum_inclusive else "("
        raise ProviderError(
            f"StepFun {name} must be within {left}{minimum}, {maximum}]",
            retryable=False,
        )
