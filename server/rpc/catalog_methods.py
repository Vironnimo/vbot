"""Tool, skill, command, and cwd-file catalog RPC handlers."""

from __future__ import annotations

import asyncio
from typing import Any

from core.chat import CommandDispatcher
from core.chat.file_mentions import list_mention_files, resolve_mention_root
from core.projects import resolve_prompt_project, resolve_skill_scope
from core.projects.projects import PROJECT_DEFAULT_ALLOWED_TOOLS
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.payloads import _invalid_skill_response, _skill_response, _tool_response
from server.rpc.validation import _required_agent_address

JsonObject = dict[str, Any]


def _list_tools(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "tool.list does not accept params")
    try:
        # All registered tools (readiness default off): the picker and Project Tool
        # Whitelist editor see every tool and style a not-ready one from the per-tool
        # ``ready``/``readiness_hint`` fields, rather than the tool vanishing. The
        # model-facing surfaces (provider/prompt definitions) still filter readiness.
        tools = state.runtime.tools.list_tools()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    # ``default_project_tools`` is the project Tool Whitelist base list — the editor
    # uses it as the "reset to defaults" target and to mark default-on tools, so the
    # base list stays a single server-side constant rather than a duplicated literal.
    return {
        "tools": [_tool_response(tool) for tool in tools],
        "default_project_tools": list(PROJECT_DEFAULT_ALLOWED_TOOLS),
    }


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
    # The optional ``agent_id`` (a bare id or an ``agent@projekt`` address) scopes
    # the skill suggestions to that agent's effective skills; without it the call
    # returns the global skill list (today's behavior). Validated as a request shape
    # before the domain work so a malformed address is a clean client error.
    unsupported_fields = sorted(set(params) - {"agent_id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported chat.commands fields: {', '.join(unsupported_fields)}",
        )
    address = _required_agent_address(params, "agent_id") if "agent_id" in params else None
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
        skills = _command_skill_suggestions(state, address)
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


def _command_skill_suggestions(state: Any, address: tuple[str, str | None] | None) -> list[Any]:
    """Return the skills offered for autocomplete, sorted by name.

    ``address is None`` → the global skill list (no agent scope). With an address,
    the agent is resolved and its effective skills are filtered against the
    agent-aware project-scoped registry, so an agent's suggestions are exactly the
    skills it could actually activate. The scope comes from the same shared policy
    a run uses (``resolve_prompt_project`` + ``resolve_skill_scope``): a project
    address suggests that project's pool, a rooted identity agent additionally sees
    its home project's skills, and the private-skill layer applies to identity
    agents only (a team slug colliding with an identity agent's id must not surface
    that agent's private skills here).
    """
    if address is None:
        return _sorted_filtered_skills(state.runtime.skills, ["*"])
    agent_id, project_id = address
    agent = state.runtime.agent_resolver.resolve_agent(project_id, agent_id)
    allowed_skills = getattr(agent, "allowed_skills", ["*"])
    prompt_project = resolve_prompt_project(state.runtime.projects, project_id, agent)
    skill_project_id, identity_agent_id = resolve_skill_scope(project_id, prompt_project, agent_id)
    return _sorted_filtered_skills(
        state.runtime.skills_for(skill_project_id, identity_agent_id), allowed_skills
    )


def _sorted_filtered_skills(skill_registry: Any, allowed_skills: list[str]) -> list[Any]:
    filter_allowed = getattr(skill_registry, "filter_allowed", None)
    if callable(filter_allowed):
        return sorted(filter_allowed(allowed_skills), key=lambda skill: skill.name)
    return sorted(skill_registry.list_all(), key=lambda skill: skill.name)


async def _list_files(state: Any, params: JsonObject) -> JsonObject:
    """List cwd files for the composer's ``@``-mention picker.

    Resolves the address's working directory exactly like tool path resolution
    (project repo for a project agent, else the agent workspace) and returns
    gitignore-filtered relative paths. The client fetches once per picker open
    and filters locally, so this stays a single call per interaction. The walk
    runs in a worker thread — a large tree must not block the event loop.
    """
    unsupported_fields = sorted(set(params) - {"agent_id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported files.list fields: {', '.join(unsupported_fields)}",
        )
    agent_id, project_id = _required_agent_address(params, "agent_id")
    try:
        root = resolve_mention_root(state.runtime, agent_id, project_id)
        files, truncated = await asyncio.to_thread(list_mention_files, root)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"root": str(root), "files": files, "truncated": truncated}


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return read-only catalog RPC handlers."""

    return {
        "tool.list": _list_tools,
        "skill.list": _list_skills,
        "chat.commands": _list_commands,
        "files.list": _list_files,
    }
