"""Tools: Agent Tool Access Policy, its resolution over the catalog, and Tool settings."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from core.tools import Tool, ToolRegistry
from core.tools.availability import (
    ToolAccess,
    apply_agent_target_tool_visibility,
    bash_allowed_env_keys,
    normalize_tool_access,
    resolve_tool_access,
    subagent_allowed_agents,
)
from core.tools.subagent import SUBAGENT_TOOL_PARAMETERS
from tests.core.tools.tools_test_support import read_file_handler

_IDENTITY = {"constraints": ("identity_agent",)}


def _catalog(*tools: tuple[str, dict[str, Any]]) -> list[Tool]:
    """Register each ``(name, declaration)`` and list the catalog as Chat resolves it."""
    registry = ToolRegistry()
    for name, declaration in tools:
        registry.register(
            name, f"The {name} Tool.", {"type": "object"}, read_file_handler, **declaration
        )
    return registry.list_tools()


# --- Policy normalization ---------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "policy", "stored"),
    [
        pytest.param(None, ToolAccess(mode="all"), {"mode": "all"}, id="missing-means-all"),
        pytest.param(
            {"mode": "selected", "allowed": []},
            ToolAccess(mode="selected"),
            {"mode": "selected", "allowed": []},
            id="selected-nothing",
        ),
        pytest.param(
            {"mode": "none", "denied": ["memory"]},
            ToolAccess(mode="none", denied=("memory",)),
            {"mode": "none", "denied": ["memory"]},
            id="denials-kept-in-none",
        ),
        *(
            pytest.param(
                {"mode": mode, **allowed, "granted": ["offline_extension"]},
                ToolAccess(
                    mode=mode,
                    allowed=tuple(allowed.get("allowed", ())),
                    granted=("offline_extension",),
                ),
                {"mode": mode, **allowed, "granted": ["offline_extension"]},
                id=f"grant-of-an-unavailable-tool-in-{mode}",
            )
            for mode, allowed in (
                ("all", {}),
                ("selected", {"allowed": ["offline_extension"]}),
                ("none", {}),
            )
        ),
    ],
)
def test_policy_normalizes_to_explicit_modes_and_round_trips(
    value: dict[str, Any] | None, policy: ToolAccess, stored: dict[str, Any]
) -> None:
    assert normalize_tool_access(value) == policy
    assert policy.to_dict() == stored
    assert normalize_tool_access(policy) == policy


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (["read"], "tool_access must be an object"),
        ({"mode": "all", "extra": 1}, "unsupported tool_access fields: extra"),
        ({"mode": "some"}, "tool_access.mode must be one of: all, selected, none"),
        ({"mode": "selected"}, "allowed is required"),
        ({"mode": "all", "allowed": []}, "only valid when mode is selected"),
        ({"mode": "selected", "allowed": ["read"], "denied": ["read"]}, "overlap"),
        ({"mode": "all", "denied": ["*"]}, "retired wildcard"),
        ({"mode": "selected", "allowed": ["   "]}, "empty names"),
        ({"mode": "all", "granted": "computer"}, "granted must be a list of strings"),
        ({"mode": "all", "granted": [False]}, "granted must be a list of strings"),
        ({"mode": "all", "granted": ["*"]}, "retired wildcard"),
        ({"mode": "all", "granted": ["computer", "computer"]}, "duplicate names"),
    ],
)
def test_ambiguous_policies_are_rejected(value: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        normalize_tool_access(value)


# --- Resolution -------------------------------------------------------------------


def test_all_selected_and_none_resolve_direct_tools_explicitly() -> None:
    tools = _catalog(("read", {}), ("write", {}))

    assert resolve_tool_access(ToolAccess(mode="all"), tools, "off").allowed_tools == (
        "read",
        "write",
    )
    assert resolve_tool_access(
        ToolAccess(mode="selected", allowed=("write",)), tools, "off"
    ).allowed_tools == ("write",)
    assert resolve_tool_access(ToolAccess(mode="none"), tools, "agent_user").allowed_tools == ()


def test_identity_constraint_is_enforced_for_all_and_selected_modes() -> None:
    tools = _catalog(("read", {}), ("project", _IDENTITY))

    identity = resolve_tool_access(ToolAccess(), tools, "off", workspace="workspace")
    project_all = resolve_tool_access(ToolAccess(), tools, "off", workspace="")
    project_selected = resolve_tool_access(
        ToolAccess(mode="selected", allowed=("project",)), tools, "off", workspace=""
    )

    assert identity.allowed_tools == ("project", "read")
    assert project_all.allowed_tools == ("read",)
    assert project_selected.allowed_tools == ()


def test_followed_tool_requires_its_source_and_can_be_denied_independently() -> None:
    tools = _catalog(
        ("session_search", {}),
        ("session_read", {"activation": "follows", "activation_source": "session_search"}),
    )

    def resolve(*denied: str) -> tuple[str, ...]:
        policy = ToolAccess(mode="selected", allowed=("session_search",), denied=denied)
        return resolve_tool_access(policy, tools, "off").allowed_tools

    assert resolve() == ("session_read", "session_search")
    assert resolve("session_read") == ("session_search",)
    assert resolve("session_search") == ()


def test_denials_win_over_memory_activation_and_session_grants() -> None:
    tools = _catalog(
        ("memory", {"activation": "memory_mode", **_IDENTITY}),
        ("history", {"activation": "session_grant"}),
    )

    resolution = resolve_tool_access(
        ToolAccess(mode="selected", allowed=(), denied=("memory", "history")),
        tools,
        "agent_user",
        workspace="workspace",
        session_tool_grants=("history",),
    )

    assert resolution.allowed_tools == ()
    assert resolution.session_tool_grants == ()


def test_a_fixed_selection_is_the_whole_tool_set() -> None:
    # A built-in Agent's Tools: nothing activates by memory mode, Session grant or follow.
    tools = _catalog(
        ("skill", {}),
        ("skill_manage", {**_IDENTITY}),
        ("skill_audit", {"activation": "follows", "activation_source": "skill"}),
        ("memory", {"activation": "memory_mode", **_IDENTITY}),
        ("history", {"activation": "session_grant"}),
    )
    allowed = ("skill", "skill_manage")

    def resolve(*, fixed: bool) -> tuple[tuple[str, ...], tuple[str, ...]]:
        resolution = resolve_tool_access(
            ToolAccess(mode="selected", allowed=allowed, fixed=fixed),
            tools,
            "agent_user",
            workspace="workspace",
            session_tool_grants=("history",),
        )
        return resolution.allowed_tools, resolution.session_tool_grants

    assert resolve(fixed=True) == (allowed, ())
    assert resolve(fixed=False) == (
        ("history", "memory", "skill", "skill_audit", "skill_manage"),
        ("history",),
    )
    # The mark is never persisted.
    assert ToolAccess(mode="selected", allowed=allowed, fixed=True).to_dict() == {
        "mode": "selected",
        "allowed": list(allowed),
    }


def test_memory_activation_is_independent_of_selected_direct_tools() -> None:
    tools = _catalog(("read", {}), ("memory", {"activation": "memory_mode", **_IDENTITY}))
    policy = ToolAccess(mode="selected")

    active = resolve_tool_access(policy, tools, "agent_user", workspace="workspace")
    off = resolve_tool_access(policy, tools, "off", workspace="workspace")

    assert active.allowed_tools == ("memory",)
    assert off.allowed_tools == ()


@pytest.mark.parametrize(
    ("mode", "granted", "denied", "active"),
    [
        pytest.param("all", True, False, True, id="granted-in-all"),
        pytest.param("selected", True, False, True, id="granted-and-selected"),
        pytest.param("all", False, False, False, id="all-mode-is-no-grant"),
        pytest.param("selected", False, False, False, id="selection-is-no-grant"),
        pytest.param("all", True, True, False, id="denial-wins"),
        pytest.param("none", True, False, False, id="none-wins"),
    ],
)
def test_opt_in_tool_needs_an_explicit_grant(
    mode: str, granted: bool, denied: bool, active: bool
) -> None:
    tools = _catalog(
        ("computer", {"requires_opt_in": True}),
        ("computer_read", {"activation": "follows", "activation_source": "computer"}),
    )
    policy = ToolAccess(
        mode=mode,
        allowed=("computer",) if mode == "selected" else (),
        granted=("computer",) if granted else (),
        denied=("computer",) if denied else (),
    )

    allowed = resolve_tool_access(policy, tools, "off").allowed_tools

    assert allowed == (("computer", "computer_read") if active else ())


# --- Tool settings ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("settings", "targets"),
    [
        ({}, ["*"]),
        ({"subagent": {"allowed_agents": ["worker", "builder@vbot"]}}, ["worker", "builder@vbot"]),
    ],
)
def test_subagent_settings_name_the_allowed_targets(
    settings: dict[str, Any], targets: list[str]
) -> None:
    assert subagent_allowed_agents(settings) == targets


@pytest.mark.parametrize(
    ("allowed_env", "keys"),
    [
        (["OPENAI_API_KEY", "OPENAI_API_KEY", "HA_TOKEN"], ["OPENAI_API_KEY", "HA_TOKEN"]),
        (["bad-key"], []),
    ],
)
def test_bash_env_settings_return_ordered_unique_grants_or_fail_closed(
    allowed_env: list[str], keys: list[str]
) -> None:
    assert bash_allowed_env_keys({"bash": {"allowed_env": allowed_env}}) == keys


def _subagent_definitions() -> list[dict[str, Any]]:
    return [
        {"name": "read", "description": "Read", "parameters": {"type": "object"}},
        {
            "name": "subagent",
            "description": "Start a Sub-Agent.",
            "parameters": copy.deepcopy(SUBAGENT_TOOL_PARAMETERS),
        },
    ]


@pytest.mark.parametrize(
    ("allowed_agents", "targets"),
    [
        pytest.param([], ["orchestrator"], id="self-delegation-only"),
        pytest.param(
            ["worker", "reviewer@vbot", "worker"],
            ["orchestrator", "worker", "reviewer@vbot"],
            id="explicit-targets",
        ),
    ],
)
def test_agent_targets_narrow_the_subagent_schema_without_mutating_the_source(
    allowed_agents: list[str], targets: list[str]
) -> None:
    source = _subagent_definitions()

    definitions = apply_agent_target_tool_visibility(
        source, agent_id="orchestrator", allowed_agents=allowed_agents
    )

    assert definitions[0] == source[0]
    parameters = definitions[1]["parameters"]
    assert parameters["properties"]["agent_id"]["enum"] == targets
    assert parameters["required"] == SUBAGENT_TOOL_PARAMETERS["required"]
    assert source == _subagent_definitions()


def test_wildcard_agent_targets_leave_tool_definitions_unchanged() -> None:
    source = _subagent_definitions()

    assert (
        apply_agent_target_tool_visibility(source, agent_id="orchestrator", allowed_agents=["*"])
        is source
    )
