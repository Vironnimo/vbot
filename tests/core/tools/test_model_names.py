"""Tool names the Model sees and names Models call instead."""

from __future__ import annotations

import os
from collections.abc import Collection

import pytest

from core.tools import BASH_TOOL_NAME, called_tool_name, model_tool_name, registry_tool_name

OFFERED = (
    BASH_TOOL_NAME,
    "read",
    "apply_patch",
    "search_files",
    "web_fetch",
    "web_search",
    "subagent",
    "analyze_image",
    "generate_speech",
    "skill",
    "background_tasks",
    "computer",
    "computer_batch",
    "computer_apps",
)


def test_shell_tool_is_offered_under_the_host_shell_name() -> None:
    shell_name = "powershell" if os.name == "nt" else "bash"

    assert model_tool_name(BASH_TOOL_NAME) == shell_name
    assert registry_tool_name(shell_name) == BASH_TOOL_NAME
    assert registry_tool_name(BASH_TOOL_NAME) == BASH_TOOL_NAME
    assert model_tool_name("read") == registry_tool_name("read") == "read"


@pytest.mark.parametrize(
    ("called", "expected"),
    [
        # Spellings of an offered name.
        ("read", "read"),
        ("READ ", "read"),
        ("WebFetch", "web_fetch"),
        ("web-fetch", "web_fetch"),
        ("functions.bash", BASH_TOOL_NAME),
        ("default_api:read", "read"),
        ("apply_patch()", "apply_patch"),
        ("PowerShell", BASH_TOOL_NAME),
        # Other harnesses' names for an offered capability.
        ("run_shell_command", BASH_TOOL_NAME),
        ("read_file", "read"),
        ("Grep", "search_files"),
        ("Task", "subagent"),
        ("fetch", "web_fetch"),
        ("google_web_search", "web_search"),
        ("vision_analyze", "analyze_image"),
        ("tts", "generate_speech"),
        ("skills_list", "skill"),
        ("KillShell", "background_tasks"),
        ("BashOutput", "background_tasks"),
        ("TaskStop", "background_tasks"),
        ("computer_action", "computer"),
        # One inserted, removed, replaced or swapped character.
        pytest.param(
            "powerhell",
            BASH_TOOL_NAME,
            marks=pytest.mark.skipif(os.name != "nt", reason="PowerShell host name"),
        ),
        ("apply_pach", "apply_patch"),
        ("serach_files", "search_files"),
        ("web_fetchh", "web_fetch"),
        ("subagant", "subagent"),
        # Tool-call markup leaked after the name is cut off; an unknown rest stays
        # as called, without the markup.
        ("read</｜DSML｜ parameter>\n<｜DSML｜ parameter name=", "read"),
        ("Read<|tool_call_end|>", "read"),
        ("board_placeholder</｜DSML｜ parameter>", "board_placeholder"),
    ],
)
def test_called_names_resolve_to_the_offered_tool_they_mean(called: str, expected: str) -> None:
    assert called_tool_name(called, OFFERED) == expected


@pytest.mark.parametrize(
    ("called", "offered", "registered"),
    [
        pytest.param("TodoWrite", OFFERED, (), id="no-offered-meaning"),
        pytest.param("Grepper", OFFERED, (), id="near-miss"),
        pytest.param("reed", OFFERED, (), id="typo-of-short-name"),
        pytest.param("web_fetche", ("web_fetch", "web_fetcher"), (), id="ambiguous-typo"),
        pytest.param("", OFFERED, (), id="empty"),
        pytest.param("memory", OFFERED, (), id="tool-not-offered"),
        pytest.param("Grep", ("read", "apply_patch"), (), id="harness-target-not-offered"),
        pytest.param("fetch", OFFERED, ("fetch", "web_fetch"), id="registered-tool-name"),
        pytest.param(
            "computer_action",
            ("computer_batch", "computer_apps"),
            ("computer",),
            id="computer-target-not-offered",
        ),
        pytest.param(
            "computer_action",
            OFFERED,
            ("computer", "computer_action"),
            id="registered-computer-action",
        ),
        pytest.param("WebFetch", ("web_fetch", "webfetch"), (), id="ambiguous-spelling"),
        pytest.param("ReadFile", ("read", "read_file", "readfile"), (), id="ambiguous-harness"),
        pytest.param(
            "functions.ReadFile", ("read", "read_file", "readfile"), (), id="ambiguous-prefixed"
        ),
    ],
)
def test_names_without_one_clear_offered_meaning_stay_as_called(
    called: str, offered: tuple[str, ...], registered: Collection[str]
) -> None:
    assert called_tool_name(called, offered, registered=registered) == called


@pytest.mark.parametrize(
    ("called", "patch_route", "replace_route"),
    [
        ("Edit", "apply_patch", "edit"),
        ("MultiEdit", "apply_patch", "edit"),
        ("str_replace_based_edit_tool", "apply_patch", "edit"),
        ("Write", "apply_patch", "write"),
        ("create_file", "apply_patch", "write"),
        ("apply_diff", "apply_patch", "apply_diff"),
    ],
)
def test_file_edit_names_resolve_to_the_offered_edit_dialect(
    called: str, patch_route: str, replace_route: str
) -> None:
    assert called_tool_name(called, (BASH_TOOL_NAME, "read", "apply_patch")) == patch_route
    assert called_tool_name(called, (BASH_TOOL_NAME, "read", "edit", "write")) == replace_route


def test_a_real_tool_wins_over_a_harness_name() -> None:
    assert called_tool_name("terminal", (BASH_TOOL_NAME, "terminal")) == "terminal"
    assert called_tool_name("Terminal", (BASH_TOOL_NAME, "terminal")) == "terminal"
    assert called_tool_name("terminal", (BASH_TOOL_NAME,)) == BASH_TOOL_NAME
    assert called_tool_name("computer_action", (*OFFERED, "computer_action")) == "computer_action"
    assert (
        called_tool_name("functions.computer_action()", OFFERED, registered=("computer_action",))
        == "computer_action"
    )
