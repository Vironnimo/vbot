"""OpenAI catalog normalization helpers (Codex ``/codex/models`` entries)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


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
