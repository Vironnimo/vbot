"""Chat Completions catalog value parsing and capability projection."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.providers._chat_completions_constants import (
    JSON_MODE_PARAMETER_NAMES,
    REASONING_PARAMETER_NAMES,
)


def _read_optional_mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key)
    if isinstance(value, dict):
        return value
    return {}


def _read_mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"Expected '{key}' to be an object")
    return value


def _read_non_empty_string(data: Mapping[str, Any], key: str) -> str:
    value = _read_string(data, key)
    if not value:
        raise ValueError(f"Expected '{key}' to be a non-empty string")
    return value


def _read_optional_non_empty_string(data: Mapping[str, Any], key: str) -> str | None:
    value = data.get(key)
    if isinstance(value, str) and value:
        return value
    return None


def _read_string(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ValueError(f"Expected '{key}' to be a string")
    return value


def _read_string_list(data: Mapping[str, Any], key: str) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"Expected '{key}' to be a list of strings")
    return value


def _read_optional_string_set(data: Mapping[str, Any], key: str) -> set[str]:
    value = data.get(key)
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, str)}


def _read_first_optional_string_tuple(
    metadata_sources: tuple[Mapping[str, Any], ...],
    keys: tuple[str, ...],
) -> tuple[str, ...]:
    for source in metadata_sources:
        for key in keys:
            value = source.get(key)
            if isinstance(value, list) and all(isinstance(item, str) for item in value):
                return tuple(value)
    return ()


def _read_first_optional_int(data: Mapping[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = data.get(key)
        parsed_value = _parse_optional_int(value)
        if parsed_value is not None:
            return parsed_value
    return None


def _parse_optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _has_image_modality(raw: Mapping[str, Any], architecture: Mapping[str, Any]) -> bool:
    return _modalities_include_image(raw) or _modalities_include_image(architecture)


def _modalities_include_image(data: Mapping[str, Any]) -> bool:
    for key in ("input_modalities", "inputModalities", "modalities"):
        value = data.get(key)
        if isinstance(value, list) and any(_modality_is_image(item) for item in value):
            return True
    return False


def _modality_is_image(value: Any) -> bool:
    if isinstance(value, str):
        return "image" in value.lower()
    if isinstance(value, dict):
        return any(_modality_is_image(item) for item in value.values())
    return False


def _supports_tools_by_default(*metadata_sources: Mapping[str, Any]) -> bool:
    explicit_value = _read_first_optional_bool(
        metadata_sources,
        ("supports_tools", "tools", "tool_calls", "function_calling"),
    )
    return explicit_value is not False


def _supports_json_mode(
    raw: Mapping[str, Any],
    top_provider: Mapping[str, Any],
    architecture: Mapping[str, Any],
    supported_parameters: set[str],
) -> bool:
    if supported_parameters & JSON_MODE_PARAMETER_NAMES:
        return True
    explicit_value = _read_first_optional_bool(
        (raw, top_provider, architecture),
        (
            "supports_json_mode",
            "json_mode",
            "supports_structured_outputs",
            "structured_outputs",
        ),
    )
    return explicit_value is True


def _supports_reasoning(
    raw: Mapping[str, Any],
    top_provider: Mapping[str, Any],
    architecture: Mapping[str, Any],
    supported_parameters: set[str],
) -> bool:
    if supported_parameters & REASONING_PARAMETER_NAMES:
        return True
    if _read_reasoning_supported(raw) or _read_reasoning_supported(architecture):
        return True
    explicit_value = _read_first_optional_bool(
        (raw, top_provider, architecture),
        ("supports_reasoning", "reasoning_supported"),
    )
    if explicit_value is True:
        return True
    return _has_non_empty_list(raw, "reasoning_efforts") or _has_non_empty_list(
        raw,
        "reasoningEfforts",
    )


def _read_reasoning_supported(data: Mapping[str, Any]) -> bool:
    reasoning = data.get("reasoning")
    return isinstance(reasoning, dict) and reasoning.get("supported") is True


def _read_first_optional_bool(
    metadata_sources: tuple[Mapping[str, Any], ...], keys: tuple[str, ...]
) -> bool | None:
    for source in metadata_sources:
        for key in keys:
            value = source.get(key)
            if isinstance(value, bool):
                return value
    return None


def _has_non_empty_list(data: Mapping[str, Any], key: str) -> bool:
    value = data.get(key)
    return isinstance(value, list) and len(value) > 0
