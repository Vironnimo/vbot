"""Permission Rules RPCs: live validation for the rule editors and a Session's limits.

``permissions.validate`` normalizes a candidate rule set without saving it, with
every problem, the preset it matches and a preview of what it lets the Agent
reach. ``permissions.session`` describes the limits a Session's Runs work under,
``permissions.set_session_rules`` sets the user's profile or rules for one Session,
``permissions.remove_session_grants`` takes back what the user allowed for the
rest of a Session, and ``permissions.clear_session_ceiling`` lifts the limits a
Session inherited. ``permissions.pending`` and ``permissions.resolve`` answer
the approval requests of live Runs.
They read Agents or Sessions, so they run on the Session database's pool like
Agent reads.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from core.chat import session_permissions
from core.permissions import (
    BUILTIN_PERMISSION_PROFILES,
    PERMISSION_PRESETS,
    PermissionRules,
    PermissionRulesError,
    RuleSetCheck,
    check_permission_rules,
    describe_layer,
    describe_permission_rules,
    editor_bases,
    matching_preset,
    normalize_permission_rules,
    normalize_session_grants,
    permission_rules_to_dict,
    preset_definitions,
    resolve_rule_set,
)
from core.projects import (
    AgentOverrides,
    AgentResolutionError,
    ResolutionAgentNotFoundError,
    RuntimeAgent,
)
from core.sessions import SessionAddress, WorkingProjectChoice
from core.tools._tool_context import ToolApprovalCancelledError, ToolAuthorityUnavailableError
from core.tools.shell_cli import ShellCliAccessError
from core.utils.errors import VBotError
from server.access import current_principal
from server.rpc import _permission_profiles
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.event_bridge import publish_session_changed
from server.rpc.validation import (
    _draft_working_project,
    _optional_bool,
    _optional_string,
    _reject_unsupported,
    _required_agent_address,
    _required_bool,
    _required_session_address,
    _required_string,
    rpc_request_fields,
)

JsonObject = dict[str, Any]


async def _validate_permission_rules(state: Any, params: JsonObject) -> JsonObject:
    """Normalize ``permission_rules`` for an editor, without saving anything.

    ``preset`` names the preset the effective rules match (``full_access`` for
    none) and ``inherited_preset`` the one of what the level inherits.
    ``presets`` holds each preset as the change it makes, and ``bases`` what
    applies to each base part and which bases the level cannot choose
    (:func:`editor_bases`), so the editor re-implements no rule.

    Without ``agent_id`` the rule set stands alone (the shared default) and the
    preview keeps placeholders. With ``agent_id`` it is that Agent's own rule set
    (``agent@project``: the Team member's vBot override): ``inherited`` and
    ``effective`` add what it inherits, and the preview expands placeholders for
    the folder its new Sessions work in. ``new_agent`` (without ``agent_id``) is
    the rule set of an Agent being created: it inherits the shared default and
    the preview keeps the placeholders of folders the Agent does not have yet.
    """
    _reject_unsupported(params, rpc_request_fields("permissions.validate"), "permissions.validate")
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
                _editor_context, state, agent_id, project_id, require_inherited=value is None
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
        "bases": editor_bases(value if isinstance(value, dict) else None, inherited),
        "inherited_preset": matching_preset(inherited),
        "inherited": permission_rules_to_dict(inherited) if inherited is not None else None,
        "effective": permission_rules_to_dict(effective) if effective is not None else None,
        "preview": (
            describe_permission_rules(effective, values=values) if effective is not None else None
        ),
    }


def _editor_context(
    state: Any, agent_id: str, project_id: str | None, *, require_inherited: bool
) -> tuple[PermissionRules | None, dict[str, str | None]]:
    """Blocking: what the edited level inherits, and its new Sessions' folders."""
    runtime = state.runtime
    resolver = runtime.agent_resolver
    member = None
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
        values = {"workdir": cwd, "project": cwd, "workspace": None, "home": str(Path.home())}
    else:
        agent = runtime.agents.get(agent_id)
        # New Sessions work in the default Project; one that no longer exists names none.
        default_project = getattr(agent, "root_project_id", None)
        working = (
            default_project
            if default_project is not None and runtime.projects.exists(default_project)
            else None
        )
        values = _session_values(state, agent, working)
    # A clear proposes inheritance as the actual policy. Editor placeholders
    # for an unreadable default cannot serve as its approved Full Access value.
    if require_inherited:
        return resolver.default_permission_rules(member), values
    return _rules_or_none(resolver.inherited_permission_rules(member)), values


async def _session_permissions(state: Any, params: JsonObject) -> JsonObject:
    """Describe the limits a Session's Runs work under; cheap for full access.

    ``session_id`` names an existing Session (its Agent overrides hold a
    Sub-Agent's inherited ceiling); without it the result is for a new Session
    in ``working_project_id`` (left out: the Agent's default Project). Each layer
    is described on its own: the Agent's rules (``source`` ``agent``) and every
    ceiling it inherited (``parent``, ``cron``, ``extension``).
    ``stored_ceiling`` says whether the Session stores an inherited ceiling
    that ``permissions.clear_session_ceiling`` removes. The access picker's
    data (``_access_choice``): the ``profiles``, the ``agent_profile``, and the
    Session's own choice (``permissions.set_session_rules``), a
    ``session_profile`` name or older ``session_rules``, both ``null`` without
    one; ``session_profile_error`` when that profile no longer exists.
    ``session_grants`` are the Session Grants, the allow rules the user added
    while approving Tool calls; the Agent's layer already includes them.
    """
    _reject_unsupported(params, rpc_request_fields("permissions.session"), "permissions.session")
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
    overrides = (
        resolver.session_overrides(SessionAddress(project_id, agent_id, session_id))
        if session_id is not None
        else AgentOverrides()
    )
    ceiling = overrides.ceiling()
    own = _access_choice(state, agent_id, project_id, overrides)
    try:
        agent: RuntimeAgent = resolver.resolve_agent(
            project_id,
            agent_id,
            session_id=session_id,
            require_model=False,
            include_temporary=session_id is not None,
        )
    except AgentResolutionError as error:
        if overrides.permission_profile is None or overrides.permission_profile in {
            profile["name"] for profile in own["profiles"]
        }:
            raise
        # The Session's profile is gone: its Runs stop until the user chooses again.
        return {
            "restricted": True,
            "layers": [],
            "stored_ceiling": ceiling is not None,
            "session_profile_error": str(error),
            **own,
        }
    data_dir = Path(runtime.storage.data_dir)
    # Composed without the Working Project first: full access resolves no more.
    permissions = session_permissions(agent, data_dir=data_dir, project_root=None, ceiling=ceiling)
    if not permissions.restricted:
        return {"restricted": False, "layers": [], "stored_ceiling": False, **own}
    layers: list[JsonObject] = []
    if permissions.rules is not None or permissions.extension_rules is not None:
        working = resolver.resolve_working_project(
            project_id, agent, session_id=session_id, requested=requested
        )
        permissions = session_permissions(
            agent,
            data_dir=data_dir,
            project_root=_project_root(state, working),
            ceiling=ceiling,
        )
        for source, rules in (
            ("agent", permissions.rules),
            ("extension", permissions.extension_rules),
        ):
            if rules is not None:
                layer = describe_permission_rules(rules, values=permissions.placeholder_values())
                layers.append({**layer, "source": source})
    for inherited in permissions.ceilings:
        layers.extend(describe_layer(layer) for layer in inherited.layers)
    return {"restricted": True, "layers": layers, "stored_ceiling": ceiling is not None, **own}


def _access_choice(
    state: Any, agent_id: str, project_id: str | None, overrides: AgentOverrides
) -> JsonObject:
    """What the access picker offers and shows as chosen.

    ``profiles`` lists every available Permission Profile and whether it
    restricts. ``agent_profile`` names the profile the Agent's own rules come
    from or equal (``full_access`` for none), ``null`` for rules of its own or
    an Agent that resolves only with its Session (a temporary Agent).
    """
    resolver = state.runtime.agent_resolver
    profiles: dict[str, PermissionRules] = {}
    for name in resolver.permission_profile_names():
        try:
            profiles[name] = resolver.permission_rules_for_profile(name)
        except AgentResolutionError:
            continue
    try:
        agent: RuntimeAgent | None = resolver.resolve_agent(
            project_id, agent_id, require_model=False
        )
    except AgentResolutionError:
        agent = None
    agent_profile: str | None = None
    if agent is not None:
        selected = getattr(agent, "permission_profile", None)
        agent_profile = (
            selected
            if isinstance(selected, str) and selected in profiles
            else _matching_profile(agent.permission_rules, profiles)
        )
    return {
        "profiles": [
            {
                "name": name,
                "builtin": name in BUILTIN_PERMISSION_PROFILES,
                "restricted": rules.restricts,
            }
            for name, rules in profiles.items()
        ],
        "agent_profile": agent_profile,
        **_stored_choice(overrides),
    }


def _matching_profile(
    rules: PermissionRules | None, profiles: dict[str, PermissionRules]
) -> str | None:
    """The profile whose complete rules equal *rules*; the built-ins come first."""
    if rules is None or not rules.restricts:
        return "full_access"
    complete = resolve_rule_set(rules, PERMISSION_PRESETS["full_access"])
    return next((name for name, profile in profiles.items() if profile == complete), None)


def _stored_choice(overrides: AgentOverrides) -> JsonObject:
    """The Session's own access as stored: a profile name, rules of its own, its grants."""
    return {
        "session_profile": overrides.permission_profile,
        "session_rules": (
            dict(overrides.permission_rules) if overrides.permission_rules is not None else None
        ),
        **_grant_summary(overrides),
    }


def _grant_summary(overrides: AgentOverrides) -> JsonObject:
    result: JsonObject = {"session_grants": list(overrides.permission_grants)}
    if overrides.read_folders:
        result["session_read_folders"] = [
            {"computer_id": computer, "folder": folder}
            for computer, folders in overrides.read_folders.items()
            for folder in folders
        ]
    return result


async def _set_session_rules(state: Any, params: JsonObject) -> JsonObject:
    """Set the user's access for one Session, or ``null`` for the Agent's rules again.

    Takes either ``permission_rules`` or a ``profile`` name; the Session keeps
    a profile by name, so later edits of the profile reach it. Only the user
    can: an Agent's own call must not widen its Session. A choice that
    restricts nothing in every part (the Full Access profile) also removes an
    inherited ceiling (``ceiling_cleared``). A Run in progress applies the
    change at its next Tool round.
    """
    _reject_unsupported(
        params,
        {"agent_id", "session_id", "permission_rules", "profile"},
        "permissions.set_session_rules",
    )
    if current_principal() is None:
        raise RpcError("access_denied", "Only the user can change a Session's permission rules.")
    if ("permission_rules" in params) == ("profile" in params):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params needs either permission_rules or profile")
    address = _required_session_address(params)
    resolver = state.runtime.agent_resolver
    rules = params.get("permission_rules")
    profile = params.get("profile")
    if "profile" in params:
        names = await state.runtime.chat_sessions.run_async(resolver.permission_profile_names)
        if not isinstance(profile, str) or profile not in names:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                f"params.profile must be one of {', '.join(names)}",
            )
    try:
        if rules is not None:
            normalize_permission_rules(rules)
    except PermissionRulesError as error:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(error)) from error
    try:
        overrides, cleared = await state.runtime.chat_sessions.run_async(
            lambda: resolver.set_session_permission_rules(address, rules, profile=profile)
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    publish_session_changed(state, address)
    return {**_stored_choice(overrides), "ceiling_cleared": cleared}


async def _clear_session_ceiling(state: Any, params: JsonObject) -> JsonObject:
    """Remove the ceiling a Session inherited from a Cron job or a parent Session.

    Only the user lifts limits a Session keeps for all later Runs; a Run in
    progress applies the change at its next Tool round. ``cleared`` is false
    when the Session stored none.
    """
    _reject_unsupported(params, {"agent_id", "session_id"}, "permissions.clear_session_ceiling")
    if current_principal() is None:
        raise RpcError("access_denied", "Only the user can lift a Session's inherited limits.")
    address = _required_session_address(params)
    try:
        cleared: bool = await state.runtime.chat_sessions.run_async(
            state.runtime.agent_resolver.clear_session_ceiling, address
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    if cleared:
        publish_session_changed(state, address)
    return {"cleared": cleared}


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


async def _pending_approvals(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, rpc_request_fields("permissions.pending"), "permissions.pending")
    return cast(JsonObject, state.chat_runs.approvals_snapshot())


async def _resolve_approval(state: Any, params: JsonObject) -> JsonObject:
    """Answer one approval request; ``remember`` also allows its rules for the whole Session.

    ``remember`` (with ``allow`` only) lists Session Grants, usually the
    request's ``session_grant`` suggestion, possibly edited by the user. They
    are stored before the call goes on, so its Tool's recheck and every later
    call of the Session see them.
    """
    if current_principal() is None:
        raise RpcError("access_denied", "Only the user can resolve an approval request.")
    _reject_unsupported(
        params, {"id", "decision", "remember", "read_folder"}, "permissions.resolve"
    )
    request_id = _required_string(params, "id")
    decision = _required_string(params, "decision")
    if decision not in {"allow", "reject"}:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "decision must be allow or reject")
    remember = params.get("remember")
    folder = _optional_string(params, "read_folder")
    if remember is None and folder is None:
        resolved = state.chat_runs.resolve_approval(request_id, decision)
    else:
        if decision != "allow" or (folder is not None and remember is not None):
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST, "Choose one Session grant with decision allow."
            )
        rules: list[str] = []
        if remember is not None:
            try:
                rules = [rule.text for rule in normalize_session_grants(remember)]
            except PermissionRulesError as error:
                raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.remember: {error}") from error
            if not rules:
                raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.remember names no rule")

        async def save(request: JsonObject) -> None:
            if folder is not None and folder not in (request.get("session_grant") or {}).get(
                "folders", ()
            ):
                raise RpcError(
                    RPC_ERROR_INVALID_REQUEST, "Choose a read folder offered for this request."
                )
            address = SessionAddress(
                request.get("project_id"), request["agent_id"], request["session_id"]
            )
            try:
                await state.runtime.chat_sessions.run_async(
                    state.runtime.agent_resolver.change_session_permission_grants,
                    address,
                    add=rules,
                    read_folder=folder,
                    computer_id=request.get("execution", {}).get("computer_id", "server"),
                )
            except Exception as exc:
                raise _map_expected_error(exc) from exc
            publish_session_changed(state, address)

        resolved = await state.chat_runs.remember_approval(
            request_id, save, folder_read=folder is not None
        )
    if not resolved:
        raise RpcError(
            "approval_not_found", "The approval request is no longer pending", routine=True
        )
    return {"resolved": True}


async def _remove_session_grants(state: Any, params: JsonObject) -> JsonObject:
    """Take back Session Grants; a Run in progress applies it at its next Tool round."""
    _reject_unsupported(
        params,
        {"agent_id", "session_id", "rules", "read_folder", "computer_id"},
        "permissions.remove_session_grants",
    )
    if current_principal() is None:
        raise RpcError("access_denied", "Only the user can change a Session's permission rules.")
    address = _required_session_address(params)
    folder = _optional_string(params, "read_folder")
    if ("rules" in params) == (folder is not None) or ("computer_id" in params and folder is None):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "Choose rules or a read folder to remove.")
    rules = params.get("rules", [])
    try:
        normalize_session_grants(rules)
    except PermissionRulesError as error:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.rules: {error}") from error
    try:
        overrides = await state.runtime.chat_sessions.run_async(
            state.runtime.agent_resolver.change_session_permission_grants,
            address,
            remove=rules,
            remove_folder=folder,
            computer_id=_optional_string(params, "computer_id") or "server",
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    publish_session_changed(state, address)
    return _grant_summary(overrides)


@contextmanager
def cli_permission_errors() -> Iterator[None]:
    """Surface refusals and expired shell grants consistently to private CLI calls."""
    try:
        yield
    except (
        ShellCliAccessError,
        VBotError,
        ToolAuthorityUnavailableError,
        ToolApprovalCancelledError,
    ) as exc:
        raise RpcError(
            "permission_denied", str(exc) or "The CLI request was cancelled.", routine=True
        ) from exc


async def _review_cli(state: Any, params: JsonObject) -> JsonObject:
    """Authorize the CLI's parsed operation in the shell call that launched it."""
    _reject_unsupported(
        params,
        {"token", "argv", "rights", "profile", "reads", "stdin"},
        "permissions.review_cli",
    )
    token = _required_string(params, "token")
    argv = params.get("argv")
    if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "argv must be a list of strings")
    rights = _required_bool(params, "rights")
    reads = params.get("reads")
    if not isinstance(reads, list) or not all(
        isinstance(read, list)
        and len(read) == 2
        and isinstance(read[0], str)
        and Path(read[0]).is_absolute()
        and isinstance(read[1], bool)
        for read in reads
    ):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST, "reads must contain absolute paths and subtree flags"
        )
    profile = params.get("profile")
    stdin = params.get("stdin")
    if not isinstance(profile, dict | None) or not isinstance(stdin, str | None):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "Invalid CLI review input")
    with cli_permission_errors():
        files = await state.runtime.shell_cli.review(
            token,
            argv=argv,
            reads=[(path, subtree) for path, subtree in reads],
            rights=rights,
            profile=profile,
            stdin=stdin,
        )
    return {"files": files}


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return the Permission Rules RPC handlers."""

    return {
        **_permission_profiles.method_handlers(),
        "permissions.pending": _pending_approvals,
        "permissions.resolve": _resolve_approval,
        "permissions.review_cli": _review_cli,
        "permissions.validate": _validate_permission_rules,
        "permissions.session": _session_permissions,
        "permissions.set_session_rules": _set_session_rules,
        "permissions.remove_session_grants": _remove_session_grants,
        "permissions.clear_session_ceiling": _clear_session_ceiling,
    }
