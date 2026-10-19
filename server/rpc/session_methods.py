"""Session methods.

Blocking work that reads or writes Sessions, including Agent store reads that
verify an Identity Agent's current-Session pointer, runs on the Session
database's worker pool (``ChatSessionManager.run_async``). Standalone Agent
resolution uses the resolver's own async API.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, cast

from core.agents import BUILTIN_AGENT_IDS
from core.archive import ArchiveSubjectInUseError
from core.channels import ChannelConfigError
from core.compaction import COMPACTION_POLICY_META_KEY, effective_compaction_policy
from core.projects import (
    AGENT_OVERRIDE_FIELDS,
    AgentOverrides,
    AgentResolutionError,
    InvalidAgentAddressError,
    format_agent_address,
    parse_agent_address,
)
from core.runs import RunAdmissionBlockedError
from core.sessions import (
    FORK_SOURCE_META_KEY,
    SessionAddress,
    SessionListCursor,
    SessionListFilters,
    SessionListPage,
    SessionProjectGrouping,
)
from core.subagents.links import descendants
from core.utils.errors import StorageError
from core.utils.logging import get_logger
from core.utils.timestamps import canonical_timestamp
from server.events import (
    RESOURCE_KIND_AGENTS,
    RESOURCE_KIND_PROJECTS,
)
from server.rpc.agent_refs import _agent_reference_lock, _guard_agent_lifecycle
from server.rpc.archive_methods import not_purged, purge_permanently
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import (
    RPC_ERROR_INVALID_REQUEST,
    RPC_ERROR_SESSION_BUSY,
    RPC_ERROR_SESSION_IN_USE,
    RpcError,
)
from server.rpc.event_bridge import (
    publish_archived_sessions,
    publish_resource_changed,
    publish_session_changed,
)
from server.rpc.payloads import _global_compaction_policy_loader
from server.rpc.validation import (
    _optional_agent_overrides,
    _optional_bool,
    _optional_positive_integer,
    _optional_string,
    _optional_working_project,
    _reject_unsupported,
    _required_agent_address,
    _required_session_address,
    _required_string,
    _validate_string_list,
)

JsonObject = dict[str, Any]

_LOGGER = get_logger("server.rpc.agents")

SESSION_LIST_DEFAULT_LIMIT = 100

SESSION_LIST_MAX_LIMIT = 100

SESSION_LIST_MAX_AGENT_BATCH = 100


async def _create_session(state: Any, params: JsonObject) -> JsonObject:
    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _optional_string(params, "session_id")
    make_current = _optional_bool(params, "make_current", default=False)
    overrides = _optional_agent_overrides(params, allow_clear=False)
    requested_project = _optional_working_project(params, project_id)
    chat_sessions = state.runtime.chat_sessions
    resolver = state.runtime.agent_resolver
    working_project_id: str | None = None

    def create_session() -> Any:
        created = chat_sessions.create(
            agent_id,
            session_id=session_id,
            project_id=project_id,
            actor="rpc",
            working_project_id=working_project_id if project_id is None else None,
            metadata=AgentOverrides(**(overrides or {})).capture(agent).session_metadata(),
        )
        if make_current and project_id is None:
            state.runtime.agents.update(agent_id, current_session_id=created.id)
        return created

    try:
        # One resolver seam validates both sources: identity agents through the
        # store, project agents through the team scan. The session is then created
        # under the matching anchor (identity dir vs. project anchor).
        agent = await resolver.resolve_agent_async(
            project_id,
            agent_id,
            **({"new_session_overrides": AgentOverrides(**overrides)} if overrides else {}),
        )
        # The Session works in this Project for its whole life: the requested
        # one (it must exist), the Workspace (null), or the Agent's default
        # Project; a Team Agent's Session in its Team's Project.
        working_project_id = await resolver.resolve_working_project_async(
            project_id, agent, requested=requested_project
        )
        session = await chat_sessions.run_async(create_session)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    # Session creation is the single emit point for the sessions channel: it also
    # covers /new and /handoff, which create their session through here. Other
    # windows refresh their session list (and the make-current marking) for this
    # agent; they do NOT switch to the new session. Scoped to the new Session so
    # windows not listing this Agent ignore it.
    publish_session_changed(state, SessionAddress(project_id, agent_id, session.id))
    response: JsonObject = {
        "agent_id": agent_id,
        "session_id": session.id,
        "working_project_id": working_project_id,
    }
    response["agent_overrides"] = AgentOverrides(**(overrides or {})).capture(agent).as_dict()
    return response


@_guard_agent_lifecycle
async def _set_session_agent_overrides(state: Any, params: JsonObject) -> JsonObject:
    """Set or clear some of one Session's Agent overrides; other fields keep their value."""
    _reject_unsupported(
        params,
        {"agent_id", "session_id", "agent_overrides", "remember"},
        "session.set_agent_overrides",
    )
    address = _required_session_address(params)
    changes = _optional_agent_overrides(params, allow_clear=True)
    if not changes:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.agent_overrides must name at least one of: " + ", ".join(AGENT_OVERRIDE_FIELDS),
        )
    remember = _optional_bool(params, "remember", default=False)
    try:
        if remember:
            overrides = await state.runtime.chat_sessions.run_async(
                state.runtime.agent_resolver.remember_session_settings, address, changes
            )
        else:
            overrides = await state.runtime.agent_resolver.update_session_overrides_async(
                address, changes
            )
        effective = await state.runtime.chat_sessions.run_async(
            state.runtime.agent_resolver.effective_config,
            address.project_id,
            address.agent_id,
            session_id=address.session_id,
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    publish_session_changed(state, address)
    if remember:
        publish_resource_changed(
            state, RESOURCE_KIND_PROJECTS if address.project_id else RESOURCE_KIND_AGENTS
        )
    _LOGGER.info(
        "Session agent overrides updated (agent=%s session=%s fields=%s)",
        format_agent_address(address.agent_id, address.project_id),
        address.session_id,
        ",".join(sorted(changes)),
    )
    return {
        "agent_id": format_agent_address(address.agent_id, address.project_id),
        "session_id": address.session_id,
        "agent_overrides": overrides.as_dict(),
        "effective": {name: effective[name] for name in AGENT_OVERRIDE_FIELDS if name in effective},
    }


async def _delete_session(state: Any, params: JsonObject) -> JsonObject:
    """Archive one session and report where the viewing accessor should land.

    Decisions baked in: the session moves into an archive entry, not hard-deleted
    (#1, recoverable), unless ``permanent`` purges that entry right after the
    archive; deletion is refused while a run is active or queued on it
    (#4) and while a Bootstrap job or Cron job pins it; the
    response carries ``next_session_id`` for #2 navigation; and the removed
    session is dropped from the active recall index immediately (#6). Channel-
    bound and sub-agent sessions need no special handling — a channel session
    simply resumes empty on the next inbound message, and an active sub-agent
    child is already covered by the per-session busy guard.
    """
    supported_fields = {"agent_id", "session_id", "permanent"}
    _reject_unsupported(params, supported_fields, "session.delete")

    address = _required_session_address(params)
    permanent = _optional_bool(params, "permanent", default=False)
    try:
        # Validate the owner, but archiving a conversation needs no runnable Model.
        await state.runtime.agent_resolver.resolve_agent_async(
            address.project_id, address.agent_id, require_model=False
        )
        try:
            async with _agent_reference_lock(state):
                outcome = await state.runtime.archive.archive_session(address)
        except ArchiveSubjectInUseError as exc:
            raise RpcError(
                RPC_ERROR_SESSION_IN_USE,
                f"cannot delete Session referenced by {', '.join(exc.references)}",
                data={"references": [dict(reference) for reference in exc.details]},
            ) from exc
        except RunAdmissionBlockedError as exc:
            raise RpcError(
                RPC_ERROR_SESSION_BUSY,
                "This Session or one of its Reflection Sessions has an active or queued Run. "
                "Wait until it finishes or stop it, then try again.",
            ) from exc
        next_session_id = outcome.next_session_id
        if address.project_id is not None:
            next_session_id = await state.runtime.chat_sessions.run_async(
                _project_agent_landing, state, address.agent_id, address.project_id
            )
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    # Same emit point as session.create/rename: other windows on this agent
    # refresh their list (and the current marking), scoped to the agent. The
    # scope names each archived Session and its landing so windows still
    # displaying or holding it release it and follow, like the deleting window.
    await publish_archived_sessions(
        state, outcome.addresses, landings={(address.project_id, address.agent_id): next_session_id}
    )
    # Re-aiming the identity current pointer is an agent-config change, so refresh
    # agent state in other windows (the current marking + return-to-current path).
    if outcome.was_current:
        publish_resource_changed(state, RESOURCE_KIND_AGENTS)
    # The purge runs after the reference lock is released; it needs none.
    purge = (
        await purge_permanently(state, outcome.entry_id)
        if permanent
        else {"archive_entry_id": outcome.entry_id, **not_purged()}
    )
    return {
        "agent_id": address.agent_id,
        "session_id": address.session_id,
        "next_session_id": next_session_id,
        **purge,
    }


def _project_agent_landing(state: Any, agent_id: str, project_id: str) -> str | None:
    """Return the session a viewing accessor of a Project Agent switches to after a delete (#2).

    A Project Agent has no server-side current pointer: the landing is its most
    recently active remaining session, or ``None`` when none remain (the
    accessor opens a new conversation; no Session is created for it). (An
    Identity Agent's landing is its resulting current Session, which the
    archive re-aims.)
    """
    newest_session_id = state.runtime.chat_sessions.newest_session_id(agent_id, project_id)
    return None if newest_session_id is None else str(newest_session_id)


_SESSION_LIST_FILTER_PARAMS = frozenset(
    {
        "include_subagents",
        "include_memory_reflections",
        "include_skill_reflections",
        "include_cron",
        "include_channels",
    }
)

# Newest Sessions each Project group of ``session.project_groups`` shows.
SESSION_PROJECT_GROUP_DEFAULT_LIMIT = 5

SESSION_PROJECT_GROUP_MAX_LIMIT = 20


def _session_list_filters(params: JsonObject) -> SessionListFilters:
    return SessionListFilters(
        include_subagents=_optional_bool(params, "include_subagents", default=True),
        include_memory_reflections=_optional_bool(
            params, "include_memory_reflections", default=True
        ),
        include_skill_reflections=_optional_bool(params, "include_skill_reflections", default=True),
        include_cron=_optional_bool(params, "include_cron", default=True),
        include_channels=_optional_bool(params, "include_channels", default=True),
    )


def _session_list_rows(
    state: Any,
    summaries: Sequence[JsonObject],
    agent_policy: Callable[[str | None, str], Any],
) -> list[JsonObject]:
    """Shape Session summaries as ``session.list`` rows.

    Each row names its owner as ``agent_address`` and carries whether a Run is
    active and its effective Compaction Policy.
    """
    load_global_policy = _global_compaction_policy_loader(state)
    run_manager = state.runtime.chat_run_manager
    sessions = [dict(summary) for summary in summaries]
    for session in sessions:
        session_id = session.get("id")
        session_agent_id = session.pop("agent_id", None)
        raw_project_id = session.pop("project_id", None)
        if not isinstance(session_agent_id, str):
            continue
        session_project_id = raw_project_id if isinstance(raw_project_id, str) else None
        session["agent_address"] = format_agent_address(session_agent_id, session_project_id)
        session["has_active_run"] = isinstance(session_id, str) and (
            run_manager.active_run(
                agent_id=session_agent_id, session_id=session_id, project_id=session_project_id
            )
            is not None
        )
        override = session.pop(COMPACTION_POLICY_META_KEY, None)
        session["compaction_policy_override"] = (
            dict(override) if isinstance(override, dict) else None
        )
        session["compaction_policy_effective"] = effective_compaction_policy(
            override, agent_policy(session_project_id, session_agent_id), load_global_policy
        )
    return sessions


def _listed_agent_policies(state: Any) -> Callable[[str | None, str], Any]:
    """Resolve the Compaction Policy of each Agent a cross-Agent list meets, once.

    A Session can outlive its Agent's definition (a Team member its Project no
    longer defines); its rows then fall back to the global policy.
    """
    resolver = state.runtime.agent_resolver
    policies: dict[tuple[str | None, str], Any] = {}

    def policy(project_id: str | None, agent_id: str) -> Any:
        key = (project_id, agent_id)
        if key not in policies:
            try:
                agent = resolver.resolve_agent(project_id, agent_id, require_model=False)
            except AgentResolutionError:
                policies[key] = None
            else:
                policies[key] = getattr(agent, "compaction_policy", None)
        return policies[key]

    return policy


def _session_project_grouping(state: Any) -> SessionProjectGrouping:
    """Group by the registered Projects; the built-in Agents' Sessions stay out."""
    return SessionProjectGrouping(
        project_ids=tuple(project.project_id for project in state.runtime.projects.list()),
        excluded_agent_ids=tuple(BUILTIN_AGENT_IDS.values()),
    )


def _session_list_page_payload(
    state: Any, page: SessionListPage, agent_policy: Callable[[str | None, str], Any]
) -> JsonObject:
    return {
        "sessions": _session_list_rows(state, page.sessions, agent_policy),
        "next_cursor": _session_list_cursor_payload(page.next_cursor),
        "total_count": page.total_count,
    }


async def _list_sessions(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(
        params,
        {
            "agent_id",
            "agent_ids",
            "working_project_id",
            "limit",
            "cursor",
            "required_session",
            *_SESSION_LIST_FILTER_PARAMS,
        },
        "session.list",
    )
    if sum(key in params for key in ("agent_id", "agent_ids", "working_project_id")) != 1:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "session.list requires exactly one of params.agent_id, params.agent_ids "
            "or params.working_project_id",
        )
    limit = (
        _optional_positive_integer(params, "limit", max_value=SESSION_LIST_MAX_LIMIT)
        or SESSION_LIST_DEFAULT_LIMIT
    )
    cursor = _session_list_cursor(params.get("cursor"))
    filters = _session_list_filters(params)
    if "working_project_id" in params:
        return await _list_project_sessions(state, params, limit, cursor, filters)
    requested_addresses = (
        [_required_string(params, "agent_id")]
        if "agent_id" in params
        else _validate_string_list("agent_ids", params.get("agent_ids"))
    )
    if len(requested_addresses) > SESSION_LIST_MAX_AGENT_BATCH:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.agent_ids must contain at most {SESSION_LIST_MAX_AGENT_BATCH} addresses",
        )
    parsed_addresses: list[tuple[str, str, str | None]] = []
    seen_addresses: set[str] = set()
    for requested_address in requested_addresses:
        agent_id, project_id = _required_agent_address({"agent_id": requested_address}, "agent_id")
        canonical_address = format_agent_address(agent_id, project_id)
        if canonical_address in seen_addresses:
            continue
        seen_addresses.add(canonical_address)
        parsed_addresses.append((canonical_address, agent_id, project_id))

    required_address = _session_list_required_address(params.get("required_session"))
    if required_address is not None:
        required_owner = format_agent_address(
            required_address.agent_id, required_address.project_id
        )
        if required_owner not in seen_addresses:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "params.required_session must belong to one of the listed Agent addresses",
            )

    def load_sessions() -> JsonObject:
        page = state.runtime.chat_sessions.list_summaries_page(
            [(project_id, agent_id) for _address, agent_id, project_id in parsed_addresses],
            limit=limit,
            cursor=cursor,
            filters=filters,
            required_address=required_address,
        )
        return _session_list_page_payload(state, page, _listed_agent_policies(state))

    try:
        return cast(JsonObject, await state.runtime.chat_sessions.run_async(load_sessions))
    except Exception as exc:
        raise _map_expected_error(exc) from exc


async def _list_project_sessions(
    state: Any,
    params: JsonObject,
    limit: int,
    cursor: SessionListCursor | None,
    filters: SessionListFilters,
) -> JsonObject:
    """One page of the Sessions of every Agent that work in one Project group.

    ``working_project_id`` names a registered Project, or is null for the
    Sessions that work in none (the Workspace, or a Project no longer
    registered); see ``session.project_groups``.
    """
    project_id = params["working_project_id"]
    if project_id is not None and (not isinstance(project_id, str) or not project_id.strip()):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.working_project_id must be a Project id or null",
        )
    if "required_session" in params:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.required_session lists by Agent; it excludes params.working_project_id",
        )

    def load_sessions() -> JsonObject:
        grouping = _session_project_grouping(state)
        if project_id is not None and project_id not in grouping.project_ids:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                f"params.working_project_id names no registered Project: {project_id}",
            )
        page = state.runtime.chat_sessions.list_project_page(
            grouping, project_id, limit=limit, cursor=cursor, filters=filters
        )
        return _session_list_page_payload(state, page, _listed_agent_policies(state))

    try:
        return cast(JsonObject, await state.runtime.chat_sessions.run_async(load_sessions))
    except RpcError:
        raise
    except Exception as exc:
        raise _map_expected_error(exc) from exc


async def _list_session_project_groups(state: Any, params: JsonObject) -> JsonObject:
    """The newest Sessions of every Agent, grouped by the Project they work in.

    One group per registered Project, also without Sessions, and a last group
    with ``project_id`` null for the Sessions that work in no registered
    Project (the Workspace, or a Project no longer registered). Groups with
    Sessions come first, newest activity first; empty ones follow in Project
    order. Global-scope Sessions of the built-in Agents are left out. Each
    group is a ``session.list`` page that ``session.list`` with
    ``working_project_id`` and its ``next_cursor`` continues.
    """
    _reject_unsupported(params, {"limit", *_SESSION_LIST_FILTER_PARAMS}, "session.project_groups")
    limit = (
        _optional_positive_integer(params, "limit", max_value=SESSION_PROJECT_GROUP_MAX_LIMIT)
        or SESSION_PROJECT_GROUP_DEFAULT_LIMIT
    )
    filters = _session_list_filters(params)

    def load_groups() -> JsonObject:
        grouping = _session_project_grouping(state)
        pages = state.runtime.chat_sessions.list_project_groups(
            grouping, limit=limit, filters=filters
        )
        agent_policy = _listed_agent_policies(state)
        project_pages = [(project_id, pages[project_id]) for project_id in grouping.project_ids]
        active = sorted(
            (entry for entry in project_pages if entry[1].sessions),
            key=lambda entry: str(entry[1].sessions[0].get("last_active_at") or ""),
            reverse=True,
        )
        empty = [entry for entry in project_pages if not entry[1].sessions]
        return {
            "groups": [
                {"project_id": project_id, **_session_list_page_payload(state, page, agent_policy)}
                for project_id, page in (*active, *empty, (None, pages[None]))
            ]
        }

    try:
        return cast(JsonObject, await state.runtime.chat_sessions.run_async(load_groups))
    except Exception as exc:
        raise _map_expected_error(exc) from exc


async def _get_session(state: Any, params: JsonObject) -> JsonObject:
    """Return one live Session's list summary by exact address, or ``None``.

    A point read for callers that need one Session's titles and provenance
    (such as its Parent Session link) without paging the Agent's Session list.
    The row matches a ``session.list`` row except that Compaction Policy and
    active-Run fields are not resolved.
    """
    _reject_unsupported(params, {"agent_id", "session_id"}, "session.get")
    address = _required_session_address(params)
    try:
        chat_sessions = state.runtime.chat_sessions
        summary = await chat_sessions.run_async(chat_sessions.summary, address)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    if summary is None:
        return {"session": None}
    session = dict(summary)
    session.pop("agent_id", None)
    session.pop("project_id", None)
    session.pop(COMPACTION_POLICY_META_KEY, None)
    session["agent_address"] = format_agent_address(address.agent_id, address.project_id)
    return {"session": session}


async def _session_change_stats(state: Any, params: JsonObject) -> JsonObject:
    """Return the lines one live Session's own Runs changed, with its changed files.

    ``change_stats`` is ``{files, added, removed, file_stats}`` or ``None``
    when the Session changed no file; ``session.list`` rows carry the totals.
    With ``include_subagents``, the Runs of its Sub-Agents at every depth count
    as its own and ``subagents`` names how many Sub-Agent Sessions were merged.
    """
    _reject_unsupported(
        params, {"agent_id", "session_id", "include_subagents"}, "session.change_stats"
    )
    address = _required_session_address(params)
    include_subagents = _optional_bool(params, "include_subagents", default=False)
    chat_sessions = state.runtime.chat_sessions

    def read() -> JsonObject:
        merged = (
            [link.session for link in descendants(chat_sessions, address)]
            if include_subagents
            else []
        )
        result: JsonObject = {"change_stats": chat_sessions.change_stats(address, merged=merged)}
        if include_subagents:
            result["subagents"] = len(merged)
        return result

    try:
        return cast(JsonObject, await chat_sessions.run_async(read))
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def _session_list_cursor(value: Any) -> SessionListCursor | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "last_activity_at",
        "agent_id",
        "session_id",
    }:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.cursor must be a session.list cursor object",
        )
    last_activity_at = value.get("last_activity_at")
    try:
        valid = isinstance(last_activity_at, str) and (
            canonical_timestamp(last_activity_at) == last_activity_at
        )
    except ValueError:
        valid = False
    if not valid:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.cursor.last_activity_at is invalid")
    agent_id, project_id = _required_agent_address(value, "agent_id")
    session_id = _required_string(value, "session_id")
    return SessionListCursor(
        last_activity_at=str(last_activity_at),
        project_id=project_id,
        agent_id=agent_id,
        session_id=session_id,
    )


def _session_list_cursor_payload(cursor: SessionListCursor | None) -> JsonObject | None:
    if cursor is None:
        return None
    return {
        "last_activity_at": cursor.last_activity_at,
        "agent_id": format_agent_address(cursor.agent_id, cursor.project_id),
        "session_id": cursor.session_id,
    }


def _session_list_required_address(value: Any) -> SessionAddress | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"agent_id", "session_id"}:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.required_session must contain agent_id and session_id",
        )
    agent_id, project_id = _required_agent_address(value, "agent_id")
    session_id = _required_string(value, "session_id")
    return SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)


async def _list_session_activity(state: Any, params: JsonObject) -> JsonObject:
    """Return completion-only Session activity for a batch of Agent addresses.

    One Session-store read covers every address. An address without live
    completed Sessions, including an unknown Agent, returns an empty list.
    """
    _reject_unsupported(params, {"agent_ids"}, "session.activity_list")
    requested_addresses = _validate_string_list("agent_ids", params.get("agent_ids"))
    scopes: list[tuple[str | None, str]] = []
    for requested_address in requested_addresses:
        agent_id, project_id = _required_agent_address({"agent_id": requested_address}, "agent_id")
        scopes.append((project_id, agent_id))
    scopes = list(dict.fromkeys(scopes))
    if not scopes:
        return {"agents": []}

    try:
        chat_sessions = state.runtime.chat_sessions
        activity = await chat_sessions.run_async(chat_sessions.list_completion_activity, scopes)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {
        "agents": [
            {
                "agent_id": agent_id,
                "project_id": project_id,
                "sessions": activity[(project_id, agent_id)],
            }
            for project_id, agent_id in scopes
        ]
    }


async def _mark_session_read(state: Any, params: JsonObject) -> JsonObject:
    """Acknowledge the exact terminal Run rendered in one Session."""
    _reject_unsupported(params, {"agent_id", "session_id", "run_id"}, "session.mark_read")
    address = _required_session_address(params)
    run_id = _required_string(params, "run_id")
    try:
        await state.runtime.agent_resolver.resolve_agent_async(
            address.project_id, address.agent_id, require_model=False
        )
        activity = await state.runtime.chat_sessions.mark_terminal_run_read_async(address, run_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    return {
        "agent_id": format_agent_address(address.agent_id, address.project_id),
        "session_id": address.session_id,
        **activity,
    }


async def _fork_session(state: Any, params: JsonObject) -> JsonObject:
    """Copy a session 1:1 into a fresh id, optionally re-homed to another agent.

    A general capability: the fork is a normal, visible session that records its
    provenance (``fork_source``). Sessions owns the fork policy: Channel and
    Sub-Agent bindings stay behind so the fork is unbound, and a fork into
    another Agent leaves the pinned Skill catalog behind so the target re-pins
    its own.
    """
    supported_fields = {"agent_id", "session_id", "target_agent_id"}
    _reject_unsupported(params, supported_fields, "session.fork")

    source_agent_id, source_project_id = _required_agent_address(params, "agent_id")
    session_id = _required_string(params, "session_id")
    source_address = SessionAddress(source_project_id, source_agent_id, session_id)
    target_explicit = "target_agent_id" in params
    target_agent_id, target_project_id = _optional_fork_target(
        params, source_agent_id, source_project_id
    )

    re_homed = (target_agent_id, target_project_id) != (source_agent_id, source_project_id)

    resolver = state.runtime.agent_resolver
    try:
        # Resolve both endpoints through the one seam so an unknown source or
        # target agent fails before any file work (mirrors session.create/delete).
        await resolver.resolve_agent_async(source_project_id, source_agent_id, require_model=False)
        if re_homed:
            await resolver.resolve_agent_async(
                target_project_id, target_agent_id, require_model=False
            )
        fork = await state.runtime.chat_sessions.fork(
            source_address,
            target_agent_id=target_agent_id if target_explicit else None,
            # Without a target the fork stays in the source's scope, which for a
            # Project Session is its Project.
            target_project_id=target_project_id,
        )
        fork_address = SessionAddress(target_project_id, target_agent_id, fork.id)
        fork_metadata = await state.runtime.chat_sessions.get_metadata_async(fork_address)
        fork_source = fork_metadata.get(FORK_SOURCE_META_KEY)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    # Same emit point as session.create: other windows on the *target* agent
    # refresh their session list so the fork shows immediately.
    publish_session_changed(state, fork_address)
    _LOGGER.info(
        "Session forked (source_agent=%s source_session=%s target_agent=%s target_session=%s)",
        format_agent_address(source_agent_id, source_project_id),
        session_id,
        format_agent_address(target_agent_id, target_project_id),
        fork.id,
    )
    return {
        "session": {
            "id": fork.id,
            "agent_id": format_agent_address(target_agent_id, target_project_id),
            "fork_source": fork_source,
        }
    }


def _optional_fork_target(
    params: JsonObject, source_agent_id: str, source_project_id: str | None
) -> tuple[str, str | None]:
    """Parse the optional ``target_agent_id`` fork destination, defaulting to source.

    Absent → fork within the source's own (agent, project). A malformed address is
    a client error surfaced as ``invalid_request`` (mirrors ``_required_agent_address``).
    """
    raw = _optional_string(params, "target_agent_id")
    if raw is None:
        return source_agent_id, source_project_id
    try:
        return parse_agent_address(raw)
    except InvalidAgentAddressError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc


async def _link_session_to_channel(state: Any, params: JsonObject) -> JsonObject:
    supported_fields = {"agent_id", "session_id", "channel_id", "platform_conv_id"}
    _reject_unsupported(params, supported_fields, "session.link_channel")

    agent_id = _required_string(params, "agent_id")
    session_id = _required_string(params, "session_id")
    address = SessionAddress(None, agent_id, session_id)
    channel_id = _required_string(params, "channel_id")
    platform_conv_id = _required_string(params, "platform_conv_id")

    try:
        channel_service = state.runtime.channel_service
        channel_config = await channel_service.get_channel(channel_id)
        if channel_config.agent_id != agent_id:
            raise ChannelConfigError(
                f"Channel {channel_id} belongs to agent {channel_config.agent_id}, not {agent_id}"
            )

        # The metadata mutation itself rejects a missing Session.
        def link_channel(metadata: JsonObject) -> None:
            metadata.update(
                {
                    "source_channel_id": channel_id,
                    "platform": channel_config.platform,
                    "platform_conv_id": platform_conv_id,
                    "last_reply_target": {
                        "channel_id": channel_id,
                        "platform_target": platform_conv_id,
                    },
                }
            )

        chat_sessions = state.runtime.chat_sessions
        previous, _updated = await chat_sessions.run_async(
            chat_sessions.mutate_metadata_with_previous,
            address,
            link_channel,
        )
        previous_link = (
            previous.get("source_channel_id"),
            previous.get("platform"),
            previous.get("platform_conv_id"),
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    if previous_link != (channel_id, channel_config.platform, platform_conv_id):
        publish_session_changed(state, address)
        _LOGGER.info(
            "Session linked to Channel (agent=%s session=%s channel=%s)",
            agent_id,
            session_id,
            channel_id,
        )
    return {"ok": True}


async def _rename_session(state: Any, params: JsonObject) -> JsonObject:
    """Set or clear a session's display title (the WebUI rename and ``/rename``).

    Thin over the single titling seam ``chat_sessions.set_title``: an empty (or
    absent) title clears it, so the session reverts to its automatic display.
    The response carries the stored title (``None`` when cleared) so the caller
    can confirm what was applied after normalization.
    """
    supported_fields = {"agent_id", "session_id", "title"}
    _reject_unsupported(params, supported_fields, "session.rename")

    address = _required_session_address(params)
    title = _session_title_param(params)
    try:
        stored_title = await state.runtime.chat_sessions.set_title_async(address, title)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    # A rename changes the session's list display, so other windows on this agent
    # refresh their session list — scoped to the Session like session.create.
    publish_session_changed(state, address)
    return {"agent_id": address.agent_id, "session_id": address.session_id, "title": stored_title}


async def _set_session_compaction_policy(state: Any, params: JsonObject) -> JsonObject:
    """Set a full Session Policy override, or clear it back to live inheritance."""
    _reject_unsupported(
        params, {"agent_id", "session_id", "policy"}, "session.set_compaction_policy"
    )
    address = _required_session_address(params)
    policy = params.get("policy")
    try:
        from core.settings.normalizers import normalize_compaction_policy

        normalized = normalize_compaction_policy(policy) if policy is not None else None
        agent = await state.runtime.agent_resolver.resolve_agent_async(
            address.project_id, address.agent_id, require_model=False
        )
        chat_sessions = state.runtime.chat_sessions

        # The metadata mutation itself rejects a missing Session.
        def set_policy(metadata: JsonObject) -> None:
            if normalized is None:
                metadata.pop(COMPACTION_POLICY_META_KEY, None)
            else:
                metadata[COMPACTION_POLICY_META_KEY] = normalized

        def apply_policy() -> tuple[Any, JsonObject]:
            # Resolve the effective Policy first: unreadable Compaction settings
            # reject the request before the override is stored.
            effective = effective_compaction_policy(
                normalized,
                getattr(agent, "compaction_policy", None),
                state.runtime.storage.load_compaction_settings,
            )
            previous, _updated = chat_sessions.mutate_metadata_with_previous(address, set_policy)
            return previous.get(COMPACTION_POLICY_META_KEY), effective

        previous_override, effective = await chat_sessions.run_async(apply_policy)
    except StorageError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    publish_session_changed(state, address)
    if previous_override != normalized:
        _LOGGER.info(
            "Session compaction policy %s (agent=%s session=%s)",
            "set" if normalized is not None else "cleared",
            format_agent_address(address.agent_id, address.project_id),
            address.session_id,
        )
    return {
        "agent_id": format_agent_address(address.agent_id, address.project_id),
        "session_id": address.session_id,
        "override": normalized,
        "effective": effective,
        "source": "session" if normalized is not None else "agent_or_global",
    }


def _session_title_param(params: JsonObject) -> str:
    """Read the rename title: any string, empty allowed (an empty title clears).

    Unlike ``_required_string``/``_optional_string`` this accepts the empty
    string, which is the explicit "clear the title" signal; an absent field is
    treated the same way.
    """
    value = params.get("title", "")
    if not isinstance(value, str):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.title must be a string")
    return value


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return the registered session RPC handlers."""
    return {
        "session.create": _create_session,
        "session.activity_list": _list_session_activity,
        "session.list": _list_sessions,
        "session.project_groups": _list_session_project_groups,
        "session.get": _get_session,
        "session.change_stats": _session_change_stats,
        "session.mark_read": _mark_session_read,
        "session.fork": _fork_session,
        "session.delete": _delete_session,
        "session.rename": _rename_session,
        "session.set_compaction_policy": _set_session_compaction_policy,
        "session.set_agent_overrides": _set_session_agent_overrides,
        "session.link_channel": _link_session_to_channel,
    }
