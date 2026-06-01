"""Tests for public Settings schema parsing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.settings import (
    SettingsValidationError,
    SettingsValidationReport,
    parse_settings_update,
    validate_agent_data,
    validate_settings_file,
)


def diagnostics_as_tuples(report: SettingsValidationReport) -> list[tuple[str, str, str]]:
    return [
        (diagnostic.severity, diagnostic.path, diagnostic.message)
        for diagnostic in report.diagnostics
    ]


def test_parse_settings_update_normalizes_all_supported_sections() -> None:
    parsed = parse_settings_update(
        {
            "appearance": {"language": "en"},
            "skills": {"directories": ["~/skills", " C:/skills/team "]},
            "subagents": {
                "max_subagent_depth": 6,
                "max_subagents_per_turn": 12,
                "subagent_timeout_minutes": 90,
            },
            "compaction": {
                "auto": False,
                "threshold": 1,
                "tail_tokens": 12_000,
                "summary_model": "openai/gpt-5.2",
            },
            "defaults": {
                "agent": {
                    "model": "openai/gpt-5.2",
                    "fallback_model": "openai/gpt-5.1",
                    "temperature": 1,
                    "thinking_effort": "",
                }
            },
            "recall": {"backend": "sqlite_fts"},
            "model_tasks": {
                "speech_to_text": {
                    "target": "openrouter/openai/gpt-4o-transcribe::api-key",
                    "options": {"language": "auto"},
                }
            },
        }
    )

    assert parsed == {
        "appearance": {"language": "en"},
        "skills": {"directories": ["~/skills", " C:/skills/team "]},
        "subagents": {
            "max_subagent_depth": 6,
            "max_subagents_per_turn": 12,
            "subagent_timeout_minutes": 90,
        },
        "compaction": {
            "auto": False,
            "threshold": 1.0,
            "tail_tokens": 12_000,
            "summary_model": "openai/gpt-5.2",
        },
        "defaults": {
            "agent": {
                "model": "openai/gpt-5.2",
                "fallback_model": "openai/gpt-5.1",
                "temperature": 1.0,
                "thinking_effort": "",
            }
        },
        "recall": {"backend": "sqlite_fts"},
        "model_tasks": {
            "speech_to_text": {
                "target": "openrouter/openai/gpt-4o-transcribe::api-key",
                "options": {"language": "auto"},
            }
        },
    }


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({}, "settings.update requires a section"),
        ({"general": {}}, "unsupported settings sections: general"),
        ({"appearance": []}, "params.appearance must be an object"),
        ({"skills": {"directories": [1]}}, "params.skills.directories"),
        (
            {
                "subagents": {
                    "max_subagent_depth": 4,
                    "max_subagents_per_turn": 8,
                }
            },
            "missing sub-agent settings: subagent_timeout_minutes",
        ),
        (
            {
                "compaction": {
                    "auto": True,
                    "threshold": 1.5,
                    "tail_tokens": 15_000,
                    "summary_model": None,
                }
            },
            "params.compaction.threshold must be in",
        ),
        (
            {"defaults": {"agent": {"unknown_field": True}}},
            "unsupported defaults.agent settings: unknown_field",
        ),
        ({"recall": []}, "params.recall must be an object"),
        (
            {"recall": {"backend": "unknown_backend"}},
            "params.recall.backend must be one of",
        ),
        ({"model_tasks": []}, "params.model_tasks must be an object"),
        (
            {"model_tasks": {"speech_to_text": {"target": 1}}},
            "params.model_tasks.speech_to_text.target must be a string",
        ),
        (
            {"model_tasks": {"speech_to_text": {"options": []}}},
            "params.model_tasks.speech_to_text.options must be an object",
        ),
    ],
)
def test_parse_settings_update_rejects_invalid_payloads(
    params: dict,
    message: str,
) -> None:
    with pytest.raises(SettingsValidationError, match=message):
        parse_settings_update(params)


def test_validate_settings_file_accepts_missing_settings(tmp_path: Path) -> None:
    report = validate_settings_file(tmp_path / "settings.json")

    assert report.ok is True
    assert report.exists is False
    assert report.diagnostics == ()


def test_validate_settings_file_accepts_known_settings(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "server_port": 8500,
                "appearance": {"language": "en"},
                "skill_directories": ["~/skills"],
                "extension_directories": ["C:/vbot/extensions"],
                "attachment_max_size_bytes": 1024,
                "speech_upload_max_size_bytes": 2048,
                "max_subagent_depth": 4,
                "max_subagents_per_turn": 8,
                "subagent_timeout_minutes": 60,
                "compaction": {
                    "auto": True,
                    "threshold": 0.8,
                    "tail_tokens": 15_000,
                    "summary_model": None,
                },
                "recall": {"backend": "sqlite_fts"},
                "defaults": {
                    "agent": {
                        "model": "openai/gpt-5.2",
                        "fallback_model": "",
                        "temperature": 0.7,
                        "thinking_effort": "medium",
                    }
                },
                "model_tasks": {
                    "speech_to_text": {
                        "target": "openrouter/openai/gpt-4o-transcribe::api-key",
                        "options": {"language": "auto"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    report = validate_settings_file(settings_path)

    assert report.ok is True
    assert report.exists is True
    assert report.diagnostics == ()


def test_validate_settings_file_reports_invalid_json(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{", encoding="utf-8")

    report = validate_settings_file(settings_path)

    assert report.ok is False
    assert diagnostics_as_tuples(report) == [
        (
            "error",
            "$",
            "Invalid JSON: Expecting property name enclosed in double quotes at line 1 column 2",
        )
    ]


def test_validate_settings_file_reports_wrong_root_type(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("[]", encoding="utf-8")

    report = validate_settings_file(settings_path)

    assert report.ok is False
    assert diagnostics_as_tuples(report) == [("error", "$", "Expected a JSON object, got list")]


def test_validate_settings_file_reports_invalid_fields(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "server_port": 70000,
                "skill_directories": ["relative/path"],
                "attachment_max_size_bytes": 0,
                "speech_upload_max_size_bytes": 0,
                "compaction": {"threshold": 2, "tail_tokens": False},
                "defaults": {"agent": {"temperature": "warm", "unknown": True}},
                "model_tasks": {"speech_to_text": {"target": "", "options": []}},
                "typo": True,
            }
        ),
        encoding="utf-8",
    )

    report = validate_settings_file(settings_path)

    assert report.ok is False
    assert diagnostics_as_tuples(report) == [
        ("warning", "$.typo", "unknown settings key: typo"),
        ("error", "$.server_port", "must be between 1 and 65535"),
        ("error", "$.skill_directories[0]", "must be an absolute or home-relative path"),
        ("error", "$.attachment_max_size_bytes", "must be a positive integer"),
        ("error", "$.speech_upload_max_size_bytes", "must be a positive integer"),
        ("error", "$.compaction.threshold", "must be in (0, 1]"),
        ("error", "$.compaction.tail_tokens", "must be a positive integer"),
        (
            "error",
            "$.defaults.agent.unknown",
            "unsupported defaults.agent setting: unknown",
        ),
        ("error", "$.defaults.agent.temperature", "must be a number"),
        ("error", "$.model_tasks.speech_to_text.target", "must be a non-empty string"),
        ("error", "$.model_tasks.speech_to_text.options", "must be an object"),
    ]


def test_validate_settings_file_reports_invalid_recall_backend(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"recall": {"backend": "SQLite FTS"}}), encoding="utf-8")

    report = validate_settings_file(settings_path)

    assert report.ok is False
    assert diagnostics_as_tuples(report) == [
        ("error", "$.recall.backend", "must use lowercase snake_case")
    ]


def _valid_agent_data() -> dict[str, object]:
    return {
        "id": "coder",
        "name": "Coder",
        "model": "",
        "fallback_model": "",
        "temperature": None,
        "thinking_effort": None,
        "allowed_tools": ["*"],
        "allowed_skills": ["*"],
        "custom_system_prompt_enabled": False,
        "created_at": "2026-05-03T12:00:00Z",
        "updated_at": "2026-05-03T12:00:00Z",
    }


def test_validate_agent_data_accepts_missing_custom_prompt_toggle() -> None:
    data = _valid_agent_data()
    del data["custom_system_prompt_enabled"]

    diagnostics = validate_agent_data(data)

    assert diagnostics == []


def test_validate_agent_data_rejects_non_bool_custom_prompt_toggle() -> None:
    data = _valid_agent_data()
    data["custom_system_prompt_enabled"] = "yes"

    diagnostics = validate_agent_data(data)

    assert diagnostics_as_tuples(
        SettingsValidationReport(
            file_path=Path("agent.json"),
            exists=True,
            diagnostics=tuple(diagnostics),
        )
    ) == [("error", "$.custom_system_prompt_enabled", "must be a boolean")]
