"""Tests for central ``project.json`` validation in core.settings.validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.settings import (
    is_valid_project_id,
    load_validated_project_json,
    validate_data_dir_config,
    validate_project_data,
    validate_project_file,
)
from core.settings.settings import SettingsValidationError


def _valid_project_data() -> dict[str, object]:
    return {
        "project_id": "vbot",
        "display_name": "vBot",
        "cwd": "/srv/repos/vbot",
        "default_agent": "orchestrator",
        "default_model": "openai/gpt-5",
        "auto_load": ["AGENTS.md", "PROJECT.md"],
        "created_at": "2026-06-18T10:00:00Z",
        "updated_at": "2026-06-18T10:00:00Z",
    }


def _diagnostics(data: object) -> list[tuple[str, str, str]]:
    return [
        (diagnostic.severity, diagnostic.path, diagnostic.message)
        for diagnostic in validate_project_data(data)
    ]


def test_validate_project_data_accepts_full_valid_config() -> None:
    assert validate_project_data(_valid_project_data()) == []


def test_validate_project_data_accepts_minimal_project_just_a_cwd() -> None:
    data = {
        "project_id": "scratch",
        "display_name": "Scratch",
        "cwd": "/srv/repos/scratch",
        "created_at": "2026-06-18T10:00:00Z",
        "updated_at": "2026-06-18T10:00:00Z",
    }

    assert validate_project_data(data) == []


def test_validate_project_data_accepts_empty_optional_pointers_and_auto_load() -> None:
    data = _valid_project_data()
    data["default_agent"] = ""
    data["default_model"] = ""
    data["auto_load"] = []

    assert validate_project_data(data) == []


def test_validate_project_data_rejects_non_object_root() -> None:
    assert _diagnostics([1, 2, 3]) == [("error", "$", "Expected a JSON object, got list")]


def test_validate_project_data_rejects_invalid_project_id() -> None:
    data = _valid_project_data()
    data["project_id"] = "bad/slug"

    assert (
        "error",
        "$.project_id",
        "must be 1-64 characters using only letters, numbers, hyphen, or underscore",
    ) in _diagnostics(data)


def test_validate_project_data_rejects_empty_display_name() -> None:
    data = _valid_project_data()
    data["display_name"] = "   "

    assert ("error", "$.display_name", "must be a non-empty string") in _diagnostics(data)


def test_validate_project_data_rejects_empty_cwd() -> None:
    data = _valid_project_data()
    data["cwd"] = ""

    assert ("error", "$.cwd", "must be a non-empty string") in _diagnostics(data)


def test_validate_project_data_rejects_non_string_default_pointer() -> None:
    data = _valid_project_data()
    data["default_agent"] = 7

    assert ("error", "$.default_agent", "must be a string or null") in _diagnostics(data)


def test_validate_project_data_rejects_non_list_auto_load() -> None:
    data = _valid_project_data()
    data["auto_load"] = "AGENTS.md"

    assert ("error", "$.auto_load", "must be a list of strings") in _diagnostics(data)


def test_validate_project_data_rejects_empty_auto_load_entry() -> None:
    data = _valid_project_data()
    data["auto_load"] = ["AGENTS.md", "  "]

    assert ("error", "$.auto_load[1]", "must be a non-empty string") in _diagnostics(data)


def test_validate_project_data_warns_on_unknown_field() -> None:
    data = _valid_project_data()
    data["team"] = ["builder"]

    assert ("warning", "$.team", "unknown project field: team") in _diagnostics(data)


def test_validate_project_data_reports_missing_required_fields() -> None:
    paths = {path for _, path, _ in _diagnostics({"project_id": "vbot"})}

    assert "$.display_name" in paths
    assert "$.cwd" in paths
    assert "$.created_at" in paths
    assert "$.updated_at" in paths


def test_validate_project_file_reports_missing_file(tmp_path: Path) -> None:
    report = validate_project_file(tmp_path / "project.json")

    assert report.exists is False
    assert not report.ok


def test_validate_project_file_accepts_valid_file(tmp_path: Path) -> None:
    config_path = tmp_path / "project.json"
    config_path.write_text(json.dumps(_valid_project_data()), encoding="utf-8")

    report = validate_project_file(config_path)

    assert report.ok
    assert report.exists


def test_load_validated_project_json_returns_mapping(tmp_path: Path) -> None:
    config_path = tmp_path / "project.json"
    config_path.write_text(json.dumps(_valid_project_data()), encoding="utf-8")

    loaded = load_validated_project_json(config_path)

    assert loaded["project_id"] == "vbot"
    assert loaded["cwd"] == "/srv/repos/vbot"


def test_load_validated_project_json_raises_on_invalid(tmp_path: Path) -> None:
    config_path = tmp_path / "project.json"
    config_path.write_text(json.dumps({"project_id": "x"}), encoding="utf-8")

    with pytest.raises(SettingsValidationError):
        load_validated_project_json(config_path)


def test_validate_data_dir_config_includes_project_files(tmp_path: Path) -> None:
    project_dir = tmp_path / "projects" / "vbot"
    project_dir.mkdir(parents=True)
    (project_dir / "project.json").write_text(
        json.dumps(_valid_project_data()), encoding="utf-8"
    )

    reports = validate_data_dir_config(tmp_path)

    project_reports = [
        report for report in reports if report.file_path.name == "project.json"
    ]
    assert len(project_reports) == 1
    assert project_reports[0].ok


@pytest.mark.parametrize("project_id", ["vbot", "a", "Project_1", "x-y_z", "0", "a" * 64])
def test_is_valid_project_id_accepts_filesystem_safe_slugs(project_id: str) -> None:
    assert is_valid_project_id(project_id) is True


@pytest.mark.parametrize(
    "project_id",
    ["", ".hidden", "../escape", "with space", "slash/name", "_leading", "-leading", "a" * 65],
)
def test_is_valid_project_id_rejects_unsafe_values(project_id: str) -> None:
    assert is_valid_project_id(project_id) is False


def test_is_valid_project_id_rejects_non_string() -> None:
    assert is_valid_project_id(123) is False
    assert is_valid_project_id(None) is False
