"""GitHub Copilot provider adapter."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.github_copilot_policy import (
    COPILOT_METADATA_KEY,
    GitHubCopilotModelPolicy,
    copilot_model_policy,
)
from core.providers.openai_compatible import (
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_MAX_OUTPUT_TOKENS,
    OpenAICompatibleAdapter,
    _read_mapping,
    _read_non_empty_string,
    _read_optional_mapping,
    _read_string,
)

OPENAI_REASONING_COPILOT_MODEL_POLICY = copilot_model_policy("gpt-5-mini")


class GitHubCopilotAdapter(OpenAICompatibleAdapter):
    """OpenAI-compatible adapter for GitHub Copilot."""

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        policy = _copilot_model_policy(model_id)
        return super()._build_payload(
            messages,
            model_id,
            **policy.filter_request_kwargs(kwargs),
        )

    @classmethod
    def normalize_catalog_entry(
        cls,
        raw: Mapping[str, Any],
        defaults: Mapping[str, Any] | None = None,
    ) -> Model:
        """Normalize one captured GitHub Copilot ``/models`` entry."""

        capabilities = _read_mapping(raw, "capabilities")
        limits = _read_optional_mapping(capabilities, "limits")
        supports = _read_optional_mapping(capabilities, "supports")

        return Model(
            model_id=_read_non_empty_string(raw, "id"),
            name=_read_string(raw, "name"),
            capabilities=Capabilities(
                vision=supports.get("vision") is True,
                tools=supports.get("tool_calls") is True,
                json_mode=supports.get("structured_outputs") is True,
                reasoning=ReasoningCapabilities(supported=_copilot_supports_reasoning(supports)),
            ),
            context_window=_read_optional_token_limit(
                limits,
                "max_context_window_tokens",
                DEFAULT_CONTEXT_WINDOW,
            ),
            max_output_tokens=_read_optional_token_limit(
                limits,
                "max_output_tokens",
                _provider_default_max_tokens(defaults),
            ),
            metadata=_copilot_runtime_metadata(raw, capabilities, supports),
        )


def _copilot_supports_reasoning(supports: Mapping[str, Any]) -> bool:
    reasoning_effort = supports.get("reasoning_effort")
    if isinstance(reasoning_effort, list) and reasoning_effort:
        return True
    return "min_thinking_budget" in supports or "max_thinking_budget" in supports


def _copilot_model_policy(model_id: str) -> GitHubCopilotModelPolicy:
    return copilot_model_policy(model_id)


def _copilot_runtime_metadata(
    raw: Mapping[str, Any],
    capabilities: Mapping[str, Any],
    supports: Mapping[str, Any],
) -> Mapping[str, Any]:
    metadata: dict[str, Any] = {}
    for source_key in ("vendor", "version"):
        value = raw.get(source_key)
        if isinstance(value, str) and value:
            metadata[source_key] = value
    family = capabilities.get("family")
    if isinstance(family, str) and family:
        metadata["family"] = family

    supported_endpoints = raw.get("supported_endpoints")
    if isinstance(supported_endpoints, list):
        endpoints = [endpoint for endpoint in supported_endpoints if isinstance(endpoint, str)]
        if endpoints:
            metadata["supported_endpoints"] = endpoints

    reasoning_effort = supports.get("reasoning_effort")
    if isinstance(reasoning_effort, list):
        efforts = [effort for effort in reasoning_effort if isinstance(effort, str)]
        if efforts:
            metadata["reasoning_efforts"] = efforts

    for support_key in (
        "min_thinking_budget",
        "max_thinking_budget",
        "adaptive_thinking",
        "parallel_tool_calls",
        "streaming",
        "structured_outputs",
        "tool_calls",
    ):
        value = supports.get(support_key)
        if isinstance(value, bool | int) and not (
            isinstance(value, bool) and support_key.endswith("budget")
        ):
            metadata[support_key] = value

    return {COPILOT_METADATA_KEY: metadata} if metadata else {}


def _read_optional_token_limit(
    data: Mapping[str, Any],
    key: str,
    fallback: int,
) -> int:
    value = data.get(key)
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return fallback


def _provider_default_max_tokens(defaults: Mapping[str, Any] | None) -> int:
    if defaults is None:
        return DEFAULT_MAX_OUTPUT_TOKENS
    max_tokens = defaults.get("max_tokens")
    if isinstance(max_tokens, bool):
        return DEFAULT_MAX_OUTPUT_TOKENS
    if isinstance(max_tokens, int):
        return max_tokens
    if isinstance(max_tokens, str) and max_tokens.isdecimal():
        return int(max_tokens)
    return DEFAULT_MAX_OUTPUT_TOKENS
