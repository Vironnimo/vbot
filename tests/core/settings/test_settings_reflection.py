"""Tests for reflection settings parsing, normalization, and raw validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.settings import (
    SettingsValidationError,
    SettingsValidationReport,
    parse_settings_update,
    validate_settings_file,
)
from core.settings.normalizers import (
    REFLECTION_SETTING_DEFAULTS,
    normalize_reflection_settings,
)
from core.utils.errors import StorageError


def _diagnostics_as_tuples(report: SettingsValidationReport) -> list[tuple[str, str, str]]:
    return [
        (diagnostic.severity, diagnostic.path, diagnostic.message)
        for diagnostic in report.diagnostics
    ]


class TestParseReflectionUpdate:
    def test_enabled_true(self) -> None:
        result = parse_settings_update({"reflection": {"enabled": True}})
        assert result == {"reflection": {"enabled": True}}

    def test_full_section(self) -> None:
        result = parse_settings_update(
            {
                "reflection": {
                    "enabled": True,
                    "memory_turn_interval": 5,
                    "skill_tool_call_interval": 40,
                }
            }
        )
        assert result == {
            "reflection": {
                "enabled": True,
                "memory_turn_interval": 5,
                "skill_tool_call_interval": 40,
            }
        }

    def test_partial_interval_only(self) -> None:
        result = parse_settings_update({"reflection": {"memory_turn_interval": 3}})
        assert result == {"reflection": {"memory_turn_interval": 3}}

    def test_empty_reflection_dict(self) -> None:
        result = parse_settings_update({"reflection": {}})
        assert result == {"reflection": {}}

    def test_reflection_not_a_dict(self) -> None:
        with pytest.raises(SettingsValidationError, match="params.reflection must be an object"):
            parse_settings_update({"reflection": []})

    def test_unknown_field(self) -> None:
        with pytest.raises(SettingsValidationError, match="unsupported reflection settings: extra"):
            parse_settings_update({"reflection": {"enabled": True, "extra": 1}})

    @pytest.mark.parametrize("value", ["yes", 1, 0, None, 1.0, [], {}])
    def test_enabled_not_boolean(self, value: object) -> None:
        with pytest.raises(
            SettingsValidationError, match="params.reflection.enabled must be a boolean"
        ):
            parse_settings_update({"reflection": {"enabled": value}})

    @pytest.mark.parametrize("field", ["memory_turn_interval", "skill_tool_call_interval"])
    @pytest.mark.parametrize("value", ["five", 1.5, True, None, 0, -1])
    def test_interval_must_be_positive_integer(self, field: str, value: object) -> None:
        with pytest.raises(SettingsValidationError, match=f"params.reflection.{field}"):
            parse_settings_update({"reflection": {field: value}})


class TestNormalizeReflectionSettings:
    def test_none_section_returns_defaults(self) -> None:
        assert normalize_reflection_settings(None) == REFLECTION_SETTING_DEFAULTS

    def test_defaults_are_disabled(self) -> None:
        assert REFLECTION_SETTING_DEFAULTS["enabled"] is False

    def test_partial_section_fills_defaults(self) -> None:
        normalized = normalize_reflection_settings({"enabled": True})
        assert normalized == {**REFLECTION_SETTING_DEFAULTS, "enabled": True}

    def test_full_section_round_trips(self) -> None:
        section = {"enabled": True, "memory_turn_interval": 3, "skill_tool_call_interval": 7}
        assert normalize_reflection_settings(section) == section

    def test_non_object_section_raises(self) -> None:
        with pytest.raises(StorageError, match="settings.reflection to be an object"):
            normalize_reflection_settings("on")

    def test_non_boolean_enabled_raises(self) -> None:
        with pytest.raises(StorageError, match="enabled must be a boolean"):
            normalize_reflection_settings({"enabled": "yes"})

    @pytest.mark.parametrize("field", ["memory_turn_interval", "skill_tool_call_interval"])
    @pytest.mark.parametrize("value", ["five", 1.5, True, 0, -2])
    def test_invalid_interval_raises(self, field: str, value: object) -> None:
        with pytest.raises(StorageError, match=field):
            normalize_reflection_settings({field: value})


class TestValidateReflection:
    def test_valid_reflection_section_has_no_errors(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(
            json.dumps(
                {
                    "reflection": {
                        "enabled": True,
                        "memory_turn_interval": 10,
                        "skill_tool_call_interval": 25,
                    }
                }
            ),
            encoding="utf-8",
        )

        report = validate_settings_file(settings_path)

        assert report.ok is True
        assert report.diagnostics == ()

    def test_omitting_reflection_section_is_valid(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({"server_port": 8500}), encoding="utf-8")

        report = validate_settings_file(settings_path)

        assert report.ok is True

    def test_reflection_not_an_object(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({"reflection": []}), encoding="utf-8")

        report = validate_settings_file(settings_path)

        assert report.ok is False
        assert _diagnostics_as_tuples(report) == [
            ("error", "$.reflection", "must be an object"),
        ]

    def test_non_boolean_enabled(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({"reflection": {"enabled": "yes"}}), encoding="utf-8")

        report = validate_settings_file(settings_path)

        assert report.ok is False
        assert _diagnostics_as_tuples(report) == [
            ("error", "$.reflection.enabled", "must be a boolean"),
        ]

    @pytest.mark.parametrize("field", ["memory_turn_interval", "skill_tool_call_interval"])
    def test_non_integer_interval(self, tmp_path: Path, field: str) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({"reflection": {field: "five"}}), encoding="utf-8")

        report = validate_settings_file(settings_path)

        assert report.ok is False
        assert _diagnostics_as_tuples(report) == [
            ("error", f"$.reflection.{field}", "must be a positive integer"),
        ]

    @pytest.mark.parametrize("field", ["memory_turn_interval", "skill_tool_call_interval"])
    def test_zero_interval(self, tmp_path: Path, field: str) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({"reflection": {field: 0}}), encoding="utf-8")

        report = validate_settings_file(settings_path)

        assert report.ok is False
        assert _diagnostics_as_tuples(report) == [
            ("error", f"$.reflection.{field}", "must be at least 1"),
        ]

    def test_unknown_reflection_field_warns(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(
            json.dumps({"reflection": {"enabled": True, "extra": 1}}),
            encoding="utf-8",
        )

        report = validate_settings_file(settings_path)

        assert report.ok is True
        assert _diagnostics_as_tuples(report) == [
            ("warning", "$.reflection.extra", "unknown reflection field: extra"),
        ]
