"""The shared layout accepts future fields but validates its known wire format."""

from __future__ import annotations

from typing import Any

import pytest

from core.settings import SettingsValidationError, parse_settings_update, validate_settings_data
from core.settings.paths import SettingsPathError, apply_settings_patch, parse_patch_operations


@pytest.mark.parametrize(
    "layout",
    [
        [],
        {"chat.footer": None},
        {"chat.footer": {"hidden": "x"}},
        {"chat.footer": {"order": [False]}},
        {"chat.footer": {"hidden": [""]}},
        {"chat.footer": {"replacements": []}},
        {"chat.footer": {"replacements": {"target": 1}}},
        {"chat.footer": {"future": float("nan")}},
        {"chat.footer": {"future": {"value": float("inf")}}},
        {"chat.footer": {"hidden": ["id"] * 2049}},
        {f"surface.{index}": {} for index in range(257)},
        {"chat.footer": {"order": ["x" * 513]}},
        {"chat.footer": {"future": "x" * 1_048_576}},
        {"chat.footer": {"future": {1: "non-json-key"}}},
    ],
    ids=[
        "top-level",
        "surface",
        "hidden",
        "order-item",
        "empty-id",
        "replacements",
        "replacement-id",
        "nan",
        "infinity",
        "entry-limit",
        "surface-limit",
        "id-limit",
        "byte-limit",
        "non-json-key",
    ],
)
def test_ui_layout_is_validated_at_section_path_and_raw_boundaries(layout: Any) -> None:
    with pytest.raises(SettingsValidationError):
        parse_settings_update({"appearance": {"ui_layout": layout}})
    with pytest.raises(SettingsPathError):
        apply_settings_patch(
            {},
            parse_patch_operations(
                [{"op": "set", "path": "appearance.ui_layout", "value": layout}]
            ),
        )
    diagnostics = validate_settings_data({"appearance": {"ui_layout": layout}})
    assert any(
        item.severity == "error" and "appearance.ui_layout" in item.path for item in diagnostics
    )


def test_ui_layout_rejects_excessive_depth_without_unbounded_recursion() -> None:
    layout: dict[str, Any] = {}
    cursor = layout
    for _ in range(34):
        nested: dict[str, Any] = {}
        cursor["future"] = nested
        cursor = nested
    with pytest.raises(SettingsValidationError):
        parse_settings_update({"appearance": {"ui_layout": layout}})
