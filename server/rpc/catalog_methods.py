"""Tool, skill, command, and cwd-file catalog RPC handlers."""

from __future__ import annotations

from typing import Any

from core.chat.file_mentions import (
    list_mention_directory,
    list_mention_files,
    resolve_mention_root,
)
from core.projects import (
    WorkingProjectMissingError,
    project_tool_configurability_reason,
    resolve_prompt_project,
    resolve_skill_scope,
)
from core.projects.projects import PROJECT_DEFAULT_ALLOWED_TOOLS
from core.sessions import AGENT_DEFAULT_PROJECT, WorkingProjectChoice
from core.utils.workers import BoundedWorkerPool
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.payloads import _invalid_skill_response, _skill_response, _tool_response
from server.rpc.runtime_access import _state_command_dispatcher
from server.rpc.validation import (
    _draft_working_project,
    _optional_string,
    _reject_unsupported,
    _required_agent_address,
)

JsonObject = dict[str, Any]
_COMMAND_CATALOG_OUTPUT = {
    "notice": "toast",
    "detail": "transient",
    "state_change": "action",
}
_CATALOG_WORKERS = BoundedWorkerPool(name="catalog", max_workers=4)


def _list_tools(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "tool.list does not accept params")
    try:
        # All registered tools (readiness default off): the picker and Project Tool
        # Whitelist editor see every tool and style a not-ready one from the per-tool
        # ``ready``/``readiness_hint`` fields, rather than the tool vanishing. The
        # model-facing surfaces (provider/prompt definitions) still filter readiness.
        tools = state.runtime.tools.list_tools(
            include_session_scoped=True,
            include_catalog_hidden=False,
        )
        _validate_tool_relationships(tools)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    # ``default_project_tools`` is the project Tool Whitelist base list — the editor
    # uses it as the "reset to defaults" target and to mark default-on tools, so the
    # base list stays a single server-side constant rather than a duplicated literal.
    return {
        "tools": [_project_annotated_tool_response(tool) for tool in tools],
        "default_project_tools": list(PROJECT_DEFAULT_ALLOWED_TOOLS),
    }


def _project_annotated_tool_response(tool: Any) -> JsonObject:
    """Project the generic Tool plus server-owned Project configurability policy."""
    response = _tool_response(tool)
    reason = project_tool_configurability_reason(
        activation=tool.activation,
        constraints=tool.constraints,
    )
    response["project_configurable"] = reason is None
    response["project_configurability_reason"] = reason
    return response


def _validate_tool_relationships(tools: list[Any]) -> None:
    """Reject follower references that cannot activate at runtime."""

    names = {tool.name for tool in tools}
    for tool in tools:
        if (
            getattr(tool, "activation", "configurable") == "follows"
            and getattr(tool, "activation_source", None) not in names
        ):
            raise ValueError(
                f"Tool '{tool.name}' follows an unregistered Tool: "
                f"{getattr(tool, 'activation_source', None)}"
            )


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


async def _list_commands(state: Any, params: JsonObject) -> JsonObject:
    # The optional ``agent_id`` (a bare id or an ``agent@projekt`` address) scopes
    # the skill suggestions to that agent's effective skills; without it the call
    # returns the global skill list (today's behavior). The optional ``session_id``
    # names that Agent's Session, whose Skills may be another Agent's (a Librarian
    # Session) and which works in its own Project. Without a Session, the optional
    # ``working_project_id`` names the Project a draft's new Session would work in
    # (left out: the Agent's default Project; null: its Workspace). Validated as a
    # request shape before the domain work so a malformed address is a clean
    # client error.
    _reject_unsupported(params, {"agent_id", "session_id", "working_project_id"}, "chat.commands")
    address = _required_agent_address(params, "agent_id") if "agent_id" in params else None
    session_id = _optional_string(params, "session_id")
    if address is None and ("session_id" in params or "working_project_id" in params):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.session_id and params.working_project_id need params.agent_id",
        )
    working_project_id = _draft_working_project(
        params, None if address is None else address[1], session_id
    )
    try:
        command_items = [
            {
                "name": spec.name,
                "description": spec.description,
                "type": "command",
                # The public presentation hint is projected from Chat's neutral
                # result category; the catalog never drives command execution.
                "argument": spec.argument,
                "output": _COMMAND_CATALOG_OUTPUT[spec.catalog_result],
            }
            for spec in _state_command_dispatcher(state).catalog()
        ]
        skills = await _command_skill_suggestions(state, address, session_id, working_project_id)
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


async def _command_skill_suggestions(
    state: Any,
    address: tuple[str, str | None] | None,
    session_id: str | None = None,
    working_project_id: WorkingProjectChoice = AGENT_DEFAULT_PROJECT,
) -> list[Any]:
    """Return the skills offered for autocomplete, sorted by name.

    ``address is None`` → the global skill list (no agent scope). With an address,
    the agent is resolved and its effective skills are filtered against the
    agent-aware project-scoped registry, so an agent's suggestions are exactly the
    skills it could actually activate. The scope comes from the same shared policy
    a run uses (``resolve_prompt_project`` + ``resolve_skill_scope``): a project
    address suggests that project's pool, an identity agent working in a Project
    additionally sees that Project's skills, and the private-skill layer applies to
    identity agents only (a team slug colliding with an identity agent's id must
    not surface that agent's private skills here). With ``session_id`` the Agent
    resolves as that Session runs it, in the Session's working Project, so a
    Librarian Session suggests the Skills it maintains; without one the working
    Project is *working_project_id* (a draft's new Session). A Session whose
    Project no longer exists cannot run, so it is offered no Skills.

    A Chat asks whenever its Agent address changes, so nothing here runs on the
    Event Loop: the Agent resolves on its own pools, and the scope and
    availability checks (Project and Agent files, Skill directories, binary
    requirements on ``PATH``) run on the catalog pool.
    """
    if address is None:
        return await _CATALOG_WORKERS.run(_sorted_filtered_skills, state.runtime.skills, ["*"])
    agent_id, project_id = address
    resolver = state.runtime.agent_resolver
    agent = await resolver.resolve_agent_async(project_id, agent_id, session_id=session_id)
    try:
        working_project = await resolver.resolve_working_project_async(
            project_id, agent, session_id=session_id, requested=working_project_id
        )
    except WorkingProjectMissingError:
        return []
    return await _CATALOG_WORKERS.run(
        _agent_skill_suggestions, state.runtime, project_id, agent, working_project
    )


def _agent_skill_suggestions(
    runtime: Any, project_id: str | None, agent: Any, working_project_id: str | None
) -> list[Any]:
    allowed_skills = getattr(agent, "allowed_skills", ["*"])
    prompt_project = resolve_prompt_project(runtime.projects, working_project_id)
    skill_project_id, identity_agent_id = resolve_skill_scope(project_id, prompt_project, agent)
    return _sorted_filtered_skills(
        runtime.skills_for(skill_project_id, identity_agent_id), allowed_skills
    )


def _sorted_filtered_skills(skill_registry: Any, allowed_skills: list[str]) -> list[Any]:
    return sorted(skill_registry.filter_allowed(allowed_skills), key=lambda skill: skill.name)


async def _list_files(state: Any, params: JsonObject) -> JsonObject:
    """List the mention root for the composer's ``@``-mention picker.

    Resolves the working directory exactly like tool path resolution (the repo
    of the Project the Session works in, else the agent workspace). The optional
    ``session_id`` names the Session whose working Project counts; without one,
    the optional ``working_project_id`` names a draft's (left out: the Agent's
    default Project; null: its Workspace). Without ``directory`` the result is
    the index of the files and directories the search Tools would search there;
    the client fetches it once per picker open and filters locally. With
    ``directory`` (relative to the root, ``""`` is the root) it carries that
    directory's direct ``entries`` instead, ignored ones included and marked.
    """
    _reject_unsupported(
        params, {"agent_id", "session_id", "working_project_id", "directory"}, "files.list"
    )
    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _optional_string(params, "session_id")
    working_project_id = _draft_working_project(params, project_id, session_id)
    directory = params.get("directory")
    if directory is not None and not isinstance(directory, str):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.directory must be a string or null")
    try:
        # Resolving the root reads the Agent, whose current-Session pointer it
        # verifies, and the Session, so it runs on the Session database's pool.
        root = await state.runtime.chat_sessions.run_async(
            lambda: resolve_mention_root(
                state.runtime,
                agent_id,
                project_id,
                session_id=session_id,
                working_project_id=working_project_id,
            )
        )
        if directory is not None:
            listed = await list_mention_directory(root, directory)
            return {
                "root": str(root),
                "files": [],
                "directories": [],
                "truncated": listed.truncated,
                "entries": [
                    {"name": entry.name, "kind": entry.kind, "ignored": entry.ignored}
                    for entry in listed.entries
                ],
            }
        index = await list_mention_files(root)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {
        "root": str(root),
        "files": list(index.files),
        "directories": list(index.directories),
        "truncated": index.truncated,
    }


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return read-only catalog RPC handlers."""

    return {
        "tool.list": _list_tools,
        "skill.list": _list_skills,
        "chat.commands": _list_commands,
        "files.list": _list_files,
    }
