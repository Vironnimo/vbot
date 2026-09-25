"""Live Tool definitions accept intended calls and stay canonical; voice instructions."""

from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator

from core.model_tasks._live_tools import (
    DIRECT_VOICE_INSTRUCTIONS,
    VOICE_INSTRUCTIONS,
    live_tools,
    voice_instructions,
)
from core.providers.tool_schema import render_tool_definitions

MODES = pytest.mark.parametrize(
    ("direct_tools", "constant"),
    [(False, VOICE_INSTRUCTIONS), (True, DIRECT_VOICE_INSTRUCTIONS)],
    ids=["delegate", "direct-tools"],
)


def test_tool_schemas_accept_intended_calls_and_reject_unsupported_program():
    app, terminal = live_tools()
    for tool in (app, terminal):
        Draft202012Validator.check_schema(tool["parameters"])
    Draft202012Validator(app["parameters"]).validate(
        {"action": "send", "agent_id": "joel", "session_id": "session-a", "text": "continue"}
    )
    validator = Draft202012Validator(terminal["parameters"])
    validator.validate({"action": "start", "program": "codex", "count": 4, "workdir": "/workspace"})
    assert list(validator.iter_errors({"action": "start", "program": "bash"}))
    for count in (5, 12, 33):
        validator.validate({"action": "start", "program": "codex", "count": count})
    assert "maximum" not in terminal["parameters"]["properties"]["count"]
    for count in (0, -1, 1.5, "5"):
        assert list(validator.iter_errors({"action": "start", "program": "codex", "count": count}))
    for call in (
        {"action": "close", "terminal_id": "term-a"},
        {"action": "show_group", "group_id": "group-a"},
        {"action": "create_group", "name": "Review"},
        {"action": "rename_group", "group_id": "group-a", "name": "Review"},
        {"action": "delete_group", "group_id": "group-a"},
    ):
        validator.validate(call)


def test_tool_definitions_render_as_non_strict_provider_tools():
    rendered = render_tool_definitions(live_tools(), profile="explicit_non_strict")

    assert [tool["name"] for tool in rendered] == ["vbot_app", "vbot_terminal"]
    assert all(tool["strict"] is False for tool in rendered)


@MODES
def test_voice_instructions_without_wake_phrases_are_the_mode_text(
    direct_tools: bool, constant: str
) -> None:
    assert voice_instructions(direct_tools=direct_tools) == constant
    assert voice_instructions(direct_tools=direct_tools, wake_phrases=[]) == constant


@MODES
def test_wake_phrases_add_one_policy_block_that_quotes_each_phrase(
    direct_tools: bool, constant: str
) -> None:
    base = constant.split("\n\n")
    blocks = voice_instructions(
        direct_tools=direct_tools, wake_phrases=("Hey Nabu", "Hey Jarvis")
    ).split("\n\n")

    added = [block for block in blocks if block not in base]
    assert len(added) == 1
    assert [block for block in blocks if block != added[0]] == base
    # Same place in both modes: right before the closing backchannel policy.
    assert blocks[-2] == added[0]
    assert '"Hey Nabu", "Hey Jarvis"' in added[0]

    single = voice_instructions(direct_tools=direct_tools, wake_phrases=["Okay Nabu"])
    assert '"Okay Nabu"' in single
    assert '"Okay Nabu",' not in single
