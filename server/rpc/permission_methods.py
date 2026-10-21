"""Permission Rules RPCs: live validation for the rule editors and a Session's limits.

``permissions.validate`` normalizes a candidate rule set without saving it, with
every problem, the preset it matches and a preview of what it lets the Agent
reach. ``permissions.session`` describes the limits a Session's Runs work under.
Both read Agents, so they run on the Session database's pool like Agent reads.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.chat import session_permissions
from core.permissions import (
    PermissionRules,
    RuleSetCheck,
    check_permission_rules,
    describe_layer,
    describe_permission_rules,
    matching_preset,
    normalize_permission_rules,
    permission_rules_to_dict,
    preset_definitions,
    resolve_rule_set,
)
from core.projects import ResolutionAgentNotFoundError, RuntimeAgent
from core.sessions import SessionAddress, WorkingProjectChoice
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.validation import (
    _draft_working_project,
    _optional_bool,
    _optional_string,
    _reject_unsupported,
    _required_agent_address,
)

JsonObject = dict[str, Any]


async def _validate_permission_rules(state: Any, params: JsonObject) -> JsonObject:
    """Normalize ``permission_rules`` for an editor, without saving anything.

    ``preset`` names the preset the effective rules match (``full_access`` for
    none) and ``inherited_preset`` the one of what the level inherits.

    Without ``agent_id`` the rule set stands alone (the shared default) and the
    preview keeps placeholders. With ``agent_id`` it is that Agent's own rule set
    (``agent@project``: the Team member's vBot override): ``inherited`` and
    ``effective`` add what it inherits, and the preview expands placeholders for
    the folder its new Sessions work in. ``new_agent`` (without ``agent_id``) is
    the rule set of an Agent being created: it inherits the shared default and
    the preview keeps the placeholders of folders the Agent does not have yet.
    """
    _reject_unsupported(
        params, {"permission_rules", "agent_id", "new_agent"}, "permissions.validate"
    )
    if "permission_rules" not in params:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.permission_rules is required; null stands for no own rules",
        )
    value = params["permission_rules"]
    address = _required_agent_address(params, "agent_id") if "agent_id" in params else None
    new_agent = _optional_bool(params, "new_agent", default=False)
    if new_agent and address is not None:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.new_agent is for an Agent being created; leave out agent_id",
        )
    check = check_permission_rules(value) if value is not None else RuleSetCheck(None, (), ())
    inherited: PermissionRules | None = None
    values: dict[str, str | None] = {}
    if new_agent:
        inherited = _rules_or_none(state.runtime.agent_resolver.inherited_permission_rules())
        values = {"project": None, "home": str(Path.home())}
    elif address is not None:
        agent_id, project_id = address
        try:
            inherited, values = await state.runtime.chat_sessions.run_async(
                _editor_context, state, agent_id, project_id
            )
        except Exception as exc:
            raise _map_expected_error(exc) from exc
    valid = not check.problems
    own = check.rules if valid else None
    effective = resolve_rule_set(own, inherited) if valid else None
    return {
        "valid": valid,
        "permission_rules": permission_rules_to_dict(own) if own is not None else None,
        "notes": list(check.notes),
        "errors": [problem.to_dict() for problem in check.problems],
        # The preset the resulting rules match; an editor's picker shows it.
        "preset": matching_preset(effective) if valid else None,
        "presets": preset_definitions(),
        "inherited_preset": matching_preset(inherited),
        "inherited": permission_rules_to_dict(inherited) if inherited is not None else None,
        "effective": permission_rules_to_dict(effective) if effective is not None else None,
        "preview": (
            describe_permission_rules(effective, values=values) if effective is not None else None
        ),
    }


def _editor_context(
    state: Any, agent_id: str, project_id: str | None
) -> tuple[PermissionRules | None, dict[str, str | None]]:
    """Blocking: what the edited level inherits, and its new Sessions' folders."""
    runtime = state.runtime
    resolver = runtime.agent_resolver
    if project_id is not None:
        project = runtime.projects.get(project_id)
        member = next(
            (item for item in resolver.team_for_project(project_id) if item.agent_id == agent_id),
            None,
        )
        if member is None:
            raise ResolutionAgentNotFoundError(
                f"agent '{agent_id}' is not on project '{project_id}' team"
            )
        cwd = str(Path(project.cwd))
        inherited = resolver.inherited_permission_rules(member)
        values = {"workdir": cwd, "project": cwd, "workspace": None, "home": str(Path.home())}
        return _rules_or_none(inherited), values
    agent = runtime.agents.get(agent_id)
    # New Sessions work in the default Project; one that no longer exists names none.
    default_project = getattr(agent, "root_project_id", None)
    working = (
        default_project
        if default_project is not None and runtime.projects.exists(default_project)
        else None
    )
    inherited = resolver.inherited_permission_rules()
    return _rules_or_none(inherited), _session_values(state, agent, working)


async def _session_permissions(state: Any, params: JsonObject) -> JsonObject:
    """Describe the limits a Session's Runs work under; cheap for full access.

    ``session_id`` names an existing Session (its Agent overrides hold a
    Sub-Agent's inherited ceiling); without it the result is for a new Session
    in ``working_project_id`` (left out: the Agent's default Project). Each layer
    is described on its own: the Agent's rules (``source`` ``agent``) and every
    ceiling it inherited (``parent``, ``extension``).
    """
    _reject_unsupported(
        params, {"agent_id", "session_id", "working_project_id"}, "permissions.session"
    )
    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _optional_string(params, "session_id")
    requested = _draft_working_project(params, project_id, session_id)
    try:
        summary: JsonObject = await state.runtime.chat_sessions.run_async(
            _session_summary, state, agent_id, project_id, session_id, requested
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return summary


def _session_summary(
    state: Any,
    agent_id: str,
    project_id: str | None,
    session_id: str | None,
    requested: WorkingProjectChoice,
) -> JsonObject:
    runtime = state.runtime
    resolver = runtime.agent_resolver
    agent: RuntimeAgent = resolver.resolve_agent(
        project_id, agent_id, session_id=session_id, require_model=False
    )
    ceiling = (
        resolver.session_overrides(SessionAddress(project_id, agent_id, session_id)).ceiling()
        if session_id is not None
        else None
    )
    data_dir = Path(runtime.storage.data_dir)
    # Composed without the Working Project first: full access resolves no more.
    permissions = session_permissions(agent, data_dir=data_dir, project_root=None, ceiling=ceiling)
    if not permissions.restricted:
        return {"restricted": False, "layers": []}
    layers: list[JsonObject] = []
    if permissions.rules is not None:
        working = resolver.resolve_working_project(
            project_id, agent, session_id=session_id, requested=requested
        )
        permissions = session_permissions(
            agent,
            data_dir=data_dir,
            project_root=_project_root(state, working),
            ceiling=ceiling,
        )
        assert permissions.rules is not None
        layers.append(
            describe_permission_rules(permissions.rules, values=permissions.placeholder_values())
        )
    for inherited in permissions.ceilings:
        layers.extend(describe_layer(layer) for layer in inherited.layers)
    return {"restricted": True, "layers": layers}


def _session_values(
    state: Any, agent: RuntimeAgent, working_project_id: str | None
) -> dict[str, str | None]:
    """The placeholder values of a Session working in ``working_project_id``."""
    return session_permissions(
        agent,
        data_dir=Path(state.runtime.storage.data_dir),
        project_root=_project_root(state, working_project_id),
        ceiling=None,
    ).placeholder_values()


def _project_root(state: Any, working_project_id: str | None) -> Path | None:
    if working_project_id is None:
        return None
    return Path(state.runtime.projects.get(working_project_id).cwd)


def _rules_or_none(stored: dict[str, Any] | None) -> PermissionRules | None:
    return normalize_permission_rules(stored) if stored is not None else None


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return the Permission Rules RPC handlers."""

    return {
        "permissions.validate": _validate_permission_rules,
        "permissions.session": _session_permissions,
    }
