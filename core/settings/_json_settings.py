"""Shared recursive Settings JSON-object validation without persistence dependencies."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.utils.errors import StorageError


def normalize_json_object(value: Any, path: str) -> dict[str, Any]:
    """Return a deep-validated JSON object, rejecting non-JSON values."""

    if not isinstance(value, Mapping):
        raise StorageError(f"Expected {path} to be an object")
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise StorageError(f"Expected {path} keys to be non-empty strings")
        normalized[key] = _normalize_json_value(item, f"{path}.{key}")
    return normalized


def _normalize_json_value(value: Any, path: str) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list):
        return [_normalize_json_value(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, Mapping):
        return normalize_json_object(value, path)
    raise StorageError(f"Unsupported JSON value at {path}")
