"""Tests for Agent-derived Tool visibility and dispatch allowlists."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.tools.availability import (
    PROJECT_TOOL_NAME,
    TOOL_ACCESS_MODE_NONE,
    ToolAccess,
    apply_agent_target_tool_visibility,
    bash_allowed_env_keys,
    normalize_tool_access,
    resolve_tool_access,
    subagent_allowed_agents,
)
from core.tools.subagent import SUBAGENT_TOOL_PARAMETERS


def _definitions() -> list[dict[str, object]]:
    target_parameters = {
        "type": "object",
        "properties": {"agent_id": {"type": "string"}},
    }
    return [
        {"name": "read", "description": "Read", "parameters": {"type": "object"}},
        {
            "name": "subagent",
            "description": "Start a Sub-Agent.",
            "parameters": target_parameters,
        },
    ]


def test_empty_additional_targets_keep_self_delegation_in_subagent_schema() -> None:
    definitions = apply_agent_target_tool_visibility(
        _definitions(), agent_id="orchestrator", allowed_agents=[]
    )

    assert [definition["name"] for definition in definitions] == ["read", "subagent"]
    for definition in definitions[1:]:
        parameters = definition["parameters"]
        assert isinstance(parameters, dict)
        properties = parameters["properties"]
        assert isinstance(properties, dict)
        assert properties["agent_id"]["enum"] == ["orchestrator"]
        assert "required" not in parameters


def test_explicit_agent_targets_narrow_subagent_schema_without_mutating_source() -> None:
    source = _definitions()

    definitions = apply_agent_target_tool_visibility(
        source,
        agent_id="orchestrator",
        allowed_agents=["worker", "reviewer@vbot", "worker"],
    )

    for definition in definitions[1:]:
        parameters = definition["parameters"]
        assert isinstance(parameters, dict)
        properties = parameters["properties"]
        assert isinstance(properties, dict)
        agent_id = properties["agent_id"]
        assert isinstance(agent_id, dict)
        assert agent_id["enum"] == ["orchestrator", "worker", "reviewer@vbot"]
        assert "required" not in parameters
    source_parameters = source[1]["parameters"]
    assert isinstance(source_parameters, dict)
    source_properties = source_parameters["properties"]
    assert isinstance(source_properties, dict)
    assert "enum" not in source_properties["agent_id"]


def test_wildcard_agent_targets_leave_tool_definitions_unchanged() -> None:
    source = _definitions()

    assert (
        apply_agent_target_tool_visibility(source, agent_id="orchestrator", allowed_agents=["*"])
        is source
    )


def test_explicit_agent_targets_narrow_flat_subagent_run_target() -> None:
    source = [
        {
            "name": "subagent",
            "description": "Start a Sub-Agent.",
            "parameters": SUBAGENT_TOOL_PARAMETERS,
        }
    ]

    definitions = apply_agent_target_tool_visibility(
        source,
        agent_id="orchestrator",
        allowed_agents=["worker"],
    )

    parameters = definitions[0]["parameters"]
    properties = parameters["properties"]
    assert properties["agent_id"]["enum"] == [
        "orchestrator",
        "worker",
    ]
    assert "enum" not in SUBAGENT_TOOL_PARAMETERS["properties"]["agent_id"]


def _tool(
    name: str,
    *,
    activation: str = "configurable",
    activation_source: str | None = None,
    constraints: tuple[str, ...] = (),
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        activation=activation,
        activation_source=activation_source,
        constraints=constraints,
        internal=False,
    )


def test_normalize_tool_access_replaces_wildcard_with_explicit_modes() -> None:
    assert normalize_tool_access(None) == ToolAccess(mode="all")
    assert normalize_tool_access({"mode": "selected", "allowed": []}) == ToolAccess(mode="selected")
    assert normalize_tool_access({"mode": "none", "denied": ["memory"]}) == ToolAccess(
        mode="none", denied=("memory",)
    )


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"mode": "selected"}, "allowed is required"),
        ({"mode": "all", "allowed": []}, "only valid when mode is selected"),
        ({"mode": "selected", "allowed": ["read"], "denied": ["read"]}, "overlap"),
        ({"mode": "all", "denied": ["*"]}, "retired wildcard"),
        ({"mode": "selected", "allowed": ["   "]}, "empty names"),
    ],
)
def test_normalize_tool_access_rejects_ambiguous_policies(
    value: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        normalize_tool_access(value)


def test_all_selected_and_none_resolve_direct_tools_explicitly() -> None:
    tools = [_tool("read"), _tool("write")]

    assert resolve_tool_access(ToolAccess(mode="all"), tools, "off").allowed_tools == (
        "read",
        "write",
    )
    assert resolve_tool_access(
        ToolAccess(mode="selected", allowed=("write",)), tools, "off"
    ).allowed_tools == ("write",)
    assert (
        resolve_tool_access(
            ToolAccess(mode=TOOL_ACCESS_MODE_NONE), tools, "agent_user"
        ).allowed_tools
        == ()
    )


def test_missing_subagent_tool_settings_defaults_to_wildcard() -> None:
    assert subagent_allowed_agents({}) == ["*"]


def test_bash_env_settings_return_ordered_unique_grants() -> None:
    assert bash_allowed_env_keys(
        {"bash": {"allowed_env": ["OPENAI_API_KEY", "OPENAI_API_KEY", "HA_TOKEN"]}}
    ) == ["OPENAI_API_KEY", "HA_TOKEN"]


def test_invalid_bash_env_settings_fail_closed() -> None:
    assert bash_allowed_env_keys({"bash": {"allowed_env": ["bad-key"]}}) == []


def test_identity_constraint_is_enforced_for_all_and_selected_modes() -> None:
    tools = [
        _tool("read"),
        _tool(PROJECT_TOOL_NAME, constraints=("identity_agent",)),
    ]

    identity = resolve_tool_access(ToolAccess(), tools, "off", workspace="workspace")
    project_all = resolve_tool_access(ToolAccess(), tools, "off", workspace="")
    project_selected = resolve_tool_access(
        ToolAccess(mode="selected", allowed=(PROJECT_TOOL_NAME,)),
        tools,
        "off",
        workspace="",
    )

    assert identity.allowed_tools == ("read", "project")
    assert project_all.allowed_tools == ("read",)
    assert project_selected.allowed_tools == ()


def test_followed_tool_requires_its_source_and_can_be_denied_independently() -> None:
    tools = [
        _tool("session_search"),
        _tool("session_read", activation="follows", activation_source="session_search"),
    ]

    active = resolve_tool_access(
        ToolAccess(mode="selected", allowed=("session_search",)), tools, "off"
    )
    denied_follower = resolve_tool_access(
        ToolAccess(
            mode="selected",
            allowed=("session_search",),
            denied=("session_read",),
        ),
        tools,
        "off",
    )
    denied_source = resolve_tool_access(
        ToolAccess(
            mode="selected",
            allowed=("session_search",),
            denied=("session_search",),
        ),
        tools,
        "off",
    )

    assert active.allowed_tools == ("session_search", "session_read")
    assert denied_follower.allowed_tools == ("session_search",)
    assert denied_source.allowed_tools == ()


def test_denials_win_over_memory_activation_and_session_grants() -> None:
    tools = [
        _tool("memory", activation="memory_mode", constraints=("identity_agent",)),
        _tool("history", activation="session_grant"),
    ]
    policy = ToolAccess(mode="selected", denied=("memory", "history"))

    resolution = resolve_tool_access(
        policy,
        tools,
        "agent_user",
        workspace="workspace",
        session_tool_grants=("history",),
    )

    assert resolution.allowed_tools == ()
    assert resolution.session_tool_grants == ()


def test_memory_activation_is_independent_of_selected_direct_tools() -> None:
    tools = [
        _tool("read"),
        _tool("memory", activation="memory_mode", constraints=("identity_agent",)),
    ]

    active = resolve_tool_access(
        ToolAccess(mode="selected"), tools, "agent_user", workspace="workspace"
    )
    off = resolve_tool_access(ToolAccess(mode="selected"), tools, "off", workspace="workspace")

    assert active.allowed_tools == ("memory",)
    assert off.allowed_tools == ()


def test_nested_subagent_tool_settings_expose_explicit_targets() -> None:
    assert subagent_allowed_agents(
        {"subagent": {"allowed_agents": ["worker", "builder@vbot"]}}
    ) == ["worker", "builder@vbot"]
