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
)
from core.providers.tool_schema import render_tool_definitions


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


def test_instructions_name_exactly_the_tools_each_model_gets() -> None:
    guide = re.compile(r"^- (\w+):", re.MULTILINE)
    for instructions in (DIRECT_VOICE_INSTRUCTIONS, DELEGATION_INSTRUCTIONS):
        tools_block = instructions.split("Your Tools:\n", 1)[1].split("\n\n", 1)[0]
        assert guide.findall(tools_block) == list(LIVE_TOOL_NAMES)
    assert LIVE_TOOL_REQUEST in VOICE_INSTRUCTIONS
    assert not any(f"{name}:" in VOICE_INSTRUCTIONS for name in LIVE_TOOL_NAMES)


def test_results_render_as_plain_text() -> None:
    assert live_result_text(live_success("Sent to s1 (Coder).")) == "Sent to s1 (Coder)."
    assert live_result_text(live_failure("unknown_ref", "There is no s7 in this call.")) == (
        "Error (unknown_ref): There is no s7 in this call."
    )
