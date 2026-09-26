"""Tool names the Model sees and names Models call instead."""

from __future__ import annotations

import pytest

from core.tools import BASH_TOOL_NAME, called_tool_name

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


@pytest.mark.parametrize(
    ("called", "expected"),
    [
        ("read", "read"),
        ("Read", "read"),
        ("READ ", "read"),
        ("functions.bash", BASH_TOOL_NAME),
        ("default_api:read", "read"),
        ("tools.search_files", "search_files"),
        ("WebFetch", "web_fetch"),
        ("web-fetch", "web_fetch"),
        ("apply_patch()", "apply_patch"),
        ("PowerShell", BASH_TOOL_NAME),
        ("shell", BASH_TOOL_NAME),
        ("run_shell_command", BASH_TOOL_NAME),
        ("Bash", BASH_TOOL_NAME),
        ("read_file", "read"),
        ("Edit", "apply_patch"),
        ("write_file", "apply_patch"),
        ("Grep", "search_files"),
        ("Glob", "search_files"),
        ("list_dir", "search_files"),
        ("Task", "subagent"),
        ("fetch", "web_fetch"),
        ("google_web_search", "web_search"),
        ("vision_analyze", "analyze_image"),
        ("tts", "text_to_speech"),
        ("str_replace_based_edit_tool", "apply_patch"),
        ("skills_list", "skill"),
    ],
)
def test_called_names_resolve_to_the_offered_tool_they_mean(called: str, expected: str) -> None:
    assert called_tool_name(called, OFFERED) == expected


@pytest.mark.parametrize("called", ["TodoWrite", "image_generation", "Grepper", "", "memory"])
def test_names_without_an_offered_meaning_stay_as_called(called: str) -> None:
    assert called_tool_name(called, OFFERED) == called


def test_a_harness_name_maps_only_to_an_offered_tool() -> None:
    assert called_tool_name("Grep", ("read", "apply_patch")) == "Grep"


def test_a_registered_tool_is_never_remapped() -> None:
    # "fetch" names a real Tool here, even though this request does not offer it.
    assert called_tool_name("fetch", OFFERED, registered={"fetch", "web_fetch"}) == "fetch"


def test_an_offered_tool_wins_over_a_harness_name() -> None:
    assert called_tool_name("terminal", (BASH_TOOL_NAME, "terminal")) == "terminal"
    assert called_tool_name("Terminal", (BASH_TOOL_NAME, "terminal")) == "terminal"
    assert called_tool_name("terminal", (BASH_TOOL_NAME,)) == BASH_TOOL_NAME


@pytest.mark.parametrize(
    ("called", "offered"),
    [
        ("WebFetch", ("web_fetch", "webfetch")),
        ("ReadFile", ("read", "read_file", "readfile")),
        ("SearchWeb", ("web_search", "search_web", "searchweb")),
        ("functions.ReadFile", ("read", "read_file", "readfile")),
    ],
)
def test_ambiguous_spelling_stays_as_called(called: str, offered: tuple[str, ...]) -> None:
    assert called_tool_name(called, offered) == called
