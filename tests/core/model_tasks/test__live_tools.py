"""Live Tool definitions accept intended calls and match the instructions that name them."""

from __future__ import annotations

import re

import pytest
from jsonschema import Draft202012Validator

from core.model_tasks._live_tools import (
    DELEGATION_INSTRUCTIONS,
    DIRECT_VOICE_INSTRUCTIONS,
    LIVE_TOOL_NAMES,
    LIVE_TOOL_REQUEST,
    VOICE_INSTRUCTIONS,
    live_failure,
    live_result_text,
    live_success,
    live_tools,
    request_tool,
    voice_instructions,
)
from core.providers.tool_schema import render_tool_definitions

MODES = pytest.mark.parametrize(
    ("direct_tools", "constant"),
    [(False, VOICE_INSTRUCTIONS), (True, DIRECT_VOICE_INSTRUCTIONS)],
    ids=["delegate", "direct-tools"],
)


def validator(name: str) -> Draft202012Validator:
    tool = next(item for item in live_tools() if item["name"] == name)
    return Draft202012Validator(tool["parameters"])


def test_tool_schemas_are_valid_and_accept_the_intended_calls() -> None:
    for tool in [*live_tools(), request_tool()]:
        Draft202012Validator.check_schema(tool["parameters"])
    for name, arguments in [
        ("overview", {}),
        ("overview", {"agent": "Coder"}),
        ("start_agent_session", {"agent": "Coder", "task": "Fix it", "count": 3}),
        ("start_agent_session", {"agent": "Reviewer", "task": "Review", "project": "vBot"}),
        ("start_coding_terminal", {"program": "claude", "task": "Fix it", "folder": "vBot"}),
        ("start_coding_terminal", {"program": "codex", "count": 10, "name": "Pair"}),
        ("send_message", {"target": "s2", "text": "yes"}),
        ("read", {"target": "t1"}),
        ("stop", {"target": "Coder"}),
        ("open", {"target": "s2"}),
        ("open", {"view": "projects"}),
        ("terminal", {"action": "key", "target": "t1", "key": "ctrl-c"}),
        ("terminal", {"action": "reorder", "order": ["t2", "t1"]}),
        ("terminal", {"action": "rename_group", "target": "Codex", "name": "Review"}),
    ]:
        validator(name).validate(arguments)


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("start_coding_terminal", {"program": "bash"}),
        ("start_coding_terminal", {"program": "codex", "count": 0}),
        ("start_coding_terminal", {"program": "codex", "count": 11}),
        ("start_agent_session", {"agent": "Coder"}),
        ("send_message", {"target": "s2", "text": ""}),
        ("open", {"view": "settings"}),
        ("terminal", {"action": "type"}),
    ],
)
def test_tool_schemas_reject_calls_the_app_cannot_run(name: str, arguments: dict) -> None:
    assert list(validator(name).iter_errors(arguments))


def test_tool_definitions_render_as_non_strict_provider_tools() -> None:
    rendered = render_tool_definitions(live_tools(), profile="explicit_non_strict")
    assert [tool["name"] for tool in rendered] == list(LIVE_TOOL_NAMES)
    assert all(tool["strict"] is False for tool in rendered)


def test_instructions_leave_each_models_tools_to_their_definitions() -> None:
    """No instructions list Tools, and none names a Tool its model does not get."""
    listed_tool = re.compile(r"^- \w+:", re.MULTILINE)
    for instructions in (VOICE_INSTRUCTIONS, DIRECT_VOICE_INSTRUCTIONS, DELEGATION_INSTRUCTIONS):
        assert not listed_tool.search(instructions)
    for instructions in (DIRECT_VOICE_INSTRUCTIONS, DELEGATION_INSTRUCTIONS):
        assert LIVE_TOOL_REQUEST not in instructions
    assert LIVE_TOOL_REQUEST in VOICE_INSTRUCTIONS
    assert not any(name in VOICE_INSTRUCTIONS for name in LIVE_TOOL_NAMES if "_" in name)


def test_results_render_as_plain_text() -> None:
    assert live_result_text(live_success("Sent to s1 (Coder).")) == "Sent to s1 (Coder)."
    assert live_result_text(live_failure("unknown_ref", "There is no s7 in this call.")) == (
        "Error (unknown_ref): There is no s7 in this call."
    )


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
