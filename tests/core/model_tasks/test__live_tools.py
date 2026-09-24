"""Live Tool definitions accept intended calls and stay canonical."""

from __future__ import annotations

from jsonschema import Draft202012Validator

from core.model_tasks._live_tools import live_tools
from core.providers.tool_schema import render_tool_definitions


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
