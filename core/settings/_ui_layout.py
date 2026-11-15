"""Validation for the shared, forward-compatible UI layout preference."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from core.settings._json_settings import normalize_json_value
from core.utils.errors import StorageError

MAX_UI_LAYOUT_BYTES = 1_048_576
MAX_UI_LAYOUT_SURFACES = 256
MAX_UI_LAYOUT_ITEMS = 2048
MAX_UI_LAYOUT_ID_LENGTH = 512
MAX_UI_LAYOUT_DEPTH = 32


class UiLayoutError(StorageError):
    """A malformed layout value with its precise field path."""

    def __init__(self, path: str, detail: str) -> None:
        self.path = path
        self.detail = detail
        super().__init__(f"{path}: {detail}")


def normalize_ui_layout(value: Any, path: str = "appearance.ui_layout") -> dict[str, Any] | None:
    """Copy a complete layout, retaining unknown surfaces and future fields.

    Null means no shared preference has been selected; an empty object is an
    intentional default layout. Whole-object writes can explicitly remove data.
    """
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise UiLayoutError(path, "must be an object or null")
    if len(value) > MAX_UI_LAYOUT_SURFACES:
        raise UiLayoutError(path, f"must contain at most {MAX_UI_LAYOUT_SURFACES} surfaces")
    pending: list[tuple[Any, str, int]] = [(value, path, 0)]
    while pending:
        item, item_path, depth = pending.pop()
        if depth > MAX_UI_LAYOUT_DEPTH:
            raise UiLayoutError(item_path, f"must not exceed depth {MAX_UI_LAYOUT_DEPTH}")
        if isinstance(item, Mapping):
            pending.extend((child, f"{item_path}.{key}", depth + 1) for key, child in item.items())
        elif isinstance(item, list):
            pending.extend(
                (child, f"{item_path}[{index}]", depth + 1) for index, child in enumerate(item)
            )
    try:
        normalized: dict[str, Any] = normalize_json_value(value, path)
        byte_count = len(
            json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
    except (StorageError, ValueError, UnicodeError) as error:
        raise UiLayoutError(path, str(error)) from error
    if byte_count > MAX_UI_LAYOUT_BYTES:
        raise UiLayoutError(path, f"must use at most {MAX_UI_LAYOUT_BYTES} UTF-8 JSON bytes")
    for surface_id, surface in normalized.items():
        surface_path = f"{path}[{surface_id!r}]"
        _validate_id(surface_id, surface_path)
        if not isinstance(surface, dict):
            raise UiLayoutError(surface_path, "must be an object")
        for field in ("hidden", "order"):
            if field not in surface:
                continue
            field_path = f"{surface_path}.{field}"
            entries = surface[field]
            if not isinstance(entries, list):
                raise UiLayoutError(field_path, "must be an array of contribution IDs")
            _validate_count(entries, field_path)
            for index, contribution_id in enumerate(entries):
                _validate_id(contribution_id, f"{field_path}[{index}]")
        if "replacements" in surface:
            field_path = f"{surface_path}.replacements"
            replacements = surface["replacements"]
            if not isinstance(replacements, dict):
                raise UiLayoutError(field_path, "must be an object mapping contribution IDs")
            _validate_count(replacements, field_path)
            for target, replacement in replacements.items():
                _validate_id(target, f"{field_path}[{target!r}]")
                # Earlier browser-local layouts used an empty replacement for
                # the default element. Retain it so explicit import is lossless.
                if replacement != "":
                    _validate_id(replacement, f"{field_path}[{target!r}]")
    return normalized


def _validate_id(value: Any, path: str) -> None:
    if not isinstance(value, str) or not value or len(value) > MAX_UI_LAYOUT_ID_LENGTH:
        raise UiLayoutError(
            path, f"must be a non-empty string of at most {MAX_UI_LAYOUT_ID_LENGTH} characters"
        )


def _validate_count(value: list[Any] | dict[str, Any], path: str) -> None:
    if len(value) > MAX_UI_LAYOUT_ITEMS:
        raise UiLayoutError(path, f"must contain at most {MAX_UI_LAYOUT_ITEMS} entries")
