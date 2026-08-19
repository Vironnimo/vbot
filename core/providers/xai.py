"""xAI Responses adapter for API-key and SuperGrok OAuth Connections."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.providers.github_copilot_responses import (
    REASONING_ENCRYPTED_CONTENT_INCLUDE,
    build_responses_payload,
    estimate_responses_input_tokens,
)
from core.providers.openai import OpenAIAdapter, OpenAISubscriptionResponsesPolicy
from core.providers.providers import ProviderConfig
from core.providers.reasoning import (
    closest_supported_effort,
    model_reasoning_levels,
    model_reasoning_supported,
    normalize_thinking_effort,
)

XAI_RESPONSES_REQUEST_PARAMETERS = frozenset(
    {
        "max_tokens",
        "max_output_tokens",
        "prompt_cache_key",
        "service_tier",
        "temperature",
        "top_p",
    }
)
XAI_IMAGE_MEDIA_TYPES = frozenset({"image/jpeg", "image/png"})


@dataclass(frozen=True)
class XAIResponsesPolicy(OpenAISubscriptionResponsesPolicy):
    """Responses policy that respects xAI's models which cannot disable reasoning."""

    def closest_reasoning_effort(self, effort: Any) -> str | None:
        normalized = normalize_thinking_effort(effort)
        if not normalized:
            return None
        if normalized == "none" and "none" not in self.allowed_reasoning_efforts:
            return closest_supported_effort("low", self.allowed_reasoning_efforts)
        return closest_supported_effort(normalized, self.allowed_reasoning_efforts)


class XAIAdapter(OpenAIAdapter):
    """Translate vBot requests to xAI's stateless ``/responses`` protocol."""

    @classmethod
    def discovery_headers(
        cls,
        _provider_config: ProviderConfig,
        _credential_value: str,
        headers: Mapping[str, str],
    ) -> dict[str, str]:
        """Keep the selected xAI Connection header without OpenAI account routing."""

        del cls
        return dict(headers)

    @classmethod
    def discovery_params(cls) -> dict[str, str]:
        """xAI's Model listing needs no query parameters."""

        return {}

    @classmethod
    async def resolve_discovery_params(cls, fetch_json: Any) -> dict[str, str]:
        """xAI's public Model listing has no Codex client-version query."""

        del cls, fetch_json
        return {}

    def request_context_kwargs(
        self,
        *,
        agent_id: str,
        session_id: str,
        project_id: str | None = None,
        prompt_cache_affinity_id: str | None = None,
    ) -> dict[str, Any]:
        """Route cache-compatible Session prefixes to the same xAI cache shard."""

        del project_id
        conversation_id = f"{agent_id}:{session_id}"
        return {"prompt_cache_key": prompt_cache_affinity_id or conversation_id}

    def wire_media_support(self, model_id: str) -> frozenset[str]:
        """xAI's current language-model wire accepts JPEG and PNG images."""

        del model_id
        return XAI_IMAGE_MEDIA_TYPES

    def _uses_platform_responses(self, model_id: str) -> bool:
        del model_id
        return True

    def _allowed_reasoning_efforts(
        self,
        model_id: str,
        reasoning_supported: bool,
    ) -> frozenset[str]:
        if not reasoning_supported:
            return frozenset()
        levels = model_reasoning_levels(self._model_lookup, model_id)
        return frozenset(levels or ())

    def _responses_policy_for_model(self, model_id: str) -> XAIResponsesPolicy:
        base_policy = super()._responses_policy_for_model(model_id)
        return XAIResponsesPolicy(
            allowed_reasoning_efforts=base_policy.allowed_reasoning_efforts,
            supports_tools=base_policy.supports_tools,
            supports_parallel_tool_calls=base_policy.supports_parallel_tool_calls,
            supports_structured_outputs=base_policy.supports_structured_outputs,
            supports_streaming=base_policy.supports_streaming,
            supported_request_parameters=XAI_RESPONSES_REQUEST_PARAMETERS,
        )

    def _build_responses_payload(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        stream: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        request_kwargs = dict(kwargs)
        self._apply_model_output_limit(
            request_kwargs,
            model_id,
            messages,
            estimated_input_tokens=estimate_responses_input_tokens(
                messages,
                tools=request_kwargs.get("tools"),
            ),
        )
        payload = build_responses_payload(
            messages,
            model_id=model_id,
            policy=self._responses_policy_for_model(model_id),
            stream=stream,
            **request_kwargs,
        )
        if model_reasoning_supported(self._model_lookup, model_id) is True:
            include = payload.setdefault("include", [])
            if REASONING_ENCRYPTED_CONTENT_INCLUDE not in include:
                include.append(REASONING_ENCRYPTED_CONTENT_INCLUDE)
        payload["store"] = False
        return payload
