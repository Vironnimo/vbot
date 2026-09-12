"""Openai policy."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.providers._openai_constants import (
    OPENAI_SUBSCRIPTION_REQUEST_PARAMETERS,
    OPTIONAL_REQUEST_PARAMETER_NAMES,
    REASONING_PARAMETER_NAMES,
    RESPONSES_POLICY_ENDPOINT,
    STRUCTURED_OUTPUT_PARAMETER_NAMES,
    TOOL_PARAMETER_NAMES,
)
from core.providers.reasoning import (
    closest_supported_effort,
    normalize_thinking_effort,
)


@dataclass(frozen=True)
class OpenAISubscriptionResponsesPolicy:
    """Responses request policy for OpenAI Subscription models."""

    allowed_reasoning_efforts: frozenset[str]
    supports_tools: bool
    supports_parallel_tool_calls: bool
    supports_structured_outputs: bool
    supports_streaming: bool = True
    endpoint_path: str = RESPONSES_POLICY_ENDPOINT
    supported_request_parameters: frozenset[str] = OPENAI_SUBSCRIPTION_REQUEST_PARAMETERS
    supports_explicit_none_effort: bool = False
    minimum_reasoning_effort: str | None = None

    @property
    def allows_any_reasoning_controls(self) -> bool:
        return bool(self.allowed_reasoning_efforts)

    def filter_request_kwargs(self, kwargs: Mapping[str, Any]) -> dict[str, Any]:
        filtered_kwargs = dict(kwargs)
        if not self.supports_tools:
            for parameter_name in TOOL_PARAMETER_NAMES:
                filtered_kwargs.pop(parameter_name, None)
        elif not self.supports_parallel_tool_calls:
            filtered_kwargs.pop("parallel_tool_calls", None)

        if not self.supports_structured_outputs:
            for parameter_name in STRUCTURED_OUTPUT_PARAMETER_NAMES:
                filtered_kwargs.pop(parameter_name, None)

        if not self.allows_any_reasoning_controls:
            for parameter_name in REASONING_PARAMETER_NAMES:
                filtered_kwargs.pop(parameter_name, None)
        else:
            self._normalize_reasoning_effort(filtered_kwargs, "thinking_effort")
            self._normalize_reasoning_effort(filtered_kwargs, "reasoning_effort")

        for parameter_name in OPTIONAL_REQUEST_PARAMETER_NAMES:
            if (
                parameter_name in filtered_kwargs
                and parameter_name not in self.supported_request_parameters
            ):
                filtered_kwargs.pop(parameter_name, None)
        return filtered_kwargs

    def closest_reasoning_effort(self, effort: Any) -> str | None:
        normalized_effort = normalize_thinking_effort(effort)
        if not normalized_effort:
            return None
        if normalized_effort == "none":
            if self.minimum_reasoning_effort in self.allowed_reasoning_efforts:
                return self.minimum_reasoning_effort
            return "none" if self.allows_any_reasoning_controls else None
        return closest_supported_effort(normalized_effort, self.allowed_reasoning_efforts)

    def supports_request_parameter(self, parameter_name: str) -> bool:
        return parameter_name in self.supported_request_parameters

    def _normalize_reasoning_effort(
        self,
        filtered_kwargs: dict[str, Any],
        parameter_name: str,
    ) -> None:
        if parameter_name not in filtered_kwargs:
            return
        safe_effort = self.closest_reasoning_effort(filtered_kwargs.get(parameter_name))
        if safe_effort is None:
            filtered_kwargs.pop(parameter_name, None)
            return
        filtered_kwargs[parameter_name] = safe_effort


def _normalize_catalog_raw(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    normalized = dict(raw)
    if not _optional_string(normalized.get("id")):
        slug = _optional_string(normalized.get("slug")) or _optional_string(normalized.get("model"))
        if slug:
            normalized["id"] = slug
    if not _optional_string(normalized.get("name")):
        display_name = _optional_string(normalized.get("display_name")) or _optional_string(
            normalized.get("title")
        )
        if display_name:
            normalized["name"] = display_name
    return normalized


def _optional_string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _optional_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _string_set(value: Any) -> frozenset[str]:
    if not isinstance(value, list):
        return frozenset()
    return frozenset(item for item in value if isinstance(item, str) and item)


def _subscription_capability_supported(
    raw_parameters: frozenset[str],
    default_value: bool,
    metadata_sources: tuple[Mapping[str, Any], ...],
    explicit_keys: tuple[str, ...],
    matching_parameters: frozenset[str],
) -> bool:
    explicit_value = _first_optional_bool(metadata_sources, explicit_keys)
    if explicit_value is not None:
        return explicit_value
    if raw_parameters:
        return bool(raw_parameters & matching_parameters)
    return default_value


def _subscription_supported_parameters(
    raw_parameters: frozenset[str],
    tools_supported: bool,
    json_supported: bool,
    reasoning_supported: bool,
) -> list[str]:
    supported_parameters: list[str] = []
    sparse_catalog = not raw_parameters
    if tools_supported:
        supported_parameters.append("tools")
    if json_supported:
        supported_parameters.append("response_format")
    if reasoning_supported:
        supported_parameters.append("reasoning")
    if tools_supported and (sparse_catalog or "parallel_tool_calls" in raw_parameters):
        supported_parameters.append("parallel_tool_calls")
    return supported_parameters


def _first_optional_bool(
    metadata_sources: tuple[Mapping[str, Any], ...],
    keys: tuple[str, ...],
) -> bool | None:
    for source in metadata_sources:
        for key in keys:
            value = source.get(key)
            if isinstance(value, bool):
                return value
            if key == "reasoning" and isinstance(value, Mapping):
                supported = value.get("supported")
                if isinstance(supported, bool):
                    return supported
    return None
