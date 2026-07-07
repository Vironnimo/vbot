"""Tests for local-models settings parsing and validation."""

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


class TestParseLocalModelsUpdate:
    def test_valid_window(self) -> None:
        result = parse_settings_update(
            {"local_models": {"context_windows": {"ollama/ministral-3:8b": 16384}}}
        )
        assert result == {"local_models": {"context_windows": {"ollama/ministral-3:8b": 16384}}}

    def test_null_value_marks_removal(self) -> None:
        result = parse_settings_update(
            {"local_models": {"context_windows": {"ollama/ministral-3:8b": None}}}
        )
        assert result == {"local_models": {"context_windows": {"ollama/ministral-3:8b": None}}}

    def test_empty_map_is_valid(self) -> None:
        result = parse_settings_update({"local_models": {"context_windows": {}}})
        assert result == {"local_models": {"context_windows": {}}}

    def test_section_not_an_object(self) -> None:
        with pytest.raises(SettingsValidationError, match="params.local_models must be an object"):
            parse_settings_update({"local_models": []})

    def test_missing_context_windows(self) -> None:
        with pytest.raises(
            SettingsValidationError, match="params.local_models requires context_windows"
        ):
            parse_settings_update({"local_models": {}})

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(
            SettingsValidationError, match="unsupported local_models settings: extra"
        ):
            parse_settings_update({"local_models": {"context_windows": {}, "extra": 1}})

    def test_context_windows_not_an_object(self) -> None:
        with pytest.raises(
            SettingsValidationError,
            match="params.local_models.context_windows must be an object",
        ):
            parse_settings_update({"local_models": {"context_windows": []}})

    @pytest.mark.parametrize("key", ["", "no-slash", "  "])
    def test_key_without_provider_prefix_rejected(self, key: str) -> None:
        with pytest.raises(SettingsValidationError, match="'<provider>/<model_id>'"):
            parse_settings_update({"local_models": {"context_windows": {key: 4096}}})

    @pytest.mark.parametrize("value", [0, -1, "16384", 1.5, True, [], {}])
    def test_non_positive_or_non_int_window_rejected(self, value: object) -> None:
        with pytest.raises(SettingsValidationError, match="must be a positive integer"):
            parse_settings_update({"local_models": {"context_windows": {"ollama/m": value}}})


class TestValidateLocalModels:
    def test_valid_section_has_no_errors(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(
            json.dumps({"local_models": {"context_windows": {"ollama/m": 16384}}}),
            encoding="utf-8",
        )

        report = validate_settings_file(settings_path)

        assert report.ok is True
        assert report.diagnostics == ()

    def test_omitting_section_is_valid(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({"server_port": 8500}), encoding="utf-8")

        assert validate_settings_file(settings_path).ok is True

    def test_section_not_an_object(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({"local_models": []}), encoding="utf-8")

        report = validate_settings_file(settings_path)

        assert report.ok is False
        assert _diagnostics_as_tuples(report) == [
            ("error", "$.local_models", "must be an object"),
        ]

    def test_context_windows_not_an_object(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(
            json.dumps({"local_models": {"context_windows": 42}}), encoding="utf-8"
        )

        report = validate_settings_file(settings_path)

        assert report.ok is False
        assert _diagnostics_as_tuples(report) == [
            ("error", "$.local_models.context_windows", "must be an object"),
        ]

    def test_bad_key_and_bad_value_are_errors(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(
            json.dumps({"local_models": {"context_windows": {"no-slash": 4096, "ollama/m": 0}}}),
            encoding="utf-8",
        )

        report = validate_settings_file(settings_path)

        assert report.ok is False
        diagnostics = _diagnostics_as_tuples(report)
        assert (
            "error",
            "$.local_models.context_windows['no-slash']",
            "key must be a '<provider>/<model_id>' string",
        ) in diagnostics
        assert (
            "error",
            "$.local_models.context_windows['ollama/m']",
            "must be a positive integer",
        ) in diagnostics

    def test_unknown_field_warns(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(
            json.dumps({"local_models": {"context_windows": {}, "extra": 1}}),
            encoding="utf-8",
        )

        report = validate_settings_file(settings_path)

        assert report.ok is True
        assert _diagnostics_as_tuples(report) == [
            ("warning", "$.local_models.extra", "unknown local_models field: extra"),
        ]
