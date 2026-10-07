"""What the Models of a Live call are told: only Tools they have, and how results arrive."""

from __future__ import annotations

import re

import pytest

from core.model_tasks._live_brief import EFFECTS_LABEL, backend_instructions, voice_instructions
from core.tools.live import LIVE_TOOL_NAMES

_ALL_TOOLS = frozenset({*LIVE_TOOL_NAMES, "vbot_request"})


def _named_tools(text: str) -> set[str]:
    """The Tools *text* names; one-word names such as stop are ordinary words, too."""
    return {name for name in _ALL_TOOLS if "_" in name and re.search(rf"\b{name}\b", text)}


@pytest.mark.parametrize(
    ("options", "named", "effects_line"),
    [
        (
            {"tools": ["overview", "read_output", "end_call"]},
            {"overview", "read_output", "end_call"},
            False,
        ),
        ({"tools": ["overview", "vbot_request"]}, {"vbot_request"}, True),
        ({"tools": [], "delegation": "vbot", "backend_tools": ["end_call"]}, set(), True),
        ({"tools": [], "delegation": "openai", "backend_tools": ["end_call"]}, set(), False),
        ({"tools": ["vbot_request"], "backend_tools": ["overview"]}, {"vbot_request"}, True),
        ({"tools": []}, set(), False),
    ],
    ids=[
        "own-tools",
        "own-tools-and-requests",
        "hands-on-to-vbot",
        "hands-on-to-openai",
        "requests-only",
        "no-access",
    ],
)
def test_the_voice_model_is_told_only_about_tools_it_has(
    options: dict[str, object], named: set[str], effects_line: bool
) -> None:
    text = voice_instructions(**options)  # type: ignore[arg-type]

    assert _named_tools(text) == named - {"overview"}
    assert ("ends with what vBot changed" in text) is effects_line


def test_the_voice_model_hands_on_hanging_up_only_when_the_backend_can_end_the_call() -> None:
    with_end = voice_instructions(tools=[], delegation="vbot", backend_tools=["end_call"])
    without_end = voice_instructions(tools=[], delegation="vbot", backend_tools=["overview"])

    assert "that includes ending this call" in with_end
    assert "ending this call" not in without_end


def test_wake_phrases_are_quoted_and_never_to_be_answered() -> None:
    text = voice_instructions(tools=[], wake_phrases=("hey vbot", "computer"))

    assert 'starting with a wake phrase ("hey vbot", "computer")' in text
    assert "Never say a wake phrase yourself." in text
    assert "Wake phrases" not in voice_instructions(tools=[])


@pytest.mark.parametrize("context_note", [True, False])
def test_the_backend_is_told_only_about_tools_it_has(context_note: bool) -> None:
    has = {"start_agent_session", "send_message"}
    text = backend_instructions(has.__contains__, context_note=context_note)
    full = backend_instructions(_ALL_TOOLS.__contains__, context_note=context_note)

    assert _named_tools(text) <= has
    assert "end_call" in full and "read_output" in full
    # Only vBot's backend Agent gets the call's context as a System Reminder.
    assert ("System Reminder" in text) is context_note
    assert ("what vBot shows right now" in full) is context_note
    assert "what vBot shows right now" not in text
    assert EFFECTS_LABEL not in text
