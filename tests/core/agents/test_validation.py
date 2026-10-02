"""Tests for Agents-owned ``agent.json`` validation."""

from __future__ import annotations

from typing import Any

import pytest

from core.agents import validate_agent_data


def _valid_agent_data() -> dict[str, Any]:
    return {
        "format_version": 1,
        "id": "coder",
        "name": "Coder",
        "model": "",
        "fallback_models": [],
        "temperature": None,
        "thinking_effort": None,
        "memory_prompt_mode": "agent_user",
        "tool_access": {"mode": "all"},
        "allowed_skills": ["*"],
        "excluded_skills": ["pdf"],
        "custom_system_prompt_enabled": False,
        "librarian_enabled": False,
        "created_at": "2026-05-03T12:00:00Z",
        "updated_at": "2026-05-03T12:00:00Z",
    }


def _diagnostics(data: object) -> list[tuple[str, str, str]]:
    return [
        (diagnostic.severity, diagnostic.path, diagnostic.message)
        for diagnostic in validate_agent_data(data)
    ]


def _all_optional_fields_null() -> dict[str, Any]:
    data: dict[str, Any] = dict.fromkeys(
        [
            *_valid_agent_data(),
            "workspace",
            "root_project_id",
            "tools",
            "compaction_policy",
            "current_session_id",
        ]
    )
    data.update(format_version=1, id="minimal")
    return data


@pytest.mark.parametrize(
    "data",
    [
        _valid_agent_data(),
        {"format_version": 1, "id": "minimal"},
        # Null optional fields count as missing.
        _all_optional_fields_null(),
    ],
    ids=["complete", "only-id", "null-optional-fields"],
)
def test_validate_agent_data_accepts_a_document_that_names_only_its_id(
    data: dict[str, Any],
) -> None:
    assert _diagnostics(data) == []


@pytest.mark.parametrize(
    ("changes", "diagnostic"),
    [
        ({"id": None}, ("$.id", "must be a non-empty string")),
        (
            {"tools": {"subagent": {"allowed_agents": ["worker", 1]}}},
            ("$.tools.subagent.allowed_agents[1]", "must be a string"),
        ),
        (
            {"tools": {"bash": {"allowed_env": ["OPENAI_API_KEY", "bad-key"]}}},
            (
                "$.tools.bash.allowed_env",
                "tools.bash.allowed_env has invalid environment key name(s): 'bad-key'",
            ),
        ),
        (
            {"custom_system_prompt_enabled": "yes"},
            ("$.custom_system_prompt_enabled", "must be a boolean"),
        ),
        ({"librarian_enabled": "no"}, ("$.librarian_enabled", "must be a boolean")),
        (
            {"memory_prompt_mode": "sometimes"},
            ("$.memory_prompt_mode", "must be one of: agent, agent_user, off"),
        ),
        ({"temperature": float("nan")}, ("$.temperature", "must be finite")),
        ({"temperature": 2.5}, ("$.temperature", "must be between 0 and 2")),
        (
            {"thinking_effort": "extreme"},
            (
                "$.thinking_effort",
                "must be one of: '', 'high', 'low', 'max', 'medium', 'minimal', 'none', 'xhigh'",
            ),
        ),
        (
            {"fallback_models": ["openai/gpt-5.2", 12]},
            ("$.fallback_models", "must be a list of strings"),
        ),
        (
            {"fallback_models": ["openai/gpt-5.2", "openai/gpt-5.2"]},
            ("$.fallback_models", "must not contain duplicates: openai/gpt-5.2"),
        ),
        (
            {"fallback_models": [f"openai/model-{index}" for index in range(6)]},
            ("$.fallback_models", "accepts at most 5 entries, got 6"),
        ),
        (
            {"excluded_skills": ["pdf", ""]},
            ("$.excluded_skills[1]", "must be a non-empty string"),
        ),
        (
            {"excluded_skills": ["*"]},
            (
                "$.excluded_skills",
                'cannot contain "*"; set allowed_skills to [] to allow no Skills',
            ),
        ),
    ],
    ids=[
        "missing-id",
        "subagent-target",
        "bash-env-key",
        "custom-prompt-toggle",
        "librarian-switch",
        "memory-mode",
        "non-finite-temperature",
        "temperature-range",
        "thinking-effort",
        "fallback-entry",
        "fallback-duplicates",
        "fallback-length",
        "excluded-skill-entry",
        "excluded-skills-wildcard",
    ],
)
def test_validate_agent_data_reports_one_error_per_invalid_field(
    changes: dict[str, Any], diagnostic: tuple[str, str]
) -> None:
    data = _valid_agent_data()
    data.update(changes)
    if data["id"] is None:
        del data["id"]

    assert _diagnostics(data) == [("error", *diagnostic)]
