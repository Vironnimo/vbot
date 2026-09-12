"""Session methods."""

from __future__ import annotations

import inspect
from typing import Any

from core.channels import ChannelConfigError
from core.compaction import COMPACTION_POLICY_META_KEY
from core.projects import (
    InvalidAgentAddressError,
    format_agent_address,
    parse_agent_address,
)
from core.runs import RunAdmissionBlockedError
from core.sessions import (
    FORK_SOURCE_META_KEY,
    SESSION_FORK_ALWAYS_STRIP_META_KEYS,
    SESSION_FORK_CROSS_AGENT_STRIP_META_KEYS,
    SessionAddress,
    SessionListCursor,
    SessionListFilters,
)
from core.settings.normalizers import normalize_compaction_settings
from core.tools.terminal_manager import TerminalOwner
from core.utils.errors import StorageError
from core.utils.logging import get_logger
from server.events import (
    RESOURCE_KIND_AGENTS,
    RESOURCE_KIND_SESSIONS,
)
from server.rpc._session_workers import _SESSION_RPC_WORKERS
from server.rpc.agent_refs import _agent_reference_lock
from server.rpc.channel_methods import _channel_config_by_id
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import (
    RPC_ERROR_INVALID_REQUEST,
    RPC_ERROR_SESSION_BUSY,
    RpcError,
)
from server.rpc.event_bridge import publish_resource_changed
from server.rpc.runtime_access import _state_chat_runs
from server.rpc.validation import (
    _optional_bool,
    _optional_positive_integer,
    _optional_string,
    _reject_unsupported,
    _required_agent_address,
    _required_string,
    _validate_string_list,
)

JsonObject = dict[str, Any]

_LOGGER = get_logger("server.rpc.agents")

SESSION_LIST_DEFAULT_LIMIT = 100

SESSION_LIST_MAX_LIMIT = 100

SESSION_LIST_MAX_AGENT_BATCH = 100


def _session_address(
    agent_id: str, session_id: str, project_id: str | None = None
) -> SessionAddress:
    """Address one Session from RPC scalar fields."""
    return SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)


async def _session_io(
    manager: Any,
    async_name: str,
    sync_name: str,
    *arguments: Any,
    **keyword_arguments: Any,
) -> Any:
    """Use the Sessions async API while retaining narrow legacy test doubles."""
    async_method = getattr(manager, async_name, None)
    if inspect.iscoroutinefunction(async_method):
        return await async_method(*arguments, **keyword_arguments)
    return await _SESSION_RPC_WORKERS.run(
        getattr(manager, sync_name),
        *arguments,
        **keyword_arguments,
    )


async def _create_session(state: Any, params: JsonObject) -> JsonObject:
    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _optional_string(params, "session_id")
    make_current = _optional_bool(params, "make_current", default=False)

    def create_session() -> Any:
        state.runtime.agent_resolver.resolve_agent(project_id, agent_id)
        created = state.runtime.chat_sessions.create(
            agent_id,
            session_id=session_id,
            project_id=project_id,
        )
        if make_current and project_id is None:
            state.runtime.agents.update(agent_id, current_session_id=created.id)
        return created

    try:
        # One resolver seam validates both sources: identity agents through the
        # store, project agents through the team scan. The session is then created
        # under the matching anchor (identity dir vs. project anchor).
        session = await _SESSION_RPC_WORKERS.run(create_session)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    # Session creation is the single emit point for the sessions channel: it also
    # covers /new and /handoff, which create their session through here. Other
    # windows refresh their session list (and the make-current marking) for this
    # agent; they do NOT switch to the new session. Scoped to the agent so windows
    # on a different agent ignore it.
    publish_resource_changed(state, RESOURCE_KIND_SESSIONS, scope={"agent_id": agent_id})
    return {"agent_id": agent_id, "session_id": session.id}


async def _delete_session(state: Any, params: JsonObject) -> JsonObject:
    """Archive one session and report where the viewing accessor should land.

    Decisions baked in: the session is archived, not hard-deleted (#1,
    recoverable); deletion is refused while a run is active or queued on it (#4);
    the response carries ``next_session_id`` for #2 navigation; and the removed
    session is dropped from the active recall index immediately (#6). Channel-
    bound and sub-agent sessions need no special handling — a channel session
    simply resumes empty on the next inbound message, and an active sub-agent
    child is already covered by the per-session busy guard.
    """
    supported_fields = {"agent_id", "session_id"}
    _reject_unsupported(params, supported_fields, "session.delete")

    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _required_string(params, "session_id")
    deleting_current = False
    try:
        # One resolver seam validates both agent sources, exactly like
        # session.create, so an unknown agent fails before any file work.
        await _SESSION_RPC_WORKERS.run(
            state.runtime.agent_resolver.resolve_agent,
            project_id,
            agent_id,
        )
        chat_sessions = state.runtime.chat_sessions
        try:
            async with (
                _agent_reference_lock(state),
                _state_chat_runs(state).session_admission_guard(
                    _session_address(agent_id, session_id, project_id)
                ),
            ):
                _ensure_no_bootstrap_session_reference(state, agent_id, project_id, session_id)
                # Existence check under the guard: concurrent deletes cannot both
                # cross the storage boundary, and a missing Session still maps to
                # the ordinary domain error.
                await _session_io(
                    chat_sessions,
                    "get_async",
                    "get",
                    _session_address(agent_id, session_id, project_id),
                )
                # An identity agent tracks a current-session pointer; note when we
                # are deleting it so the re-aim is broadcast below.
                if project_id is None:
                    deleting_current = await _SESSION_RPC_WORKERS.run(
                        lambda: state.runtime.agents.get(agent_id).current_session_id == session_id
                    )
                await state.runtime.terminal_manager.close_scope(
                    TerminalOwner(project_id, agent_id, session_id)
                )
                await chat_sessions.archive(_session_address(agent_id, session_id, project_id))
                next_session_id = await _SESSION_RPC_WORKERS.run(
                    _resolve_post_delete_landing,
                    state,
                    agent_id,
                    session_id,
                    project_id,
                )
                await state.runtime.remove_session_from_recall(agent_id, session_id, project_id)
        except RunAdmissionBlockedError as exc:
            raise RpcError(
                RPC_ERROR_SESSION_BUSY,
                f"cannot delete session with an active or queued run: {session_id}",
            ) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    # Same emit point as session.create/rename: other windows on this agent
    # refresh their list (and the current marking), scoped to the agent.
    publish_resource_changed(state, RESOURCE_KIND_SESSIONS, scope={"agent_id": agent_id})
    # Re-aiming the identity current pointer is an agent-config change, so refresh
    # agent state in other windows (the current marking + return-to-current path).
    if deleting_current:
        publish_resource_changed(state, RESOURCE_KIND_AGENTS)
    _LOGGER.info(
        "Session archived (agent=%s session=%s)",
        format_agent_address(agent_id, project_id),
        session_id,
    )
    return {"agent_id": agent_id, "session_id": session_id, "next_session_id": next_session_id}


def _ensure_no_bootstrap_session_reference(
    state: Any,
    agent_id: str,
    project_id: str | None,
    session_id: str,
) -> None:
    service = getattr(state.runtime, "bootstrap_service", None)
    if service is None:
        return
    references = sorted(
        f"bootstrap:{job.id}"
        for job in service.list_jobs()
        if (
            job.agent_id == agent_id
            and job.project_id == project_id
            and job.session_id == session_id
            and getattr(job, "status", "active") != "completed"
        )
    )
    if references:
        raise RpcError(
            RPC_ERROR_SESSION_BUSY,
            f"cannot delete Session referenced by {', '.join(references)}",
        )


def _resolve_post_delete_landing(
    state: Any, agent_id: str, session_id: str, project_id: str | None
) -> str:
    """Return the session a viewing accessor should switch to after a delete (#2).

    The most-recently-active remaining session, or a fresh empty one when none
    remain. For an identity agent this goes through the shared
    ``reset_current_after_session_removed`` seam, which re-aims the current
    pointer when the deleted session was the current one and creates the fresh
    session when none remain — so the landing is that agent's resulting current
    and no session is ever created twice. A project config agent has no
    server-side current pointer, so the landing is derived directly from the
    remaining sessions (creating a fresh one when none remain).
    """
    chat_sessions = state.runtime.chat_sessions
    if project_id is None:
        agent = state.runtime.agents.reset_current_after_session_removed(agent_id, session_id)
        return str(agent.current_session_id)
    remaining = chat_sessions.list_with_metadata(agent_id, project_id)
    if remaining:
        newest = max(remaining, key=lambda session: session["last_active_at"])
        return str(newest["id"])
    return str(chat_sessions.create(agent_id, project_id=project_id).id)


async def _list_sessions(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(
        params,
        {
            "agent_id",
            "agent_ids",
            "limit",
            "cursor",
            "include_subagents",
            "include_memory_reflections",
            "include_skill_reflections",
            "include_cron",
            "required_session",
        },
        "session.list",
    )
    if ("agent_id" in params) == ("agent_ids" in params):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "session.list requires exactly one of params.agent_id or params.agent_ids",
        )
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

    limit = (
        _optional_positive_integer(params, "limit", max_value=SESSION_LIST_MAX_LIMIT)
        or SESSION_LIST_DEFAULT_LIMIT
    )
    cursor = _session_list_cursor(params.get("cursor"))
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
    filters = SessionListFilters(
        include_subagents=_optional_bool(params, "include_subagents", default=True),
        include_memory_reflections=_optional_bool(
            params, "include_memory_reflections", default=True
        ),
        include_skill_reflections=_optional_bool(params, "include_skill_reflections", default=True),
        include_cron=_optional_bool(params, "include_cron", default=True),
    )

    def load_sessions() -> tuple[list[JsonObject], SessionListCursor | None, int]:
        resolver = getattr(state.runtime, "agent_resolver", None)
        agents = getattr(state.runtime, "agents", None)
        inherited_policies: dict[tuple[str | None, str], JsonObject] = {}
        for _address, agent_id, project_id in parsed_addresses:
            if resolver is not None:
                agent = resolver.resolve_agent(project_id, agent_id)
            elif agents is not None:
                agent = agents.get(agent_id)
            else:
                agent = None
            own_policy = getattr(agent, "compaction_policy", None)
            inherited_policies[(project_id, agent_id)] = (
                dict(own_policy)
                if isinstance(own_policy, dict)
                else (
                    state.runtime.storage.load_compaction_settings()
                    if getattr(state.runtime, "storage", None) is not None
                    else normalize_compaction_settings(None)
                )
            )
        page = state.runtime.chat_sessions.list_summaries_page(
            [(project_id, agent_id) for _address, agent_id, project_id in parsed_addresses],
            limit=limit,
            cursor=cursor,
            filters=filters,
            required_address=required_address,
        )
        sessions = [dict(session) for session in page.sessions]
        run_manager = getattr(state.runtime, "chat_run_manager", None)
        for session in sessions:
            session_id = session.get("id")
            session_agent_id = session.pop("agent_id", None)
            session_project_id = session.pop("project_id", None)
            if not isinstance(session_agent_id, str):
                continue
            session["agent_address"] = format_agent_address(
                session_agent_id,
                session_project_id if isinstance(session_project_id, str) else None,
            )
            if isinstance(session_id, str) and run_manager is not None:
                active = run_manager.active_run(
                    agent_id=session_agent_id,
                    session_id=session_id,
                    project_id=(
                        session_project_id if isinstance(session_project_id, str) else None
                    ),
                )
                session["has_active_run"] = active is not None
            else:
                session["has_active_run"] = False
            override = session.pop(COMPACTION_POLICY_META_KEY, None)
            inherited_policy = inherited_policies[
                (
                    session_project_id if isinstance(session_project_id, str) else None,
                    session_agent_id,
                )
            ]
            session["compaction_policy_override"] = (
                dict(override) if isinstance(override, dict) else None
            )
            session["compaction_policy_effective"] = (
                dict(override) if isinstance(override, dict) else dict(inherited_policy)
            )
        return sessions, page.next_cursor, page.total_count

    try:
        sessions, next_cursor, total_count = await _SESSION_RPC_WORKERS.run(load_sessions)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {
        "sessions": sessions,
        "next_cursor": _session_list_cursor_payload(next_cursor),
        "total_count": total_count,
    }


def _session_list_cursor(value: Any) -> SessionListCursor | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "active_sort",
        "agent_id",
        "session_id",
    }:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.cursor must be a session.list cursor object",
        )
    active_sort = value.get("active_sort")
    if (
        not isinstance(active_sort, (int, float))
        or isinstance(active_sort, bool)
        or not (-10_000_000.0 < float(active_sort) < 10_000_000.0)
    ):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.cursor.active_sort is invalid")
    agent_id, project_id = _required_agent_address(value, "agent_id")
    session_id = _required_string(value, "session_id")
    return SessionListCursor(
        active_sort=float(active_sort),
        project_id=project_id,
        agent_id=agent_id,
        session_id=session_id,
    )


def _session_list_cursor_payload(cursor: SessionListCursor | None) -> JsonObject | None:
    if cursor is None:
        return None
    return {
        "active_sort": cursor.active_sort,
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
    """Return completion-only Session activity for a batch of Agent addresses."""
    _reject_unsupported(params, {"agent_ids"}, "session.activity_list")
    requested_addresses = _validate_string_list("agent_ids", params.get("agent_ids"))
    parsed_addresses: list[tuple[str, str, str | None]] = []
    seen_addresses: set[str] = set()
    for requested_address in requested_addresses:
        agent_id, project_id = _required_agent_address({"agent_id": requested_address}, "agent_id")
        canonical_address = format_agent_address(agent_id, project_id)
        if canonical_address in seen_addresses:
            continue
        seen_addresses.add(canonical_address)
        parsed_addresses.append((canonical_address, agent_id, project_id))

    def load_activity() -> list[JsonObject]:
        activity_by_agent: list[JsonObject] = []
        resolver = getattr(state.runtime, "agent_resolver", None)
        agents = getattr(state.runtime, "agents", None)
        for _address, agent_id, project_id in parsed_addresses:
            if resolver is not None:
                resolver.resolve_agent(project_id, agent_id)
            elif agents is not None:
                agents.get(agent_id)
            sessions = state.runtime.chat_sessions.list_completion_activity(agent_id, project_id)
            activity_by_agent.append(
                {
                    "agent_id": agent_id,
                    "project_id": project_id,
                    "sessions": sessions,
                }
            )
        return activity_by_agent

    try:
        activity = await _SESSION_RPC_WORKERS.run(load_activity)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"agents": activity}


async def _mark_session_read(state: Any, params: JsonObject) -> JsonObject:
    """Acknowledge the exact terminal Run rendered in one Session."""
    _reject_unsupported(params, {"agent_id", "session_id", "run_id"}, "session.mark_read")
    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _required_string(params, "session_id")
    run_id = _required_string(params, "run_id")
    try:
        await _SESSION_RPC_WORKERS.run(
            state.runtime.agent_resolver.resolve_agent,
            project_id,
            agent_id,
        )
        activity = await _session_io(
            state.runtime.chat_sessions,
            "mark_terminal_run_read_async",
            "mark_terminal_run_read",
            _session_address(agent_id, session_id, project_id),
            run_id,
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    return {
        "agent_id": format_agent_address(agent_id, project_id),
        "session_id": session_id,
        **activity,
    }


async def _fork_session(state: Any, params: JsonObject) -> JsonObject:
    """Copy a session 1:1 into a fresh id, optionally re-homed to another agent.

    A general capability: the fork is a normal, visible session that records its
    provenance (``fork_source``). Channel- and sub-agent bindings are always
    stripped so the copy is unbound; a cross-agent fork additionally drops the
    pinned skill catalog so the target re-pins its own. The strip policy lives in
    the server (``SESSION_FORK_*`` constants) so the sessions domain imports no
    chat/channel constant.
    """
    supported_fields = {"agent_id", "session_id", "target_agent_id"}
    _reject_unsupported(params, supported_fields, "session.fork")

    source_agent_id, source_project_id = _required_agent_address(params, "agent_id")
    session_id = _required_string(params, "session_id")
    target_explicit = "target_agent_id" in params
    target_agent_id, target_project_id = _optional_fork_target(
        params, source_agent_id, source_project_id
    )

    strip_meta_keys = SESSION_FORK_ALWAYS_STRIP_META_KEYS
    re_homed = (target_agent_id, target_project_id) != (source_agent_id, source_project_id)
    if re_homed:
        strip_meta_keys = strip_meta_keys | SESSION_FORK_CROSS_AGENT_STRIP_META_KEYS

    def resolve_endpoints() -> None:
        state.runtime.agent_resolver.resolve_agent(source_project_id, source_agent_id)
        if re_homed:
            state.runtime.agent_resolver.resolve_agent(target_project_id, target_agent_id)

    try:
        # Resolve both endpoints through the one seam so an unknown source or
        # target agent fails before any file work (mirrors session.create/delete).
        await _SESSION_RPC_WORKERS.run(resolve_endpoints)
        fork = await state.runtime.chat_sessions.fork(
            _session_address(source_agent_id, session_id, source_project_id),
            target_agent_id=target_agent_id if target_explicit else None,
            target_project_id=target_project_id if target_explicit else None,
            strip_meta_keys=strip_meta_keys,
        )
        fork_metadata = await _session_io(
            state.runtime.chat_sessions,
            "get_metadata_async",
            "get_metadata",
            _session_address(target_agent_id, fork.id, target_project_id),
        )
        fork_source = fork_metadata.get(FORK_SOURCE_META_KEY)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    # Same emit point as session.create: other windows on the *target* agent
    # refresh their session list so the fork shows immediately.
    publish_resource_changed(state, RESOURCE_KIND_SESSIONS, scope={"agent_id": target_agent_id})
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
    channel_id = _required_string(params, "channel_id")
    platform_conv_id = _required_string(params, "platform_conv_id")

    try:
        channel_service = state.runtime.channel_service
        channel_config = _channel_config_by_id(channel_service, channel_id)
        if channel_config.agent_id != agent_id:
            raise ChannelConfigError(
                f"Channel {channel_id} belongs to agent {channel_config.agent_id}, not {agent_id}"
            )
        await _session_io(
            state.runtime.chat_sessions,
            "get_async",
            "get",
            _session_address(agent_id, session_id),
        )
        metadata = dict(
            await _session_io(
                state.runtime.chat_sessions,
                "get_metadata_async",
                "get_metadata",
                _session_address(agent_id, session_id),
            )
        )
        previous_link = (
            metadata.get("source_channel_id"),
            metadata.get("platform"),
            metadata.get("platform_conv_id"),
        )
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
        await _session_io(
            state.runtime.chat_sessions,
            "set_metadata_async",
            "set_metadata",
            _session_address(agent_id, session_id),
            metadata,
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    if previous_link != (channel_id, channel_config.platform, platform_conv_id):
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

    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _required_string(params, "session_id")
    title = _session_title_param(params)
    try:
        stored_title = await _session_io(
            state.runtime.chat_sessions,
            "set_title_async",
            "set_title",
            _session_address(agent_id, session_id, project_id),
            title,
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    # A rename changes the session's list display, so other windows on this agent
    # refresh their session list — scoped to the agent like session.create.
    publish_resource_changed(state, RESOURCE_KIND_SESSIONS, scope={"agent_id": agent_id})
    return {"agent_id": agent_id, "session_id": session_id, "title": stored_title}


async def _set_session_compaction_policy(state: Any, params: JsonObject) -> JsonObject:
    """Set a full Session Policy override, or clear it back to live inheritance."""
    _reject_unsupported(
        params, {"agent_id", "session_id", "policy"}, "session.set_compaction_policy"
    )
    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _required_string(params, "session_id")
    policy = params.get("policy")
    try:
        from core.settings.normalizers import normalize_compaction_policy

        normalized = normalize_compaction_policy(policy) if policy is not None else None
        await _session_io(
            state.runtime.chat_sessions,
            "get_async",
            "get",
            _session_address(agent_id, session_id, project_id),
        )
        metadata = await _session_io(
            state.runtime.chat_sessions,
            "get_metadata_async",
            "get_metadata",
            _session_address(agent_id, session_id, project_id),
        )
        previous_override = metadata.get(COMPACTION_POLICY_META_KEY)
        if normalized is None:
            metadata.pop(COMPACTION_POLICY_META_KEY, None)
        else:
            metadata[COMPACTION_POLICY_META_KEY] = normalized
        await _session_io(
            state.runtime.chat_sessions,
            "set_metadata_async",
            "set_metadata",
            _session_address(agent_id, session_id, project_id),
            metadata,
        )
        agent = await _SESSION_RPC_WORKERS.run(
            state.runtime.agent_resolver.resolve_agent,
            project_id,
            agent_id,
        )
        own_policy = getattr(agent, "compaction_policy", None)
        inherited = (
            dict(own_policy)
            if isinstance(own_policy, dict)
            else await _SESSION_RPC_WORKERS.run(state.runtime.storage.load_compaction_settings)
        )
        effective = normalized or inherited
    except StorageError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    publish_resource_changed(state, RESOURCE_KIND_SESSIONS, scope={"agent_id": agent_id})
    if previous_override != normalized:
        _LOGGER.info(
            "Session compaction policy %s (agent=%s session=%s)",
            "set" if normalized is not None else "cleared",
            format_agent_address(agent_id, project_id),
            session_id,
        )
    return {
        "agent_id": format_agent_address(agent_id, project_id),
        "session_id": session_id,
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
        "session.mark_read": _mark_session_read,
        "session.fork": _fork_session,
        "session.delete": _delete_session,
        "session.rename": _rename_session,
        "session.set_compaction_policy": _set_session_compaction_policy,
        "session.link_channel": _link_session_to_channel,
    }
