"""Tests for project methods overrides."""

from __future__ import annotations

from pathlib import Path

import pytest

from server.rpc.errors import RpcError
from server.rpc.project_methods import (
    _add_project,
    _clear_override,
    _set_override,
    _set_project,
    _show_project,
)
from tests.server.rpc.project_methods_test_support import (
    _make_repo,
    _make_state,
    _write_agent,
)


# ---------------------------------------------------------------------------
# Per-agent Overrides: team response fields (overrides + effective) + set/clear handlers.
# ---------------------------------------------------------------------------
def test_team_member_reports_null_overrides_by_default(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    result = _show_project(state, {"project_id": "vbot"})

    member = next(m for m in result["scan"]["team"] if m["agent_id"] == "builder")
    assert member["overrides"] is None
    # The effective block reports the model resolved from the repo (agent tier).
    assert member["effective"]["model"] == {"value": "openai/gpt-5.2", "source": "agent"}


def test_team_member_reports_overrides_value_and_effective(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})
    state.runtime.projects.set_override("vbot", "builder", "model", "openai/gpt-mini")

    result = _show_project(state, {"project_id": "vbot"})

    member = next(m for m in result["scan"]["team"] if m["agent_id"] == "builder")
    assert member["overrides"] == {"model": "openai/gpt-mini"}
    # The override is the winning tier of the effective model chain.
    assert member["effective"]["model"] == {"value": "openai/gpt-mini", "source": "override"}


def test_set_override_model_writes_override_and_returns_scan(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    result = _set_override(
        state,
        {"project_id": "vbot", "agent_id": "builder", "field": "model", "value": "openai/gpt-mini"},
    )

    member = next(m for m in result["scan"]["team"] if m["agent_id"] == "builder")
    assert member["overrides"] == {"model": "openai/gpt-mini"}
    assert state.runtime.projects.get("vbot").overrides == {"builder": {"model": "openai/gpt-mini"}}


def test_set_override_temperature_writes_override(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    _set_override(
        state, {"project_id": "vbot", "agent_id": "builder", "field": "temperature", "value": 0.4}
    )

    assert state.runtime.projects.get("vbot").overrides == {"builder": {"temperature": 0.4}}


def test_set_override_thinking_effort_writes_override(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    _set_override(
        state,
        {"project_id": "vbot", "agent_id": "builder", "field": "thinking_effort", "value": "high"},
    )

    assert state.runtime.projects.get("vbot").overrides == {"builder": {"thinking_effort": "high"}}


def test_set_tool_access_override_replaces_repository_policy(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = tmp_path / "repos" / "vbot"
    repo.mkdir(parents=True)
    _write_agent(repo, "builder.md", permission={"task": "deny"})
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    result = _set_override(
        state,
        {
            "project_id": "vbot",
            "agent_id": "builder",
            "field": "tool_access",
            "value": {"mode": "selected", "allowed": ["subagent"]},
        },
    )

    member = next(m for m in result["scan"]["team"] if m["agent_id"] == "builder")
    assert member["denied_tools"] == ["subagent"]
    assert member["overrides"] == {"tool_access": {"mode": "selected", "allowed": ["subagent"]}}
    assert member["effective"]["tool_access"] == {
        "value": {"mode": "selected", "allowed": ["subagent"]},
        "source": "override",
    }
    assert member["tools"] == {"subagent": {"allowed_agents": []}}


def test_set_tool_access_override_rejects_overlap_and_out_of_ceiling_names(
    tmp_path: Path,
) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    for value in (
        {"mode": "selected", "allowed": ["read"], "denied": ["read"]},
        {"mode": "selected", "allowed": ["missing_tool"]},
        {"mode": "all", "granted": ["computer"]},
    ):
        with pytest.raises(RpcError) as exc_info:
            _set_override(
                state,
                {
                    "project_id": "vbot",
                    "agent_id": "builder",
                    "field": "tool_access",
                    "value": value,
                },
            )

        assert exc_info.value.code == "invalid_request"


def test_clear_tool_access_override_restores_repository_policy(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = tmp_path / "repos" / "vbot"
    repo.mkdir(parents=True)
    _write_agent(repo, "builder.md", permission={"task": "deny"})
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})
    state.runtime.projects.set_override(
        "vbot",
        "builder",
        "tool_access",
        {"mode": "selected", "allowed": ["subagent"]},
    )

    result = _clear_override(
        state,
        {"project_id": "vbot", "agent_id": "builder", "field": "tool_access"},
    )

    member = next(m for m in result["scan"]["team"] if m["agent_id"] == "builder")
    assert member["overrides"] is None
    assert member["effective"]["tool_access"]["source"] == "agent"
    assert member["tools"] == {}


def test_set_override_rejects_agent_outside_project_team(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    with pytest.raises(RpcError) as exc_info:
        _set_override(
            state,
            {
                "project_id": "vbot",
                "agent_id": "typo",
                "field": "temperature",
                "value": 0.4,
            },
        )

    assert exc_info.value.code == "invalid_request"
    assert "typo" in exc_info.value.message
    assert "vbot" in exc_info.value.message
    assert state.runtime.projects.get("vbot").overrides == {}


def test_set_override_rejects_unknown_field(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    with pytest.raises(RpcError) as exc_info:
        _set_override(
            state, {"project_id": "vbot", "agent_id": "builder", "field": "nope", "value": "x"}
        )

    assert exc_info.value.code == "invalid_request"
    assert "params.field" in exc_info.value.message


def test_set_override_rejects_bad_temperature(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    with pytest.raises(RpcError) as exc_info:
        _set_override(
            state,
            {"project_id": "vbot", "agent_id": "builder", "field": "temperature", "value": 3.0},
        )

    assert exc_info.value.code == "invalid_request"


def test_set_override_rejects_unusable_model(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    with pytest.raises(RpcError) as exc_info:
        _set_override(
            state,
            {
                "project_id": "vbot",
                "agent_id": "builder",
                "field": "model",
                "value": "openai/ghost-model",
            },
        )

    assert exc_info.value.code == "invalid_request"


def test_set_override_rejects_unsupported_field_param(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    with pytest.raises(RpcError) as exc_info:
        _set_override(
            state,
            {
                "project_id": "vbot",
                "agent_id": "builder",
                "field": "model",
                "value": "openai/gpt-mini",
                "bogus": 1,
            },
        )
    assert exc_info.value.code == "invalid_request"
    assert "bogus" in exc_info.value.message


def test_clear_override_removes_field_and_returns_scan(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})
    state.runtime.projects.set_override("vbot", "builder", "model", "openai/gpt-mini")

    result = _clear_override(state, {"project_id": "vbot", "agent_id": "builder", "field": "model"})

    member = next(m for m in result["scan"]["team"] if m["agent_id"] == "builder")
    assert member["overrides"] is None
    assert state.runtime.projects.get("vbot").overrides == {}


def test_clear_override_absent_entry_is_noop(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    result = _clear_override(state, {"project_id": "vbot", "agent_id": "builder", "field": "model"})

    assert result["project"]["project_id"] == "vbot"


def test_clear_override_rejects_unknown_field(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    with pytest.raises(RpcError) as exc_info:
        _clear_override(state, {"project_id": "vbot", "agent_id": "builder", "field": "nope"})

    assert exc_info.value.code == "invalid_request"


def test_clear_override_rejects_unsupported_field_param(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    with pytest.raises(RpcError) as exc_info:
        _clear_override(
            state, {"project_id": "vbot", "agent_id": "builder", "field": "model", "bogus": 1}
        )
    assert exc_info.value.code == "invalid_request"
    assert "bogus" in exc_info.value.message


def test_clear_override_unknown_project_raises(tmp_path: Path) -> None:
    state = _make_state(tmp_path)

    with pytest.raises(RpcError) as exc_info:
        _clear_override(state, {"project_id": "missing", "agent_id": "builder", "field": "model"})

    assert exc_info.value.code == "project_not_found"


# ---------------------------------------------------------------------------
# Default temperature / thinking effort: add, set, show, validation.
# ---------------------------------------------------------------------------
def test_add_persists_default_temperature_and_thinking(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")

    result = _add_project(
        state,
        {
            "cwd": str(repo),
            "display_name": "vBot",
            "default_temperature": 0.4,
            "default_thinking_effort": "high",
        },
    )

    assert result["project"]["default_temperature"] == 0.4
    assert result["project"]["default_thinking_effort"] == "high"


def test_add_rejects_temperature_out_of_range(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot")

    with pytest.raises(RpcError) as exc_info:
        _add_project(state, {"cwd": str(repo), "display_name": "vBot", "default_temperature": 3.0})

    assert exc_info.value.code == "invalid_request"


def test_set_changes_default_temperature_and_thinking(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    _add_project(state, {"cwd": str(_make_repo(tmp_path, "vbot")), "display_name": "vBot"})

    result = _set_project(
        state,
        {"project_id": "vbot", "default_temperature": 0.2, "default_thinking_effort": "low"},
    )

    assert result["project"]["default_temperature"] == 0.2
    assert result["project"]["default_thinking_effort"] == "low"


def test_set_accepts_empty_thinking_effort_as_provider_default(tmp_path: Path) -> None:
    # "" is a real value (provider default), distinct from null — and _optional_string
    # would reject it, so this also guards against using the wrong helper (D5).
    state = _make_state(tmp_path)
    _add_project(state, {"cwd": str(_make_repo(tmp_path, "vbot")), "display_name": "vBot"})

    result = _set_project(state, {"project_id": "vbot", "default_thinking_effort": ""})

    assert result["project"]["default_thinking_effort"] == ""


def test_set_null_clears_default_thinking_effort(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    _add_project(
        state,
        {
            "cwd": str(_make_repo(tmp_path, "vbot")),
            "display_name": "vBot",
            "default_thinking_effort": "high",
        },
    )

    result = _set_project(state, {"project_id": "vbot", "default_thinking_effort": None})

    assert result["project"]["default_thinking_effort"] is None


def test_set_rejects_unknown_thinking_effort(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    _add_project(state, {"cwd": str(_make_repo(tmp_path, "vbot")), "display_name": "vBot"})

    with pytest.raises(RpcError) as exc_info:
        _set_project(state, {"project_id": "vbot", "default_thinking_effort": "ultra"})

    assert exc_info.value.code == "invalid_request"


def test_set_rejects_temperature_out_of_range(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    _add_project(state, {"cwd": str(_make_repo(tmp_path, "vbot")), "display_name": "vBot"})

    with pytest.raises(RpcError) as exc_info:
        _set_project(state, {"project_id": "vbot", "default_temperature": 3.0})

    assert exc_info.value.code == "invalid_request"


def test_show_includes_default_temperature_and_thinking(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    _add_project(
        state,
        {
            "cwd": str(_make_repo(tmp_path, "vbot")),
            "display_name": "vBot",
            "default_temperature": 0.7,
            "default_thinking_effort": "medium",
        },
    )

    result = _show_project(state, {"project_id": "vbot"})

    assert result["project"]["default_temperature"] == 0.7
    assert result["project"]["default_thinking_effort"] == "medium"


def test_team_member_response_includes_thinking_effort(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot")
    _write_agent(repo, "thinker.md", reasoning_effort="high")

    result = _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    member = result["scan"]["team"][0]
    assert member["agent_id"] == "thinker"
    assert member["thinking_effort"] == "high"
