"""Responses values."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Protocol

RESPONSES_DONE_MARKER = "[DONE]"

REASONING_ENCRYPTED_CONTENT_INCLUDE = "reasoning.encrypted_content"

REASONING_SUMMARY_DELTA_EVENTS = {
    "response.reasoning.delta",
    "response.reasoning_summary_text.delta",
    "response.reasoning_text.delta",
    "response.output_item.reasoning_summary_text.delta",
}

RESPONSES_ERROR_EVENTS = {"error", "response.error", "response.failed"}

RESPONSES_INCOMPLETE_EVENTS = {"response.incomplete"}

_REASONING_META_KEYS = ("reasoning_items", "response_output")

RESPONSES_RESPONSE_OUTPUT_META_KEY = "response_output"


class ResponsesRequestPolicy(Protocol):
    """Provider policy surface needed by the shared Responses payload builder."""

    @property
    def supports_tools(self) -> bool: ...

    @property
    def supports_parallel_tool_calls(self) -> bool: ...

    @property
    def supports_structured_outputs(self) -> bool: ...

    @property
    def allows_any_reasoning_controls(self) -> bool: ...

    @property
    def supports_explicit_none_effort(self) -> bool: ...

    def filter_request_kwargs(self, kwargs: Mapping[str, Any]) -> dict[str, Any]: ...

    def closest_reasoning_effort(self, effort: Any) -> str | None: ...

    def supports_request_parameter(self, parameter_name: str) -> bool: ...


def _response_output_from_meta(reasoning_meta: Any) -> list[Mapping[str, Any]]:
    if not isinstance(reasoning_meta, Mapping):
        return []
    items = reasoning_meta.get(RESPONSES_RESPONSE_OUTPUT_META_KEY)
    if not isinstance(items, list) or not all(isinstance(item, Mapping) for item in items):
        return []
    return items


def _is_reasoning_item(item: Any) -> bool:
    return isinstance(item, Mapping) and item.get("type") == "reasoning"


def _function_call_id(item: Mapping[str, Any]) -> str:
    call_id = _non_empty_string_or_none(item.get("call_id"))
    if call_id is not None:
        return call_id
    item_id = _non_empty_string_or_none(item.get("id"))
    return item_id if item_id is not None else "tool_call_0"


def _function_mapping(item: Mapping[str, Any]) -> Mapping[str, Any] | None:
    function = item.get("function")
    return function if isinstance(function, Mapping) else None


def _function_call_name(item: Mapping[str, Any]) -> str:
    function = _function_mapping(item)
    if function is not None:
        nested_name = _non_empty_string_or_none(function.get("name"))
        if nested_name is not None:
            return nested_name
    name = _non_empty_string_or_none(item.get("name"))
    return name if name is not None else ""


def _function_description(item: Mapping[str, Any]) -> str:
    description = item.get("description")
    if isinstance(description, str):
        return description
    function = _function_mapping(item)
    if function is None:
        return ""
    return _string_or(function.get("description"), "")


def _function_parameters(item: Mapping[str, Any]) -> Any:
    parameters = item.get("parameters")
    if isinstance(parameters, Mapping):
        return parameters
    function = _function_mapping(item)
    if function is None:
        return {}
    nested_parameters = function.get("parameters")
    return nested_parameters if isinstance(nested_parameters, Mapping) else {}


def _function_call_arguments(item: Mapping[str, Any]) -> Any:
    arguments = item.get("arguments")
    if _has_function_arguments(arguments):
        return arguments
    function = _function_mapping(item)
    if function is None:
        return None
    return function.get("arguments")


def _has_function_arguments(arguments: Any) -> bool:
    if isinstance(arguments, Mapping):
        return True
    return isinstance(arguments, str) and bool(arguments)


def _serialize_tool_arguments(arguments: Any) -> str:
    if isinstance(arguments, str):
        return arguments
    return json.dumps(arguments if arguments is not None else {}, separators=(",", ":"))


def _mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _response_output_items(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        return [value]
    return _mapping_list(value)


def _joined_or_none(parts: list[str]) -> str | None:
    return "".join(parts) if parts else None


def _string_or(value: Any, fallback: str) -> str:
    return value if isinstance(value, str) else fallback


def _non_empty_string_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None
