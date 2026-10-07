"""What the Models of a Live call are told: one voice text, Tool guidance where the Tools are."""

from __future__ import annotations

import re

import pytest

from core.model_tasks._live_brief import (
    EFFECTS_LABEL,
    backend_instructions,
    live_tool_guidance,
    voice_instructions,
)
from core.tools.live import LIVE_TOOL_NAMES

_ALL_TOOLS = frozenset({*LIVE_TOOL_NAMES, "vbot_request"})


def _named_tools(text: str) -> set[str]:
    """The Tools *text* names; one-word names such as stop are ordinary words, too."""
    return {name for name in _ALL_TOOLS if "_" in name and re.search(rf"\b{name}\b", text)}


@pytest.mark.parametrize(
    ("tools", "delegates", "hands_on_the_end"),
    [
        (["start_agent_session", "read_output", "end_call"], False, False),
        (["vbot_request"], False, True),
        ([], True, True),
        ([], False, False),
    ],
    ids=["own-live-tools", "requests-only", "hands-on-natively", "no-access"],
)
def test_every_voice_model_gets_the_same_text_and_guidance_only_for_its_live_tools(
    tools: list[str], delegates: bool, hands_on_the_end: bool
) -> None:
    shared = voice_instructions(tools=[])
    text = voice_instructions(tools=tools, delegates=delegates)
    guidance = live_tool_guidance(set(tools).__contains__)
    ending = 'hand on "end the call" at once'
    others = "\n\n".join(
        block for block in text.split("\n\n") if not block.startswith("Ending the call:")
    )

    assert others == (f"{shared}\n\n{guidance}" if guidance else shared)
    assert (ending in text) is hands_on_the_end
    assert _named_tools(text) <= set(tools)
    assert ("About vBot" in text) is bool(set(tools) & set(LIVE_TOOL_NAMES))


def test_wake_phrases_are_quoted_and_never_to_be_answered() -> None:
    text = voice_instructions(tools=[], wake_phrases=("hey vbot", "computer"))

    assert 'starting with a wake phrase ("hey vbot", "computer")' in text
    assert "Never say a wake phrase yourself." in text
    assert "Wake phrases" not in voice_instructions(tools=[])


def test_the_backend_agent_is_told_its_situation_and_only_about_tools_it_has() -> None:
    has = {"start_agent_session", "send_message"}
    text = backend_instructions(has.__contains__)
    full = backend_instructions(_ALL_TOOLS.__contains__)

    assert live_tool_guidance(has.__contains__) in text
    assert _named_tools(text) <= has
    assert "read_output" in full
    assert "System Reminder" in text
    assert ("what vBot shows right now" in full) and "what vBot shows right now" not in text
    assert EFFECTS_LABEL not in text
