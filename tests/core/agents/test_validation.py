"""Tests for Agents-owned ``agent.json`` validation."""

from __future__ import annotations

from core.agents import validate_agent_data


def _valid_agent_data() -> dict[str, object]:
    return {
        "id": "coder",
        "name": "Coder",
        "model": "",
        "fallback_model": "",
        "temperature": None,
        "thinking_effort": None,
        "memory_prompt_mode": "agent_user",
        "allowed_tools": ["*"],
        "allowed_skills": ["*"],
        "custom_system_prompt_enabled": False,
        "created_at": "2026-05-03T12:00:00Z",
        "updated_at": "2026-05-03T12:00:00Z",
    }


def _diagnostics(data: object) -> list[tuple[str, str, str]]:
    return [
        (diagnostic.severity, diagnostic.path, diagnostic.message)
        for diagnostic in validate_agent_data(data)
    ]


def test_validate_agent_data_accepts_missing_custom_prompt_toggle() -> None:
    data = _valid_agent_data()
    del data["custom_system_prompt_enabled"]

    assert validate_agent_data(data) == []


def test_validate_agent_data_accepts_missing_tool_settings() -> None:
    data = _valid_agent_data()

    assert _diagnostics(data) == []


def test_validate_agent_data_requires_only_id() -> None:
    assert _diagnostics({"id": "minimal"}) == []


def test_validate_agent_data_treats_null_optional_fields_as_missing() -> None:
    data = dict.fromkeys(_valid_agent_data())
    data["id"] = "minimal"
    data["workspace"] = None
    data["root_project_id"] = None
    data["tools"] = None
    data["compaction_policy"] = None
    data["current_session_id"] = None

    assert _diagnostics(data) == []


def test_validate_agent_data_rejects_missing_id_once() -> None:
    assert _diagnostics({}) == [
        (
            "error",
            "$.id",
            "must be a non-empty string",
        )
    ]


def test_validate_agent_data_validates_optional_subagent_tool_settings() -> None:
    data = _valid_agent_data()
    data["tools"] = {"subagent": {"allowed_agents": ["worker", 1]}}

    assert _diagnostics(data) == [
        (
            "error",
            "$.tools.subagent.allowed_agents[1]",
            "must be a string",
        )
    ]


def test_validate_agent_data_validates_optional_bash_env_grants() -> None:
    data = _valid_agent_data()
    data["tools"] = {"bash": {"allowed_env": ["OPENAI_API_KEY", "bad-key"]}}

    assert _diagnostics(data) == [
        (
            "error",
            "$.tools.bash.allowed_env",
            "tools.bash.allowed_env has invalid environment key name(s): 'bad-key'",
        )
    ]


def test_validate_agent_data_rejects_non_bool_custom_prompt_toggle() -> None:
    data = _valid_agent_data()
    data["custom_system_prompt_enabled"] = "yes"

    assert _diagnostics(data) == [("error", "$.custom_system_prompt_enabled", "must be a boolean")]


def test_validate_agent_data_rejects_invalid_memory_prompt_mode() -> None:
    data = _valid_agent_data()
    data["memory_prompt_mode"] = "sometimes"

    assert _diagnostics(data) == [
        ("error", "$.memory_prompt_mode", "must be one of: agent, agent_user, off")
    ]


def test_validate_agent_data_treats_null_memory_prompt_mode_as_missing() -> None:
    data = _valid_agent_data()
    data["memory_prompt_mode"] = None

    assert _diagnostics(data) == []


def test_validate_agent_data_rejects_non_finite_temperature() -> None:
    data = _valid_agent_data()
    data["temperature"] = float("nan")

    assert _diagnostics(data) == [("error", "$.temperature", "must be finite")]


def test_validate_agent_data_rejects_out_of_range_temperature() -> None:
    data = _valid_agent_data()
    data["temperature"] = 2.5

    assert _diagnostics(data) == [("error", "$.temperature", "must be between 0 and 2")]


def test_validate_agent_data_rejects_invalid_thinking_effort() -> None:
    data = _valid_agent_data()
    data["thinking_effort"] = "extreme"

    assert _diagnostics(data) == [
        (
            "error",
            "$.thinking_effort",
            "must be one of: '', 'high', 'low', 'max', 'medium', 'minimal', 'none', 'xhigh'",
        )
    ]
