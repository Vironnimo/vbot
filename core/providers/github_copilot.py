"""GitHub Copilot provider adapter."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.openai_compatible import (
    CHAT_COMPLETIONS_ENDPOINT,
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_MAX_OUTPUT_TOKENS,
    OPENAI_REASONING_EFFORTS,
    OpenAICompatibleAdapter,
    _read_mapping,
    _read_non_empty_string,
    _read_optional_mapping,
    _read_string,
)


@dataclass(frozen=True)
class GitHubCopilotModelPolicy:
    """Per-model Copilot runtime policy for request shaping."""

    endpoint_path: str = CHAT_COMPLETIONS_ENDPOINT
    allowed_parameters: frozenset[str] | None = None
    denied_parameters: frozenset[str] = frozenset()
    allowed_reasoning_efforts: frozenset[str] = frozenset()

    def filter_request_kwargs(self, kwargs: Mapping[str, Any]) -> dict[str, Any]:
        filtered_kwargs = dict(kwargs)
        if self.allowed_parameters is not None:
            filtered_kwargs = {
                key: value
                for key, value in filtered_kwargs.items()
                if key in self.allowed_parameters
            }
        for parameter_name in self.denied_parameters:
            filtered_kwargs.pop(parameter_name, None)

        thinking_effort = filtered_kwargs.get("thinking_effort")
        if isinstance(thinking_effort, str) and not self.allows_openai_reasoning_effort(
            thinking_effort
        ):
            filtered_kwargs.pop("thinking_effort", None)
        return filtered_kwargs

    def allows_openai_reasoning_effort(self, thinking_effort: str) -> bool:
        if not thinking_effort or thinking_effort == "none":
            return True
        return thinking_effort in self.allowed_reasoning_efforts


DEFAULT_COPILOT_MODEL_POLICY = GitHubCopilotModelPolicy()
OPENAI_REASONING_COPILOT_MODEL_POLICY = GitHubCopilotModelPolicy(
    allowed_reasoning_efforts=frozenset(OPENAI_REASONING_EFFORTS)
)
COPILOT_MODEL_POLICIES_BY_ID = {
    "gpt-5-mini": OPENAI_REASONING_COPILOT_MODEL_POLICY,
}
COPILOT_MODEL_POLICY_PREFIXES: tuple[tuple[str, GitHubCopilotModelPolicy], ...] = ()


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
        )


def _copilot_supports_reasoning(supports: Mapping[str, Any]) -> bool:
    reasoning_effort = supports.get("reasoning_effort")
    if isinstance(reasoning_effort, list) and reasoning_effort:
        return True
    return "min_thinking_budget" in supports or "max_thinking_budget" in supports


def _copilot_model_policy(model_id: str) -> GitHubCopilotModelPolicy:
    exact_match_policy = COPILOT_MODEL_POLICIES_BY_ID.get(model_id)
    if exact_match_policy is not None:
        return exact_match_policy

    for model_prefix, policy in COPILOT_MODEL_POLICY_PREFIXES:
        if model_id.startswith(model_prefix):
            return policy

    return DEFAULT_COPILOT_MODEL_POLICY


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
