"""Tests for debug settings parsing and validation."""

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


def _diagnostics_as_tuples(report: SettingsValidationReport) -> list[tuple[str, str, str]]:
    return [
        (diagnostic.severity, diagnostic.path, diagnostic.message)
        for diagnostic in report.diagnostics
    ]


class TestParseDebugUpdate:
    def test_enabled_true(self) -> None:
        result = parse_settings_update({"debug": {"enabled": True}})
        assert result == {"debug": {"enabled": True}}

    def test_enabled_false(self) -> None:
        result = parse_settings_update({"debug": {"enabled": False}})
        assert result == {"debug": {"enabled": False}}

    def test_trace_limit_50(self) -> None:
        result = parse_settings_update({"debug": {"trace_limit": 50}})
        assert result == {"debug": {"trace_limit": 50}}

    def test_trace_limit_1(self) -> None:
        result = parse_settings_update({"debug": {"trace_limit": 1}})
        assert result == {"debug": {"trace_limit": 1}}

    def test_trace_limit_500(self) -> None:
        result = parse_settings_update({"debug": {"trace_limit": 500}})
        assert result == {"debug": {"trace_limit": 500}}

    def test_both_fields(self) -> None:
        result = parse_settings_update({"debug": {"enabled": True, "trace_limit": 100}})
        assert result == {"debug": {"enabled": True, "trace_limit": 100}}

    def test_empty_debug_dict(self) -> None:
        result = parse_settings_update({"debug": {}})
        assert result == {"debug": {}}

    def test_debug_not_a_dict(self) -> None:
        with pytest.raises(SettingsValidationError, match="params.debug must be an object"):
            parse_settings_update({"debug": []})

    def test_unknown_field(self) -> None:
        with pytest.raises(SettingsValidationError, match="unsupported debug settings: extra_key"):
            parse_settings_update({"debug": {"enabled": True, "extra_key": 1}})

    def test_multiple_unknown_fields(self) -> None:
        with pytest.raises(SettingsValidationError, match="unsupported debug settings"):
            parse_settings_update({"debug": {"enabled": True, "a": 1, "b": 2}})

    @pytest.mark.parametrize(
        "value",
        ["yes", 1, 0, None, 1.0, [], {}],
    )
    def test_enabled_not_boolean(self, value: object) -> None:
        with pytest.raises(SettingsValidationError, match="params.debug.enabled must be a boolean"):
            parse_settings_update({"debug": {"enabled": value}})

    @pytest.mark.parametrize(
        ("value", "expected_message"),
        [
            ("fifty", "params.debug.trace_limit must be a positive integer"),
            (1.5, "params.debug.trace_limit must be a positive integer"),
            (True, "params.debug.trace_limit must be a positive integer"),
            (None, "params.debug.trace_limit must be a positive integer"),
            ([], "params.debug.trace_limit must be a positive integer"),
            ({}, "params.debug.trace_limit must be a positive integer"),
        ],
    )
    def test_trace_limit_not_integer(self, value: object, expected_message: str) -> None:
        with pytest.raises(SettingsValidationError, match=expected_message):
            parse_settings_update({"debug": {"trace_limit": value}})

    def test_trace_limit_zero(self) -> None:
        with pytest.raises(
            SettingsValidationError,
            match="params.debug.trace_limit must be a positive integer",
        ):
            parse_settings_update({"debug": {"trace_limit": 0}})

    def test_trace_limit_negative(self) -> None:
        with pytest.raises(
            SettingsValidationError,
            match="params.debug.trace_limit must be a positive integer",
        ):
            parse_settings_update({"debug": {"trace_limit": -1}})

    def test_trace_limit_exceeds_500(self) -> None:
        with pytest.raises(
            SettingsValidationError, match="params.debug.trace_limit must not exceed 500"
        ):
            parse_settings_update({"debug": {"trace_limit": 501}})

    def test_trace_limit_501(self) -> None:
        with pytest.raises(
            SettingsValidationError, match="params.debug.trace_limit must not exceed 500"
        ):
            parse_settings_update({"debug": {"trace_limit": 501}})


class TestValidateDebug:
    def test_valid_debug_section_has_no_errors(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(
            json.dumps({"debug": {"enabled": True, "trace_limit": 100}}),
            encoding="utf-8",
        )

        report = validate_settings_file(settings_path)

        assert report.ok is True
        assert report.exists is True
        assert report.diagnostics == ()

    def test_omitting_debug_section_is_valid(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(
            json.dumps({"server_port": 8500}),
            encoding="utf-8",
        )

        report = validate_settings_file(settings_path)

        assert report.ok is True

    def test_debug_section_with_only_enabled(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(
            json.dumps({"debug": {"enabled": False}}),
            encoding="utf-8",
        )

        report = validate_settings_file(settings_path)

        assert report.ok is True
        assert report.diagnostics == ()

    def test_debug_section_with_only_trace_limit(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(
            json.dumps({"debug": {"trace_limit": 200}}),
            encoding="utf-8",
        )

        report = validate_settings_file(settings_path)

        assert report.ok is True
        assert report.diagnostics == ()

    def test_debug_not_an_object(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(
            json.dumps({"debug": []}),
            encoding="utf-8",
        )

        report = validate_settings_file(settings_path)

        assert report.ok is False
        assert _diagnostics_as_tuples(report) == [
            ("error", "$.debug", "must be an object"),
        ]

    def test_non_boolean_enabled(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(
            json.dumps({"debug": {"enabled": "yes"}}),
            encoding="utf-8",
        )

        report = validate_settings_file(settings_path)

        assert report.ok is False
        assert _diagnostics_as_tuples(report) == [
            ("error", "$.debug.enabled", "must be a boolean"),
        ]

    def test_non_integer_trace_limit(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(
            json.dumps({"debug": {"trace_limit": "fifty"}}),
            encoding="utf-8",
        )

        report = validate_settings_file(settings_path)

        assert report.ok is False
        assert _diagnostics_as_tuples(report) == [
            ("error", "$.debug.trace_limit", "must be a positive integer (1-500)"),
        ]

    def test_zero_trace_limit(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(
            json.dumps({"debug": {"trace_limit": 0}}),
            encoding="utf-8",
        )

        report = validate_settings_file(settings_path)

        assert report.ok is False
        assert _diagnostics_as_tuples(report) == [
            ("error", "$.debug.trace_limit", "must be at least 1"),
        ]

    def test_negative_trace_limit(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(
            json.dumps({"debug": {"trace_limit": -5}}),
            encoding="utf-8",
        )

        report = validate_settings_file(settings_path)

        assert report.ok is False
        assert _diagnostics_as_tuples(report) == [
            ("error", "$.debug.trace_limit", "must be at least 1"),
        ]

    def test_trace_limit_exceeds_500(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(
            json.dumps({"debug": {"trace_limit": 501}}),
            encoding="utf-8",
        )

        report = validate_settings_file(settings_path)

        assert report.ok is False
        assert _diagnostics_as_tuples(report) == [
            ("error", "$.debug.trace_limit", "must be at most 500"),
        ]

    def test_boolean_trace_limit(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(
            json.dumps({"debug": {"trace_limit": True}}),
            encoding="utf-8",
        )

        report = validate_settings_file(settings_path)

        assert report.ok is False
        assert _diagnostics_as_tuples(report) == [
            ("error", "$.debug.trace_limit", "must be a positive integer (1-500)"),
        ]

    def test_unknown_debug_field_warns(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(
            json.dumps({"debug": {"enabled": True, "extra": 1}}),
            encoding="utf-8",
        )

        report = validate_settings_file(settings_path)

        assert report.ok is True
        assert _diagnostics_as_tuples(report) == [
            ("warning", "$.debug.extra", "unknown debug field: extra"),
        ]

    def test_multiple_invalid_debug_fields(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(
            json.dumps(
                {
                    "debug": {
                        "enabled": "yes",
                        "trace_limit": 0,
                        "unknown": True,
                    }
                }
            ),
            encoding="utf-8",
        )

        report = validate_settings_file(settings_path)

        assert report.ok is False
        diagnostics = _diagnostics_as_tuples(report)
        assert ("warning", "$.debug.unknown", "unknown debug field: unknown") in diagnostics
        assert ("error", "$.debug.enabled", "must be a boolean") in diagnostics
        assert ("error", "$.debug.trace_limit", "must be at least 1") in diagnostics

    def test_debug_with_trace_limit_at_500_is_valid(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(
            json.dumps({"debug": {"trace_limit": 500}}),
            encoding="utf-8",
        )

        report = validate_settings_file(settings_path)

        assert report.ok is True
        assert report.diagnostics == ()

    def test_debug_with_trace_limit_at_1_is_valid(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(
            json.dumps({"debug": {"trace_limit": 1}}),
            encoding="utf-8",
        )

        report = validate_settings_file(settings_path)

        assert report.ok is True
        assert report.diagnostics == ()
