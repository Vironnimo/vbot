"""Tool, skill, and command catalog RPC handlers."""

from __future__ import annotations

from typing import Any

from core.chat import CommandDispatcher
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.payloads import _invalid_skill_response, _skill_response, _tool_response

JsonObject = dict[str, Any]


def _list_tools(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "tool.list does not accept params")
    try:
        tools = state.runtime.tools.list_tools()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"tools": [_tool_response(tool) for tool in tools]}


def _list_skills(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "skill.list does not accept params")
    try:
        skills = state.runtime.skills.list_all()
        invalid_skills = state.runtime.skills.invalid_diagnostics()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {
        "skills": [_skill_response(state.runtime.skills, skill) for skill in skills],
        "invalid_skills": [_invalid_skill_response(diagnostic) for diagnostic in invalid_skills],
    }


def _list_commands(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "chat.commands does not accept params")
    try:
        command_items = [
            {
                "name": spec.name,
                "description": spec.description,
                "type": "command",
                # Argument mode and output channel let the frontend derive trigger
                # and presentation behavior without per-command special cases.
                "argument": spec.argument,
                "output": spec.output,
            }
            for spec in sorted(
                CommandDispatcher.BUILT_IN_COMMANDS.values(), key=lambda spec: spec.name
            )
        ]
        skill_registry = state.runtime.skills
        filter_allowed = getattr(skill_registry, "filter_allowed", None)
        if callable(filter_allowed):
            skills = sorted(filter_allowed(["*"]), key=lambda skill: skill.name)
        else:
            skills = sorted(skill_registry.list_all(), key=lambda skill: skill.name)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    skill_items = [
        {
            "name": skill.name,
            "description": skill.description,
            "type": "skill",
        }
        for skill in skills
    ]
    return {"items": [*command_items, *skill_items]}


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return read-only catalog RPC handlers."""

    return {
        "tool.list": _list_tools,
        "skill.list": _list_skills,
        "chat.commands": _list_commands,
    }
