"""Project Team payloads and per-Agent Overrides (project.set_override / clear_override)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.server.rpc.project_methods_test_support import _make_state, _write_agent
from tests.server.rpc_test_support import JsonObject, resource_changes, rpc_error, rpc_result

OPENCODE_AGENTS_SUBPATH = (".opencode", "agents")


def _member(result: JsonObject, agent_id: str = "builder") -> JsonObject:
    member: JsonObject = next(m for m in result["scan"]["team"] if m["agent_id"] == agent_id)
    return member


async def _vbot_state(tmp_path: Path, **agent_fields: Any) -> SimpleNamespace:
    state = _make_state(tmp_path)
    repo = tmp_path / "repos" / "vbot"
    repo.mkdir(parents=True)
    _write_agent(repo, "builder.md", **agent_fields)
    await rpc_result(state, "project.add", cwd=str(repo), display_name="vBot")
    return state


# ---------------------------------------------------------------------------
# Team member payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_team_member_reports_repo_values_denials_and_no_overrides(tmp_path: Path) -> None:
    # An OpenCode agent denying task and edit: the member surfaces the vBot tools
    # that turns off, so the editor can show it uses less than the ceiling.
    state = await _vbot_state(
        tmp_path, reasoning_effort="high", permission={"task": "deny", "edit": "deny"}
    )

    result = await rpc_result(state, "project.show", project_id="vbot")

    member = _member(result)
    assert member["thinking_effort"] == "high"
    assert member["denied_tools"] == ["apply_patch", "subagent"]
    assert member["overrides"] is None
    # The effective block reports the model resolved from the repo (agent tier).
    assert member["effective"]["model"] == {"value": "openai/gpt-5.2", "source": "agent"}


@pytest.mark.asyncio
async def test_team_member_reports_effective_repo_owned_agent_targets(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = tmp_path / "repos" / "vbot"
    agents_dir = repo.joinpath(*OPENCODE_AGENTS_SUBPATH)
    agents_dir.mkdir(parents=True)
    for name in ("builder", "reviewer"):
        _write_agent(repo, f"{name}.md")
    agents_dir.joinpath("orchestrator.md").write_text(
        (
            "---\nmodel: openai/gpt-5.2\npermission:\n  task:\n"
            '    "*": deny\n    reviewer: allow\n---\nBody.\n'
        ),
        encoding="utf-8",
    )

    result = await rpc_result(state, "project.add", cwd=str(repo), display_name="vBot")

    assert _member(result, "orchestrator")["tools"] == {
        "subagent": {"allowed_agents": ["reviewer"]}
    }
    assert _member(result, "builder")["tools"] == {
        "subagent": {"allowed_agents": ["orchestrator", "reviewer"]}
    }


# ---------------------------------------------------------------------------
# project.set_override / project.clear_override
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model", "openai/gpt-mini"),
        ("temperature", 0.4),
        ("thinking_effort", "high"),
        ("project_memory_enabled", False),
    ],
)
async def test_set_override_is_the_winning_tier_until_cleared(
    tmp_path: Path, field: str, value: Any
) -> None:
    state = await _vbot_state(tmp_path)
    override = {"project_id": "vbot", "agent_id": "builder", "field": field}

    overridden = await rpc_result(state, "project.set_override", **override, value=value)
    stored = state.runtime.projects.get("vbot").overrides
    shown = await rpc_result(state, "project.show", project_id="vbot")
    cleared = await rpc_result(state, "project.clear_override", **override)

    assert stored == {"builder": {field: value}}
    assert _member(overridden)["overrides"] == {field: value}
    assert _member(shown)["effective"][field] == {"value": value, "source": "override"}
    assert _member(cleared)["overrides"] is None
    assert state.runtime.projects.get("vbot").overrides == {}
    # Setting and clearing each change the member's effective configuration.
    assert len(resource_changes(state, "agents")) == 2


@pytest.mark.asyncio
async def test_a_permission_rules_override_is_saved_normalized_over_what_it_inherits(
    tmp_path: Path,
) -> None:
    state = await _vbot_state(tmp_path)
    override = {"project_id": "vbot", "agent_id": "builder", "field": "permission_rules"}
    value = {"write": "scratch", "allow": ["Write(docs/**)"]}
    before = len(resource_changes(state))

    refused = await rpc_error(
        state, "project.set_override", **override, value={"allow": ["Copy(x)"]}
    )
    overridden = await rpc_result(state, "project.set_override", **override, value=value)
    invalidated = [change["kind"] for change in resource_changes(state)[before:]]
    # Saving the same rules again changes nothing and invalidates nothing.
    await rpc_result(state, "project.set_override", **override, value=value)

    assert "permission_rules.allow[0]" in refused["message"]
    # The override changes the member's effective rules, so accessors showing
    # the Agent (the Chat Restricted badge) read them again.
    assert invalidated == ["projects", "agents"]
    assert len(resource_changes(state)) == before + 2
    member = _member(overridden)
    saved = {"write": "scratch", "allow": ["Edit(docs/**)"]}
    assert member["overrides"] == {"permission_rules": saved}
    # Neither the repository nor the shared default restricts this agent.
    assert member["permission_rules_default"] is None
    assert member["effective"]["permission_rules"]["value"] == saved


@pytest.mark.asyncio
async def test_tool_access_override_replaces_the_repository_policy_until_cleared(
    tmp_path: Path,
) -> None:
    state = await _vbot_state(tmp_path, permission={"task": "deny"})
    override = {"project_id": "vbot", "agent_id": "builder", "field": "tool_access"}
    policy = {"mode": "selected", "allowed": ["subagent"]}

    overridden = _member(await rpc_result(state, "project.set_override", **override, value=policy))
    cleared = _member(await rpc_result(state, "project.clear_override", **override))

    assert overridden["denied_tools"] == ["subagent"]
    assert overridden["overrides"] == {"tool_access": policy}
    assert overridden["effective"]["tool_access"] == {"value": policy, "source": "override"}
    assert overridden["tools"] == {"subagent": {"allowed_agents": []}}
    assert cleared["overrides"] is None
    assert cleared["effective"]["tool_access"]["source"] == "agent"
    assert cleared["tools"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        pytest.param({"on_demand": False, "always_loaded": ["read"]}, id="complete"),
        pytest.param({"on_demand": False}, id="inherit-list"),
        pytest.param({"always_loaded": ["read"]}, id="inherit-switch"),
    ],
)
async def test_tool_loading_override_is_stored_as_given_until_cleared(
    tmp_path: Path, value: JsonObject
) -> None:
    state = await _vbot_state(tmp_path)
    override = {"project_id": "vbot", "agent_id": "builder", "field": "tool_loading"}

    overridden = _member(await rpc_result(state, "project.set_override", **override, value=value))
    stored = state.runtime.projects.get("vbot").overrides
    cleared = _member(await rpc_result(state, "project.clear_override", **override))

    assert overridden["overrides"] == {"tool_loading": value}
    assert stored == {"builder": {"tool_loading": value}}
    assert state.runtime.projects.get("vbot").overrides == {}
    assert cleared["overrides"] is None


@pytest.mark.asyncio
async def test_clearing_an_absent_override_is_a_no_op(tmp_path: Path) -> None:
    state = await _vbot_state(tmp_path)

    result = await rpc_result(
        state, "project.clear_override", project_id="vbot", agent_id="builder", field="model"
    )

    assert result["project"]["project_id"] == "vbot"
    assert state.runtime.projects.get("vbot").overrides == {}
    assert resource_changes(state, "agents") == []


_OVERRIDE = {"project_id": "vbot", "agent_id": "builder"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "code", "named"),
    [
        (
            "project.set_override",
            {"project_id": "vbot", "agent_id": "typo", "field": "temperature", "value": 0.4},
            "invalid_request",
            "agent 'typo' is not on project 'vbot' team",
        ),
        (
            "project.set_override",
            {**_OVERRIDE, "field": "nope", "value": "x"},
            "invalid_request",
            "params.field",
        ),
        (
            "project.set_override",
            {**_OVERRIDE, "field": "temperature", "value": 3.0},
            "invalid_request",
            "",
        ),
        (
            "project.set_override",
            {**_OVERRIDE, "field": "model", "value": "openai/ghost-model"},
            "invalid_request",
            "",
        ),
        (
            "project.set_override",
            {**_OVERRIDE, "field": "model", "value": "openai/gpt-mini", "bogus": 1},
            "invalid_request",
            "bogus",
        ),
        *(
            (
                "project.set_override",
                {**_OVERRIDE, "field": "tool_access", "value": value},
                "invalid_request",
                "",
            )
            for value in (
                {"mode": "selected", "allowed": ["read"], "denied": ["read"]},
                {"mode": "selected", "allowed": ["missing_tool"]},
                {"mode": "all", "granted": ["computer"]},
            )
        ),
        (
            "project.set_override",
            {**_OVERRIDE, "field": "tool_loading", "value": {"on_demand": "yes"}},
            "invalid_request",
            "",
        ),
        ("project.clear_override", {**_OVERRIDE, "field": "nope"}, "invalid_request", ""),
        (
            "project.clear_override",
            {**_OVERRIDE, "field": "model", "bogus": 1},
            "invalid_request",
            "bogus",
        ),
        (
            "project.clear_override",
            {"project_id": "missing", "agent_id": "builder", "field": "model"},
            "project_not_found",
            "",
        ),
    ],
)
async def test_a_refused_override_request_stores_nothing(
    tmp_path: Path, method: str, params: JsonObject, code: str, named: str
) -> None:
    state = await _vbot_state(tmp_path)

    error = await rpc_error(state, method, **params)

    assert error["code"] == code
    assert named in error["message"]
    assert state.runtime.projects.get("vbot").overrides == {}
