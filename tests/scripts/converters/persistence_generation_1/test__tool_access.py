"""Tests for replacing retired Tool names in persisted Tool access."""

from __future__ import annotations

from typing import Any

import pytest

from scripts.converters.persistence_generation_1._tool_access import (
    convert_policy,
    convert_whitelist,
)


def _narrowed(notes: list[str]) -> bool:
    return any("explicitly if wanted" in note for note in notes)


@pytest.mark.parametrize(
    ("policy", "converted", "narrowed"),
    [
        # grep and glob -> search_files, which needs both.
        (
            {"mode": "selected", "allowed": ["read", "grep", "glob"]},
            {"mode": "selected", "allowed": ["read", "search_files"]},
            False,
        ),
        (
            {"mode": "selected", "allowed": ["glob", "read", "grep"]},
            {"mode": "selected", "allowed": ["search_files", "read"]},
            False,
        ),
        (
            {"mode": "selected", "allowed": ["read", "grep"]},
            {"mode": "selected", "allowed": ["read"]},
            True,
        ),
        (
            {"mode": "all", "denied": ["grep", "glob"]},
            {"mode": "all", "denied": ["search_files"]},
            False,
        ),
        ({"mode": "all", "denied": ["glob"]}, {"mode": "all", "denied": ["search_files"]}, True),
        # write and edit -> apply_patch, which needs write.
        (
            {"mode": "selected", "allowed": ["read", "write", "edit", "bash"]},
            {"mode": "selected", "allowed": ["read", "apply_patch", "bash"]},
            False,
        ),
        (
            {"mode": "selected", "allowed": ["write", "read"]},
            {"mode": "selected", "allowed": ["apply_patch", "read"]},
            False,
        ),
        (
            {"mode": "selected", "allowed": ["edit", "read"]},
            {"mode": "selected", "allowed": ["read"]},
            True,
        ),
        # Denying edit alone left full-file writes available: apply_patch stays.
        ({"mode": "all", "denied": ["cron", "edit"]}, {"mode": "all", "denied": ["cron"]}, False),
        ({"mode": "all", "denied": ["write"]}, {"mode": "all", "denied": ["apply_patch"]}, True),
        (
            {"mode": "all", "denied": ["edit", "write"]},
            {"mode": "all", "denied": ["apply_patch"]},
            False,
        ),
        (
            {"mode": "selected", "allowed": ["read"], "denied": ["bash", "edit", "write"]},
            {"mode": "selected", "allowed": ["read"], "denied": ["bash", "apply_patch"]},
            False,
        ),
        # An explicitly allowed successor is never denied because of a retired name.
        (
            {"mode": "selected", "allowed": ["apply_patch"], "denied": ["edit"]},
            {"mode": "selected", "allowed": ["apply_patch"]},
            False,
        ),
        # An explicitly denied successor is never granted because of a retired name.
        (
            {"mode": "selected", "allowed": ["write", "edit"], "denied": ["apply_patch"]},
            {"mode": "selected", "allowed": [], "denied": ["apply_patch"]},
            False,
        ),
        # terminal_beta -> terminal, read2 and read_new -> read.
        (
            {"mode": "selected", "allowed": ["terminal_beta", "bash"]},
            {"mode": "selected", "allowed": ["terminal", "bash"]},
            False,
        ),
        (
            {"mode": "all", "denied": ["terminal_beta"]},
            {"mode": "all", "denied": ["terminal"]},
            False,
        ),
        (
            {"mode": "selected", "allowed": ["read2", "bash"]},
            {"mode": "selected", "allowed": ["read", "bash"]},
            False,
        ),
        (
            {"mode": "selected", "allowed": ["read_new", "read"]},
            {"mode": "selected", "allowed": ["read"]},
            False,
        ),
        # Retired names without a successor are dropped and grant nothing else.
        (
            {"mode": "selected", "allowed": ["subagent", "subagent_result"]},
            {"mode": "selected", "allowed": ["subagent"]},
            False,
        ),
        (
            {"mode": "selected", "allowed": ["subagent_result"]},
            {"mode": "selected", "allowed": []},
            False,
        ),
        (
            {
                "mode": "all",
                "denied": ["browser", "skill_list", "session_read", "swarm_decisions"],
                "granted": ["computer"],
            },
            {"mode": "all", "granted": ["computer"]},
            False,
        ),
        # Grants and mode none carry no retired access over.
        ({"mode": "all", "granted": ["grep"]}, {"mode": "all"}, False),
        ({"mode": "none", "denied": ["grep"]}, {"mode": "none", "denied": ["search_files"]}, False),
    ],
)
def test_retired_names_in_a_policy_are_replaced_without_widening(
    policy: dict[str, Any], converted: dict[str, Any], narrowed: bool
) -> None:
    result, notes = convert_policy(policy)

    assert result == converted
    assert notes
    assert _narrowed(notes) == narrowed


def test_every_retired_group_gets_one_note_in_order_of_appearance() -> None:
    policy = {
        "mode": "selected",
        "allowed": ["edit", "subagent_result", "grep", "write", "glob", "terminal_beta"],
        "denied": ["browser"],
    }

    result, notes = convert_policy(policy)

    assert result == {
        "mode": "selected",
        "allowed": ["apply_patch", "search_files", "terminal"],
    }
    assert [note.split(" ")[0] for note in notes] == [
        "edit",
        "subagent_result",
        "grep",
        "terminal_beta",
        "browser",
    ]


@pytest.mark.parametrize(
    "policy",
    [
        {"mode": "selected", "allowed": ["read", "search_files"]},
        {"mode": "all", "denied": ["word_count"]},
        {"mode": "selected", "allowed": ["grep", "grep"]},
        {"mode": "sometimes"},
        ["grep"],
    ],
)
def test_policies_without_retired_names_or_invalid_ones_are_returned_unchanged(
    policy: Any,
) -> None:
    assert convert_policy(policy) == (policy, [])


@pytest.mark.parametrize(
    "policy",
    [
        {"mode": "selected", "allowed": ["read", "write", "edit", "grep", "glob"]},
        {"mode": "all", "denied": ["write", "glob", "terminal_beta", "browser"]},
        {"mode": "selected", "allowed": ["edit"], "denied": ["grep"]},
    ],
)
def test_converting_a_converted_policy_changes_nothing(policy: dict[str, Any]) -> None:
    converted, _notes = convert_policy(policy)

    assert convert_policy(converted) == (converted, [])


@pytest.mark.parametrize(
    ("whitelist", "converted", "narrowed"),
    [
        (
            ["read", "write", "edit", "bash", "terminal_beta", "subagent", "subagent_result"],
            ["read", "apply_patch", "bash", "terminal", "subagent"],
            False,
        ),
        (
            ["read", "write", "apply_patch", "search_files"],
            ["read", "apply_patch", "search_files"],
            False,
        ),
        (["read", "edit", "glob", "grep"], ["read", "search_files"], True),
        (["read", "grep", {"broken": True}], ["read", {"broken": True}], True),
    ],
)
def test_retired_names_in_a_project_whitelist_are_replaced_without_widening(
    whitelist: list[Any], converted: list[Any], narrowed: bool
) -> None:
    result, notes = convert_whitelist(whitelist)

    assert result == converted
    assert _narrowed(notes) == narrowed
    assert convert_whitelist(result) == (result, [])


def test_a_whitelist_without_retired_names_is_returned_unchanged() -> None:
    whitelist = ["read", "apply_patch", "word_count"]

    assert convert_whitelist(whitelist) == (whitelist, [])
