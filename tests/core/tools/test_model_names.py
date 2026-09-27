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
    "text_to_speech",
    "skill",
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
        ("Edit", "apply_patch"),
        ("Grep", "search_files"),
        ("Task", "subagent"),
        ("fetch", "web_fetch"),
        ("google_web_search", "web_search"),
        ("vision_analyze", "analyze_image"),
        ("tts", "text_to_speech"),
        ("skills_list", "skill"),
    ],
)
def test_called_names_resolve_to_the_offered_tool_they_mean(called: str, expected: str) -> None:
    assert called_tool_name(called, OFFERED) == expected


@pytest.mark.parametrize(
    ("called", "offered", "registered"),
    [
        pytest.param("TodoWrite", OFFERED, (), id="no-offered-meaning"),
        pytest.param("Grepper", OFFERED, (), id="near-miss"),
        pytest.param("", OFFERED, (), id="empty"),
        pytest.param("memory", OFFERED, (), id="tool-not-offered"),
        pytest.param("Grep", ("read", "apply_patch"), (), id="harness-target-not-offered"),
        pytest.param("fetch", OFFERED, ("fetch", "web_fetch"), id="registered-tool-name"),
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


def test_an_offered_tool_wins_over_a_harness_name() -> None:
    assert called_tool_name("terminal", (BASH_TOOL_NAME, "terminal")) == "terminal"
    assert called_tool_name("Terminal", (BASH_TOOL_NAME, "terminal")) == "terminal"
    assert called_tool_name("terminal", (BASH_TOOL_NAME,)) == BASH_TOOL_NAME
