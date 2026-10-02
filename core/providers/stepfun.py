"""StepFun direct API and Step Plan policy on the shared Chat Completions wire.

Accepted request parameters and ranges, reasoning ladders, output limits, media and
the Step Plan-only routing Model live in ``resources/wire/stepfun.json``; this
Adapter owns the Model allowlist, streaming options and StepFun status errors."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, override

import httpx

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.errors import CatalogEntrySkipped, ProviderError
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.providers import ConnectionConfig

STEPFUN_DIRECT_MODE = "direct_api"
STEPFUN_PLAN_MODE = "step_plan"
STEPFUN_CONTEXT_WINDOW = 256_000
STEPFUN_ROUTER_MAX_OUTPUT_TOKENS = 250_000

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
    @override
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
        )

    @override
    def _prepare_stream_payload(self, payload: dict[str, Any]) -> None:
        """Enable SSE without the undocumented OpenAI ``stream_options`` extension."""

        payload["stream"] = True

    @override
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
