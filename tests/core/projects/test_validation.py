"""Tests for Projects-owned ``project.json`` validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.projects import (
    ProjectError,
    load_validated_project_json,
    validate_project_data,
    validate_project_file,
)
from core.settings import is_valid_project_id

_THINKING_EFFORTS = "'', 'high', 'low', 'max', 'medium', 'minimal', 'none', 'xhigh'"


def _valid_project_data() -> dict[str, object]:
    return {
        "format_version": 1,
        "project_id": "vbot",
        "display_name": "vBot",
        "cwd": "/srv/repos/vbot",
        "default_agent": "orchestrator",
        "default_model": "openai/gpt-5",
        "source_format": "opencode",
        "auto_load": ["AGENTS.md", "PROJECT.md"],
        "allowed_tools": ["read_file", "bash"],
        "created_at": "2026-06-18T10:00:00Z",
        "updated_at": "2026-06-18T10:00:00Z",
    }


def _diagnostics(data: object) -> list[tuple[str, str, str]]:
    return [
        (diagnostic.severity, diagnostic.path, diagnostic.message)
        for diagnostic in validate_project_data(data)
    ]


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({}, id="full"),
        pytest.param(
            {
                "display_name": None,
                "default_agent": "",
                "default_model": "",
                "default_temperature": None,
                # "" is the explicit Provider default, not a missing value.
                "default_thinking_effort": "",
                "auto_load": [],
                # An empty Tool Whitelist (every Tool off) is a value, not an error.
                "allowed_tools": [],
            },
            id="empty-optional-values",
        ),
        pytest.param({"display_name": "   "}, id="blank-display-name"),
        pytest.param(
            {
                "default_temperature": 0.4,
                "default_thinking_effort": "high",
                "source_format": "claude",
                # Registry membership is runtime state: a disabled Extension must not
                # make its persisted Project unloadable.
                "allowed_tools": ["read", "disabled_extension_tool"],
                "skills_bundled_enabled": ["frontend-design"],
                "skills_global_enabled": ["pdf"],
                "skills_project_disabled": ["debugging"],
                "overrides": {
                    "builder": {
                        "model": "openai/gpt-5",
                        "temperature": 0.4,
                        "thinking_effort": "high",
                    },
                    "planner": {"model": "anthropic/claude-sonnet-4"},
                    # 0.0 (sampling floor) and "" (Provider default) are real values.
                    "floor": {"temperature": 0.0, "thinking_effort": ""},
                },
            },
            id="every-optional-field",
        ),
    ],
)
def test_validate_project_data_accepts_valid_configs(changes: dict[str, Any]) -> None:
    assert validate_project_data({**_valid_project_data(), **changes}) == []


def test_validate_project_data_accepts_only_identity_cwd_and_tool_whitelist() -> None:
    data = {
        "format_version": 1,
        "project_id": "scratch",
        "cwd": "/srv/repos/scratch",
        "allowed_tools": [],
    }

    assert validate_project_data(data) == []


@pytest.mark.parametrize(
    ("changes", "diagnostics"),
    [
        pytest.param(
            {"project_id": "bad/slug"},
            [
                (
                    "error",
                    "$.project_id",
                    "must be 1-64 characters using only letters, numbers, hyphen, or underscore",
                )
            ],
            id="project-id",
        ),
        pytest.param(
            {"cwd": ""}, [("error", "$.cwd", "must be a non-empty string")], id="empty-cwd"
        ),
        pytest.param(
            {"default_agent": 7},
            [("error", "$.default_agent", "must be a string or null")],
            id="default-pointer",
        ),
        pytest.param(
            {"default_temperature": 3.0},
            [("error", "$.default_temperature", "must be between 0 and 2")],
            id="default-temperature",
        ),
        pytest.param(
            {"default_thinking_effort": "ultra"},
            [("error", "$.default_thinking_effort", f"must be one of: {_THINKING_EFFORTS}")],
            id="default-thinking-effort",
        ),
        pytest.param(
            {"source_format": "cursor"},
            [("error", "$.source_format", "must be one of: claude, opencode")],
            id="source-format",
        ),
        pytest.param(
            {"auto_load": "AGENTS.md"},
            [("error", "$.auto_load", "must be a list of strings")],
            id="auto-load-not-a-list",
        ),
        pytest.param(
            {"auto_load": ["AGENTS.md", "  "]},
            [("error", "$.auto_load[1]", "must be a non-empty string")],
            id="empty-auto-load-entry",
        ),
        pytest.param(
            {"allowed_tools": "read"},
            [("error", "$.allowed_tools", "must be a list of strings")],
            id="tools-not-a-list",
        ),
        pytest.param(
            {"allowed_tools": ["read", "*"]},
            [
                (
                    "error",
                    "$.allowed_tools[1]",
                    "the all-tools wildcard '*' is not allowed in a Project Tool Whitelist",
                )
            ],
            id="tool-wildcard",
        ),
        pytest.param(
            {"skills_bundled_enabled": ["frontend-design", "  "]},
            [("error", "$.skills_bundled_enabled[1]", "must be a non-empty string")],
            id="empty-bundled-skill",
        ),
        pytest.param(
            {"skills_global_enabled": ["pdf", "  "]},
            [("error", "$.skills_global_enabled[1]", "must be a non-empty string")],
            id="empty-global-skill",
        ),
        pytest.param(
            {"overrides": ["builder"]},
            [("error", "$.overrides", "must be an object")],
            id="overrides-not-an-object",
        ),
        pytest.param(
            {"overrides": {"": {"model": "openai/gpt-5"}}},
            [("error", "$.overrides", "keys must be non-empty agent id strings")],
            id="empty-override-key",
        ),
        pytest.param(
            {"overrides": {"builder": "openai/gpt-5"}},
            [("error", "$.overrides.builder", "must be an object")],
            id="override-not-an-object",
        ),
        pytest.param(
            {"overrides": {"builder": {}}},
            [("error", "$.overrides.builder", "must set at least one field")],
            id="empty-override",
        ),
        pytest.param(
            {"overrides": {"builder": {"model": "  "}}},
            [("error", "$.overrides.builder.model", "must be a non-empty string")],
            id="override-model",
        ),
        pytest.param(
            {"overrides": {"builder": {"temperature": 3.0}}},
            [("error", "$.overrides.builder.temperature", "must be between 0 and 2")],
            id="override-temperature",
        ),
        pytest.param(
            {"overrides": {"builder": {"thinking_effort": "ultra"}}},
            [
                (
                    "error",
                    "$.overrides.builder.thinking_effort",
                    f"must be one of: {_THINKING_EFFORTS}",
                )
            ],
            id="override-thinking-effort",
        ),
        # Unknown fields warn for forward compatibility.
        pytest.param(
            {"team": ["builder"]},
            [("warning", "$.team", "unknown project field: team")],
            id="unknown-field",
        ),
        pytest.param(
            {
                "overrides": {
                    "builder": {"nope": "x", "tool_access": {"mode": "all", "future": 1}},
                    "future_only": {"future": True},
                }
            },
            [
                ("warning", "$.overrides.builder.nope", "unknown override field: nope"),
                (
                    "warning",
                    "$.overrides.builder.tool_access.future",
                    "unknown override field: future",
                ),
                ("warning", "$.overrides.future_only.future", "unknown override field: future"),
            ],
            id="unknown-override-fields",
        ),
    ],
)
def test_validate_project_data_reports_each_invalid_field(
    changes: dict[str, Any], diagnostics: list[tuple[str, str, str]]
) -> None:
    assert _diagnostics({**_valid_project_data(), **changes}) == diagnostics


def test_validate_project_data_rejects_non_object_root() -> None:
    assert _diagnostics([1, 2, 3]) == [("error", "$", "Expected a JSON object, got list")]


def test_validate_project_data_requires_only_cwd_and_tool_whitelist() -> None:
    assert _diagnostics({"format_version": 1, "project_id": "vbot"}) == [
        ("error", "$.cwd", "is required"),
        ("error", "$.allowed_tools", "is required"),
    ]


def test_validate_project_data_requires_the_current_format_version() -> None:
    data = _valid_project_data()
    del data["format_version"]

    assert [(severity, path) for severity, path, _ in _diagnostics(data)] == [
        ("error", "$.format_version")
    ]
    data["format_version"] = 2
    assert "written by a newer vBot" in _diagnostics(data)[0][2]


@pytest.mark.parametrize("exists", [False, True])
def test_validate_project_file_reports_the_document(tmp_path: Path, exists: bool) -> None:
    config_path = tmp_path / "project.json"
    if exists:
        config_path.write_text(json.dumps(_valid_project_data()), encoding="utf-8")

    report = validate_project_file(config_path)

    assert (report.ok, report.exists) == (exists, exists)


def test_load_validated_project_json_returns_mapping_or_raises(tmp_path: Path) -> None:
    valid_path = tmp_path / "valid.json"
    valid_path.write_text(json.dumps(_valid_project_data()), encoding="utf-8")
    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text(json.dumps({"format_version": 1, "project_id": "x"}), encoding="utf-8")

    assert load_validated_project_json(valid_path) == _valid_project_data()
    with pytest.raises(ProjectError):
        load_validated_project_json(invalid_path)


@pytest.mark.parametrize(
    ("project_id", "valid"),
    [
        *[(value, True) for value in ("vbot", "a", "Project_1", "x-y_z", "0", "a" * 64)],
        *[
            (value, False)
            for value in (
                "",
                ".hidden",
                "../escape",
                "with space",
                "slash/name",
                "_leading",
                "-leading",
                "a" * 65,
                123,
                None,
            )
        ],
    ],
)
def test_is_valid_project_id_accepts_only_filesystem_safe_slugs(
    project_id: object, valid: bool
) -> None:
    assert is_valid_project_id(project_id) is valid
