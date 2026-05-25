"""Tests for public Settings schema parsing."""

from __future__ import annotations

import pytest

from core.settings import SettingsValidationError, parse_settings_update


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
    ],
)
def test_parse_settings_update_rejects_invalid_payloads(
    params: dict,
    message: str,
) -> None:
    with pytest.raises(SettingsValidationError, match=message):
        parse_settings_update(params)
