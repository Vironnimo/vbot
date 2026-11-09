"""Session-id resolution and operator transcript reads for Session RPCs."""

from __future__ import annotations

from typing import Any

from core.projects import format_agent_address, parse_agent_address
from core.sessions import ChatSessionManager, SessionAddress, SessionListFilters
from core.sessions.errors import SessionNotFoundError
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.validation import (
    _optional_bool,
    _optional_positive_integer,
    _optional_string,
    _reject_unsupported,
    _required_string,
)

type JsonObject = dict[str, Any]


def resolve_session(
    state: Any,
    session_id: str,
    agent: str | None = None,
    *,
    agent_option: str = "--agent",
) -> SessionAddress:
    """Resolve exact ids on the Session worker; ambiguity never selects a target."""
    sessions: ChatSessionManager = state.runtime.chat_sessions
    if agent is not None:
        agent_id, project_id = parse_agent_address(agent)
        address = SessionAddress(project_id, agent_id, session_id)
        if sessions.summary(address) is None:
            raise RpcError("session_not_found", f"Session does not exist: {session_id}")
        return address
    matches = sessions.addresses_by_id(session_id)
    if not matches:
        raise RpcError("session_not_found", f"Session does not exist: {session_id}")
    if len(matches) > 1:
        candidates = [format_agent_address(item.agent_id, item.project_id) for item in matches]
        raise RpcError(
            "session_id_ambiguous",
            f"Session id {session_id} matches {len(matches)} Sessions. "
            f"Select its owner with {agent_option}: " + ", ".join(candidates[:20]),
            data={"session_id": session_id, "agents": candidates[:20]},
        )
    return matches[0]


async def show_session(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(
        params,
        {"session_id", "agent_id", "summary", "cursor", "limit", "run_id", "entry", "offset"},
        "session.show",
    )
    session_id = _required_string(params, "session_id")
    agent = _optional_string(params, "agent_id")
    compact = _optional_bool(params, "summary", default=False)
    limit = _optional_positive_integer(params, "limit", max_value=100) or 20
    run_id = _optional_string(params, "run_id")
    cursor = params.get("cursor")
    if cursor is not None and not isinstance(cursor, dict):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST, "cursor must be the object from the preceding page"
        )
    entry = _nonnegative(params, "entry")
    offset = _nonnegative(params, "offset") or 0

    def read() -> JsonObject:
        from server.rpc.session_methods import _listed_agent_policies, _session_list_rows

        sessions: ChatSessionManager = state.runtime.chat_sessions
        address = resolve_session(state, session_id, agent)
        if cursor is not None and cursor.get("history_id") != sessions.history_id:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "Session history was restored; read again without --cursor",
            )
        page = sessions.inspection_page(
            address,
            summary=compact,
            limit=limit,
            cursor=cursor,
            run_id=run_id,
            entry=entry,
            offset=offset,
        )
        if page["next_cursor"] is not None:
            page["next_cursor"]["history_id"] = sessions.history_id
        policies = _listed_agent_policies(state)
        metadata = sessions.summary(address)
        if metadata is None or sessions.get(address).generation_id != page["generation_id"]:
            raise SessionNotFoundError("Session changed while reading; read it again")
        page["session"] = _session_list_rows(state, [metadata], policies)[0]
        if entry is None:
            children = sessions.list_summaries_page(
                None, limit=10, filters=SessionListFilters(parent=address)
            )
            page["subagents"] = _session_list_rows(state, children.sessions, policies)
            page["subagent_count"] = children.total_count
        return page

    sessions: ChatSessionManager = state.runtime.chat_sessions
    try:
        return await sessions.run_async(read)
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def _nonnegative(params: JsonObject, name: str) -> int | None:
    value = params.get(name)
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"{name} must be a non-negative integer")
    return value
