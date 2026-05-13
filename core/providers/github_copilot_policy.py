"""Dynamic-first GitHub Copilot runtime policy.

This module owns model-specific Copilot request facts. It does not build
endpoint payloads; adapters use it to choose a Copilot endpoint and decide which
optional request features are safe to send.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

CHAT_COMPLETIONS_ENDPOINT = "/chat/completions"
RESPONSES_ENDPOINT = "/responses"
MESSAGES_ENDPOINT = "/v1/messages"

COPILOT_METADATA_KEY = "github_copilot"

_OPENAI_LIKE_VENDOR_MARKERS = ("openai", "azure openai")
_GPT_FAMILY_PREFIXES = ("gpt-", "gpt_", "o1", "o3", "o4")
_REASONING_PARAMETER_NAMES = frozenset(
    {"thinking_effort", "reasoning_effort", "reasoning", "include_reasoning"}
)
_STRUCTURED_OUTPUT_PARAMETER_NAMES = frozenset(
    {"response_format", "structured_outputs", "json_mode"}
)
_TOOL_PARAMETER_NAMES = frozenset({"tools", "tool_choice", "parallel_tool_calls"})
_OPTIONAL_REQUEST_PARAMETER_NAMES = frozenset(
    {
        "max_tokens",
        "max_output_tokens",
        "temperature",
        "top_p",
        "top_k",
        "stop_sequences",
    }
)
_CHAT_COMPLETIONS_REQUEST_PARAMETERS = frozenset({"max_tokens", "temperature", "top_p"})
_RESPONSES_REQUEST_PARAMETERS = frozenset(
    {"max_tokens", "max_output_tokens", "temperature", "top_p"}
)
_MESSAGES_REQUEST_PARAMETERS = frozenset(
    {
        "max_tokens",
        "max_output_tokens",
        "max_completion_tokens",
        "temperature",
        "top_p",
        "top_k",
        "stop_sequences",
    }
)


@dataclass(frozen=True)
class CopilotModelFacts:
    """Sanitized runtime facts preserved from Copilot model metadata."""

    model_id: str
    vendor: str = ""
    family: str = ""
    version: str = ""
    supported_endpoints: frozenset[str] = frozenset()
    allowed_reasoning_efforts: frozenset[str] = frozenset()
    min_thinking_budget: int | None = None
    max_thinking_budget: int | None = None
    adaptive_thinking: bool = False
    supports_tools: bool = False
    supports_parallel_tool_calls: bool = False
    supports_streaming: bool = False
    supports_structured_outputs: bool = False

    @classmethod
    def from_metadata(
        cls,
        model_id: str,
        metadata: Mapping[str, Any] | None,
    ) -> CopilotModelFacts:
        """Build facts from the sanitized metadata stored on ``Model``."""

        copilot_metadata = _copilot_metadata(metadata)
        return cls(
            model_id=model_id,
            vendor=_read_string(copilot_metadata, "vendor"),
            family=_read_string(copilot_metadata, "family"),
            version=_read_string(copilot_metadata, "version"),
            supported_endpoints=frozenset(
                endpoint
                for endpoint in _read_string_list(copilot_metadata, "supported_endpoints")
                if endpoint != "ws:/responses"
            ),
            allowed_reasoning_efforts=frozenset(
                _read_string_list(copilot_metadata, "reasoning_efforts")
            ),
            min_thinking_budget=_read_optional_int(copilot_metadata, "min_thinking_budget"),
            max_thinking_budget=_read_optional_int(copilot_metadata, "max_thinking_budget"),
            adaptive_thinking=_read_bool(copilot_metadata, "adaptive_thinking"),
            supports_tools=_read_bool(copilot_metadata, "tool_calls"),
            supports_parallel_tool_calls=_read_bool(copilot_metadata, "parallel_tool_calls"),
            supports_streaming=_read_bool(copilot_metadata, "streaming"),
            supports_structured_outputs=_read_bool(copilot_metadata, "structured_outputs"),
        )

    @property
    def has_metadata(self) -> bool:
        return any(
            (
                self.vendor,
                self.family,
                self.version,
                self.supported_endpoints,
                self.allowed_reasoning_efforts,
                self.min_thinking_budget is not None,
                self.max_thinking_budget is not None,
                self.adaptive_thinking,
                self.supports_tools,
                self.supports_parallel_tool_calls,
                self.supports_streaming,
                self.supports_structured_outputs,
            )
        )

    @property
    def family_or_model(self) -> str:
        return (self.family or self.version or self.model_id).lower()

    @property
    def is_claude_like(self) -> bool:
        return "anthropic" in self.vendor.lower() or "claude" in self.family_or_model

    @property
    def is_gemini_like(self) -> bool:
        return "google" in self.vendor.lower() or "gemini" in self.family_or_model

    @property
    def is_openai_like(self) -> bool:
        vendor = self.vendor.lower()
        if any(marker in vendor for marker in _OPENAI_LIKE_VENDOR_MARKERS):
            return True
        family = self.family_or_model
        return family.startswith(_GPT_FAMILY_PREFIXES)


@dataclass(frozen=True)
class GitHubCopilotModelPolicy:
    """Per-model Copilot runtime policy for routing and request shaping."""

    facts: CopilotModelFacts
    endpoint_path: str = CHAT_COMPLETIONS_ENDPOINT
    supported_request_parameters: frozenset[str] = frozenset()
    allowed_reasoning_efforts: frozenset[str] = frozenset()
    supports_thinking_budget: bool = False
    supports_adaptive_thinking: bool = False
    supports_tools: bool = False
    supports_parallel_tool_calls: bool = False
    supports_streaming: bool = False
    supports_structured_outputs: bool = False
    omit_temperature_when_thinking_active: bool = False

    @classmethod
    def from_metadata(
        cls,
        model_id: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> GitHubCopilotModelPolicy:
        facts = CopilotModelFacts.from_metadata(model_id, metadata)
        if not facts.has_metadata:
            facts = _fallback_facts_for_model(model_id)
        policy = cls(
            facts=facts,
            endpoint_path=_select_endpoint(facts),
            supported_request_parameters=frozenset(),
            allowed_reasoning_efforts=facts.allowed_reasoning_efforts,
            supports_thinking_budget=(
                facts.min_thinking_budget is not None and facts.max_thinking_budget is not None
            ),
            supports_adaptive_thinking=facts.adaptive_thinking,
            supports_tools=facts.supports_tools,
            supports_parallel_tool_calls=facts.supports_parallel_tool_calls,
            supports_streaming=facts.supports_streaming,
            supports_structured_outputs=facts.supports_structured_outputs,
        )
        policy = replace(
            policy,
            supported_request_parameters=_supported_request_parameters(
                facts,
                policy.endpoint_path,
            ),
        )
        return _apply_exact_override(policy)

    def filter_request_kwargs(self, kwargs: Mapping[str, Any]) -> dict[str, Any]:
        """Return request kwargs with unsupported optional features removed."""

        filtered_kwargs = dict(kwargs)
        if not self.supports_tools:
            for parameter_name in _TOOL_PARAMETER_NAMES:
                filtered_kwargs.pop(parameter_name, None)
        elif not self.supports_parallel_tool_calls:
            filtered_kwargs.pop("parallel_tool_calls", None)

        if not self.supports_structured_outputs:
            for parameter_name in _STRUCTURED_OUTPUT_PARAMETER_NAMES:
                filtered_kwargs.pop(parameter_name, None)

        if self.endpoint_path == MESSAGES_ENDPOINT:
            self._filter_messages_reasoning_kwargs(filtered_kwargs)
        elif not self.allows_any_reasoning_controls:
            for parameter_name in _REASONING_PARAMETER_NAMES:
                filtered_kwargs.pop(parameter_name, None)
        else:
            thinking_effort = filtered_kwargs.get("thinking_effort")
            if isinstance(thinking_effort, str) and not self.allows_reasoning_effort(
                thinking_effort
            ):
                filtered_kwargs.pop("thinking_effort", None)
            reasoning_effort = filtered_kwargs.get("reasoning_effort")
            if isinstance(reasoning_effort, str) and not self.allows_reasoning_effort(
                reasoning_effort
            ):
                filtered_kwargs.pop("reasoning_effort", None)

        if not self.supports_thinking_budget:
            filtered_kwargs.pop("thinking_budget", None)
            thinking = filtered_kwargs.get("thinking")
            if isinstance(thinking, Mapping) and "budget_tokens" in thinking:
                filtered_kwargs.pop("thinking", None)
        if not self.supports_adaptive_thinking:
            thinking = filtered_kwargs.get("thinking")
            if isinstance(thinking, Mapping) and thinking.get("type") == "adaptive":
                filtered_kwargs.pop("thinking", None)

        if self._should_omit_temperature(filtered_kwargs):
            filtered_kwargs.pop("temperature", None)

        for parameter_name in _OPTIONAL_REQUEST_PARAMETER_NAMES:
            if (
                parameter_name in filtered_kwargs
                and parameter_name not in self.supported_request_parameters
            ):
                filtered_kwargs.pop(parameter_name, None)
        return filtered_kwargs

    @property
    def allows_any_reasoning_controls(self) -> bool:
        return bool(
            self.allowed_reasoning_efforts
            or self.supports_thinking_budget
            or self.supports_adaptive_thinking
        )

    def allows_reasoning_effort(self, effort: str) -> bool:
        if not effort or effort == "none":
            return True
        return effort in self.allowed_reasoning_efforts

    def allows_openai_reasoning_effort(self, thinking_effort: str) -> bool:
        """Compatibility alias used by existing Copilot tests and adapter code."""

        return self.allows_reasoning_effort(thinking_effort)

    def supports_request_parameter(self, parameter_name: str) -> bool:
        return parameter_name in self.supported_request_parameters

    def _should_omit_temperature(self, kwargs: Mapping[str, Any]) -> bool:
        if not self.omit_temperature_when_thinking_active:
            return False
        if self.endpoint_path != MESSAGES_ENDPOINT:
            return False
        if "temperature" not in kwargs:
            return False
        return self._has_active_thinking(kwargs)

    def _has_active_thinking(self, kwargs: Mapping[str, Any]) -> bool:
        thinking = kwargs.get("thinking")
        if isinstance(thinking, Mapping):
            thinking_type = thinking.get("type")
            if thinking_type in {"adaptive", "enabled"}:
                return True

        thinking_budget = kwargs.get("thinking_budget")
        if isinstance(thinking_budget, int) and not isinstance(thinking_budget, bool):
            return True

        thinking_effort = kwargs.get("thinking_effort")
        if self._is_active_reasoning_effort(thinking_effort):
            return True

        reasoning_effort = kwargs.get("reasoning_effort")
        if self._is_active_reasoning_effort(reasoning_effort):
            return True

        output_config = kwargs.get("output_config")
        if isinstance(output_config, Mapping):
            effort = output_config.get("effort")
            if self._is_active_reasoning_effort(effort):
                return True

        return False

    def _is_active_reasoning_effort(self, effort: Any) -> bool:
        if not isinstance(effort, str):
            return False
        if effort in {"", "none", "minimal"}:
            return False
        return self.allows_reasoning_effort(effort)

    def _filter_messages_reasoning_kwargs(self, filtered_kwargs: dict[str, Any]) -> None:
        filtered_kwargs.pop("reasoning", None)
        filtered_kwargs.pop("include_reasoning", None)

        reasoning_effort = filtered_kwargs.get("reasoning_effort")
        if isinstance(reasoning_effort, str) and not self.allows_reasoning_effort(reasoning_effort):
            filtered_kwargs.pop("reasoning_effort", None)

        output_config = filtered_kwargs.get("output_config")
        if isinstance(output_config, Mapping):
            effort = output_config.get("effort")
            if not isinstance(effort, str) or not self.allows_reasoning_effort(effort):
                filtered_kwargs.pop("output_config", None)
        else:
            filtered_kwargs.pop("output_config", None)

        thinking = filtered_kwargs.get("thinking")
        if isinstance(thinking, Mapping):
            thinking_type = thinking.get("type")
            if (
                (thinking_type == "adaptive" and not self.supports_adaptive_thinking)
                or (thinking_type == "enabled" and not self.supports_thinking_budget)
                or thinking_type not in {"adaptive", "enabled", "disabled"}
            ):
                filtered_kwargs.pop("thinking", None)
        else:
            filtered_kwargs.pop("thinking", None)

        thinking_effort = filtered_kwargs.get("thinking_effort")
        if isinstance(thinking_effort, str):
            if not self._messages_accepts_thinking_effort_trigger(thinking_effort):
                filtered_kwargs.pop("thinking_effort", None)
        else:
            filtered_kwargs.pop("thinking_effort", None)

    def _messages_accepts_thinking_effort_trigger(self, thinking_effort: str) -> bool:
        if not thinking_effort or thinking_effort == "minimal":
            return False
        if thinking_effort == "none":
            return self.supports_adaptive_thinking or self.supports_thinking_budget
        return self.allows_reasoning_effort(thinking_effort)


def copilot_model_policy(
    model_id: str,
    metadata: Mapping[str, Any] | None = None,
) -> GitHubCopilotModelPolicy:
    """Return the dynamic-first policy for one exact Copilot model ID."""

    return GitHubCopilotModelPolicy.from_metadata(model_id, metadata)


def _select_endpoint(facts: CopilotModelFacts) -> str:
    endpoints = facts.supported_endpoints
    if facts.is_claude_like and MESSAGES_ENDPOINT in endpoints:
        return MESSAGES_ENDPOINT
    if facts.is_gemini_like:
        return (
            CHAT_COMPLETIONS_ENDPOINT
            if CHAT_COMPLETIONS_ENDPOINT in endpoints
            else _first_safe_endpoint(endpoints)
        )
    if facts.is_openai_like and RESPONSES_ENDPOINT in endpoints:
        return RESPONSES_ENDPOINT
    return _first_safe_endpoint(endpoints)


def _first_safe_endpoint(endpoints: frozenset[str]) -> str:
    if CHAT_COMPLETIONS_ENDPOINT in endpoints:
        return CHAT_COMPLETIONS_ENDPOINT
    if MESSAGES_ENDPOINT in endpoints:
        return MESSAGES_ENDPOINT
    if RESPONSES_ENDPOINT in endpoints:
        return RESPONSES_ENDPOINT
    return CHAT_COMPLETIONS_ENDPOINT


def _fallback_facts_for_model(model_id: str) -> CopilotModelFacts:
    return _STATIC_FALLBACK_FACTS_BY_ID.get(
        model_id,
        CopilotModelFacts(
            model_id=model_id, supported_endpoints=frozenset({CHAT_COMPLETIONS_ENDPOINT})
        ),
    )


def _apply_exact_override(policy: GitHubCopilotModelPolicy) -> GitHubCopilotModelPolicy:
    override = _STATIC_EXACT_OVERRIDES_BY_ID.get(policy.facts.model_id)
    if override is None:
        return policy
    return replace(policy, **override)


def _supported_request_parameters(
    facts: CopilotModelFacts,
    endpoint_path: str,
) -> frozenset[str]:
    if endpoint_path == MESSAGES_ENDPOINT:
        return _MESSAGES_REQUEST_PARAMETERS
    if endpoint_path == RESPONSES_ENDPOINT:
        parameters = set(_RESPONSES_REQUEST_PARAMETERS)
        parameters.discard("temperature")
        return frozenset(parameters)
    return _CHAT_COMPLETIONS_REQUEST_PARAMETERS


def _copilot_metadata(metadata: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if metadata is None:
        return {}
    value = metadata.get(COPILOT_METADATA_KEY)
    if isinstance(value, Mapping):
        return value
    return metadata


def _read_string(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else ""


def _read_string_list(data: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = data.get(key)
    if not isinstance(value, list | tuple):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _read_optional_int(data: Mapping[str, Any], key: str) -> int | None:
    value = data.get(key)
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _read_bool(data: Mapping[str, Any], key: str) -> bool:
    return data.get(key) is True


_STATIC_FALLBACK_FACTS_BY_ID = {
    "gpt-5-mini": CopilotModelFacts(
        model_id="gpt-5-mini",
        vendor="OpenAI",
        family="gpt-5-mini",
        supported_endpoints=frozenset({CHAT_COMPLETIONS_ENDPOINT}),
        allowed_reasoning_efforts=frozenset({"low", "medium", "high"}),
        supports_tools=True,
        supports_parallel_tool_calls=True,
        supports_streaming=True,
        supports_structured_outputs=True,
    ),
}

_STATIC_EXACT_OVERRIDES_BY_ID: dict[str, dict[str, Any]] = {
    "claude-haiku-4.5": {
        "allowed_reasoning_efforts": frozenset(),
        "supports_thinking_budget": False,
    },
    "claude-sonnet-4.6": {"omit_temperature_when_thinking_active": True},
}
