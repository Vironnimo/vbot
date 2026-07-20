"""Tests for Agent-derived Tool visibility and dispatch allowlists."""

from __future__ import annotations

from core.tools.availability import (
    apply_agent_target_tool_visibility,
    effective_agent_allowed_tools,
    subagent_allowed_agents,
)


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
        {
            "name": "subagent_result",
            "description": "Read a Sub-Agent result.",
            "parameters": target_parameters,
        },
    ]


def test_empty_additional_targets_keep_self_delegation_in_both_schemas() -> None:
    definitions = apply_agent_target_tool_visibility(
        _definitions(), agent_id="orchestrator", allowed_agents=[]
    )

    assert [definition["name"] for definition in definitions] == [
        "read",
        "subagent",
        "subagent_result",
    ]
    for definition in definitions[1:]:
        parameters = definition["parameters"]
        assert isinstance(parameters, dict)
        properties = parameters["properties"]
        assert isinstance(properties, dict)
        assert properties["agent_id"]["enum"] == ["orchestrator"]
        assert "required" not in parameters


def test_explicit_agent_targets_narrow_both_tool_schemas_without_mutating_source() -> None:
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


def test_empty_additional_targets_do_not_remove_subagent_tools_from_dispatch() -> None:
    allowed = effective_agent_allowed_tools(
        ["*"],
        "agent_user",
        registered_tool_names=["read", "subagent", "subagent_result"],
        workspace="workspace",
    )

    assert allowed == ["*"]


def test_missing_subagent_tool_settings_defaults_to_wildcard() -> None:
    assert subagent_allowed_agents({}) == ["*"]


def test_nested_subagent_tool_settings_expose_explicit_targets() -> None:
    assert subagent_allowed_agents(
        {"subagent": {"allowed_agents": ["worker", "builder@vbot"]}}
    ) == ["worker", "builder@vbot"]
