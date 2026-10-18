"""Chat History, timeline, Reflection and Sub-Agent read projections."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from core.automation import LearningError
from core.chat import (
    latest_session_context_usage,
)
from core.chat.usage import with_context_window
from core.compaction import COMPACTION_POLICY_META_KEY, effective_compaction_policy
from core.projects import AgentResolutionError, format_agent_address
from core.runs import ChatRunManager, Run
from core.sessions import (
    ChatSession,
    SessionAddress,
    SessionChatHistorySnapshot,
)
from core.subagents.links import descendants
from core.tools.shell import (
    COMMAND_STATUS_NOTE_MARKER,
    COMMAND_STATUS_TOOL_NAMES,
    background_command_statuses,
)
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import (
    RPC_ERROR_INVALID_REQUEST,
    RPC_ERROR_RUN_NOT_FOUND,
    RpcError,
)
from server.rpc.payloads import (
    _resolve_context_window,
    _run_response,
    history_message,
)
from server.rpc.validation import (
    _optional_positive_integer,
    _optional_string,
    _reject_unsupported,
    _required_agent_address,
    _required_session_address,
    _required_string,
)

JsonObject = dict[str, Any]

MAX_CHAT_HISTORY_LIMIT = 500
# Shared bounded off-loop work for Chat RPC projections and file snapshots.
_CHAT_RPC_WORKERS = BoundedWorkerPool(name="chat-rpc", max_workers=4)
_LOGGER = get_logger("server.rpc.chat")


@dataclass(frozen=True)
class _ChatHistoryProjection:
    messages: list[JsonObject]
    background_command_statuses: JsonObject
    context_usage: JsonObject | None


@dataclass(frozen=True)
class _ChatHistoryRead:
    """One `chat.history` read: the snapshot and the Session facts the response adds.

    An ``unchanged`` snapshot carries no policy or reflection Runs.
    ``context_window`` is the window of the Agent's current Model, for a
    Context usage that records none (the Session has not answered since vBot
    started recording the window of the Model that answered).
    """

    history: SessionChatHistorySnapshot
    compaction_policy: JsonObject | None
    reflection_runs: list[JsonObject] | None
    context_window: int | None = None
    # The Sessions of the Session's Sub-Agents at every depth, each with the id
    # of the Session's own Sub-Agent above it; read for every page but a
    # ``before`` one.
    subagent_sessions: dict[SessionAddress, str] | None = None


async def _chat_run_result(state: Any, params: JsonObject) -> JsonObject:
    """Read the exact completed Run, even when the Session has already continued."""
    _reject_unsupported(params, {"agent_id", "session_id", "run_id"}, "chat.run_result")
    address = _required_session_address(params)
    run_id = _required_string(params, "run_id")

    def read() -> JsonObject:
        session = state.runtime.chat_sessions.get(address)
        result = session.load_run_result(run_id=run_id)
        content = (result.assistant.content or "") if result and result.assistant else ""
        return {
            "run_id": run_id,
            "found": result is not None,
            "content": content[-6000:],
            "truncated": len(content) > 6000,
        }

    try:
        response: JsonObject = await state.runtime.chat_sessions.run_async(read)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return response


async def _chat_history(state: Any, params: JsonObject, *, timeline: bool = False) -> JsonObject:
    """Read one History page of a Session.

    Without ``before`` or ``after`` the response is the newest page with the
    Session's whole-Session facts, reflection Runs and Compaction Policy.
    Without ``session_id`` an Identity Agent's current Session is read; an
    Identity Agent without one (a new conversation, no Session yet) returns an
    empty page with ``session_id: null``. An
    ``after`` read appends to the caller's page (``incremental``); its
    ``background_command_statuses`` then cover only the appended records and
    every live background command of the Session, for the caller to merge (a
    ``running`` it holds that the read leaves out has ended), while
    ``background_commands_still_running`` is always the complete list. An
    ``after`` read with nothing appended returns an empty page without
    ``session_usage``, ``context_usage`` or ``compaction_policy``: the
    caller's values stay current; its background fields report the live
    commands only. Every read but a ``before`` one lists in
    ``subagent_background_commands`` the background commands the Session's
    Sub-Agents still run, at every depth down to a Sub-Agent the user took over,
    which with everything below it is left out (complete each time). A ``before``
    page carries neither a background field nor the Compaction Policy.
    """
    supported_fields = {"agent_id", "session_id", "limit", "before", "after"}
    if timeline:
        supported_fields.add("child_limit")
    _reject_unsupported(params, supported_fields, "chat.history")

    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _optional_string(params, "session_id")
    limit = _optional_positive_integer(params, "limit", max_value=MAX_CHAT_HISTORY_LIMIT)
    before = _optional_string(params, "before")
    after = _optional_string(params, "after")
    child_limit = (
        _optional_positive_integer(params, "child_limit", max_value=200) or 0 if timeline else 0
    )
    try:
        if session_id is None:
            # A project Session has no anchor-level current pointer (the config
            # agent carries none), so it must be named.
            if project_id is not None:
                raise RpcError(
                    RPC_ERROR_INVALID_REQUEST,
                    "params.session_id is required for a project agent address",
                )
            session_id = await state.runtime.chat_sessions.run_async(
                _current_session_id, state, agent_id
            )
            if session_id is None:
                empty_response = _empty_chat_history(agent_id)
                if timeline:
                    from server.timeline import project_timeline_history

                    return project_timeline_history(empty_response)
                return empty_response
        address = SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)
        chat_runs: ChatRunManager = state.chat_runs
        while True:
            active_run_object = chat_runs.active_run(
                agent_id=agent_id, session_id=session_id, project_id=project_id
            )
            # Capture running reviews before the durable read. If one finishes
            # during that read, its persisted terminal status wins.
            active_reviews = (
                _active_reflection_runs(state) if before is None and after is None else None
            )
            # The Sub-Agent tree walk costs milliseconds for a large tree; skip it
            # when no other Session has a background command it could find.
            subagent_walk = (
                before is None
                and state.runtime.command_manager.has_unfinished_background(besides=address)
            )
            read = await state.runtime.chat_sessions.run_async(
                _read_chat_history,
                state,
                address,
                limit=limit,
                before=before,
                after=after,
                active_reviews=active_reviews,
                subagent_walk=subagent_walk,
                complete_runs=timeline,
            )
            latest_run = chat_runs.active_run(
                agent_id=agent_id, session_id=session_id, project_id=project_id
            )
            if latest_run is active_run_object:
                break
        # Freeze the live projection at the same boundary as durable history.
        # A completion during projection must not turn an earlier page into idle history.
        live_timeline = None
        if timeline and active_run_object is not None and before is None:
            from server.timeline import capture_run_timeline

            live_timeline = capture_run_timeline(active_run_object)
        active_run = (
            await _run_response(
                active_run_object,
                sse_url=f"/api/runs/{active_run_object.id}/events",
                file_delivery=state.file_delivery,
            )
            if active_run_object is not None
            else None
        )
        history = read.history
        projection = (
            None
            if history.unchanged
            else await _CHAT_RPC_WORKERS.run(
                _project_chat_history,
                history,
                file_delivery=state.file_delivery,
            )
        )
        reflection_runs = (
            None
            if read.reflection_runs is None
            else await _with_learning_outcomes(state, address, read.reflection_runs)
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    response: JsonObject = {
        "agent_id": agent_id,
        "session_id": session_id,
        "messages": [] if projection is None else projection.messages,
        "history_generation": history.generation_id,
        "runs": list(history.runs),
        "next_after": history.after_cursor,
        "incremental": history.incremental,
        "history_reset": after is not None and not history.incremental,
        "has_newer": history.has_newer,
        "has_more": history.page.has_more,
    }
    if history.page.before_cursor is not None:
        response["next_before"] = history.page.before_cursor
    if projection is not None:
        # Whole-session provider-reported token fields: the page above may be
        # a slice, but these always cover the full transcript.
        response["session_usage"] = history.session_usage
        context_usage = (
            active_run_object.terminal_payload_extras.get("context_usage")
            if active_run_object is not None
            else None
        )
        # Present (possibly null) whenever it was read; an absent field keeps
        # the caller's value.
        if not isinstance(context_usage, dict):
            context_usage = projection.context_usage
        if context_usage is not None and "context_window" not in context_usage:
            context_usage = with_context_window(context_usage, read.context_window)
        response["context_usage"] = context_usage
    if before is None:
        statuses, still_running = _current_command_statuses(
            state,
            address,
            {} if projection is None else projection.background_command_statuses,
        )
        response["background_command_statuses"] = statuses
        response["background_commands_still_running"] = still_running
        response["subagent_background_commands"] = _subagent_background_commands(
            state, read.subagent_sessions or {}
        )
    if reflection_runs is not None:
        response["reflection_runs"] = reflection_runs
    if read.compaction_policy is not None:
        response["compaction_policy"] = read.compaction_policy
    if active_run is not None:
        response["active_run"] = active_run
    if timeline:
        from server.rpc.payloads import remove_opaque_provider_metadata_async
        from server.timeline import project_timeline_history

        if live_timeline is not None:
            live_timeline = await remove_opaque_provider_metadata_async(
                live_timeline, file_delivery=state.file_delivery
            )
        if active_run is not None:
            active_run.pop("events", None)
            if child_limit:
                active_run["timeline_url"] += f"?child_limit={child_limit}"
        return await _CHAT_RPC_WORKERS.run(
            project_timeline_history, response, live_timeline, child_limit
        )
    return response


async def _chat_timeline(state: Any, params: JsonObject) -> JsonObject:
    """Read authoritative semantic conversation elements and Session facts."""
    return await _chat_history(state, params, timeline=True)


async def _chat_timeline_children(state: Any, params: JsonObject) -> JsonObject:
    from server.timeline import timeline_children

    _reject_unsupported(
        params,
        {"agent_id", "session_id", "run_id", "before", "limit", "history_generation"},
        "chat.timeline_children",
    )
    address = _required_session_address(params)
    run_id = _required_string(params, "run_id")
    before = _required_string(params, "before")
    generation = _required_string(params, "history_generation")
    limit = _optional_positive_integer(params, "limit", max_value=200) or 100
    try:
        return await timeline_children(
            state,
            address,
            run_id,
            before,
            limit,
            generation,
        )
    except ValueError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def _read_chat_history(
    state: Any,
    address: SessionAddress,
    *,
    limit: int | None,
    before: str | None,
    after: str | None,
    active_reviews: list[Run] | None,
    subagent_walk: bool,
    complete_runs: bool = False,
) -> _ChatHistoryRead:
    """Read one History page and the Session facts it carries in one Session-pool hop.

    *subagent_walk* reads the Sessions of the Session's Sub-Agents too.
    """
    session = state.runtime.chat_sessions.get(address)
    history = session.read_chat_history_snapshot(
        limit=limit,
        before=before,
        after=after,
        excluded_roles=("note", "history_edit"),
        complete_run_segment=True,
        complete_runs=complete_runs,
        background_tool_names=COMMAND_STATUS_TOOL_NAMES if before is None else (),
        background_note_marker=COMMAND_STATUS_NOTE_MARKER if before is None else None,
        skip_unchanged=True,
    )
    subagent_sessions = _subagent_sessions(state, address) if subagent_walk else None
    if history.unchanged:
        return _ChatHistoryRead(history, None, None, subagent_sessions=subagent_sessions)
    agent = _session_agent(state, address) if before is None else None
    return _ChatHistoryRead(
        history,
        _session_compaction_policy(state, address, agent) if agent is not None else None,
        None if active_reviews is None else _read_reflection_runs(state, session, active_reviews),
        _resolve_context_window(state, str(getattr(agent, "model", "") or ""))
        if agent is not None
        else None,
        subagent_sessions,
    )


def _subagent_sessions(state: Any, address: SessionAddress) -> dict[SessionAddress, str]:
    """The Sessions of *address*'s Sub-Agents at every depth, each with the id of
    *address*'s own Sub-Agent above it. Blocking: reads the Sub-Agent links.

    The walk ends at every Sub-Agent the user took over: that Session and those
    below it belong to the user, and the Session's Stop all leaves them running.
    """
    above: dict[SessionAddress, str] = {}
    # Breadth first: a Parent's Session is known before its children's.
    for link in descendants(state.runtime.chat_sessions, address):
        direct = link.id if link.parent == address else above.get(link.parent)
        if direct is not None:
            above[link.session] = direct
    return above


def _session_agent(state: Any, address: SessionAddress) -> Any | None:
    """Return the Session's Agent, ``None`` when it no longer resolves.

    A Session whose Agent no longer resolves through the ordinary addressing
    (a removed Team member, a temporary Session) stays readable; the facts
    that depend on its Agent are reported as unknown by omitting them.
    """
    try:
        return state.runtime.agent_resolver.resolve_agent(address.project_id, address.agent_id)
    except AgentResolutionError as exc:
        _LOGGER.debug(
            "Agent unavailable for history (agent=%s session=%s): %s",
            format_agent_address(address.agent_id, address.project_id),
            address.session_id,
            exc,
        )
        return None


def _session_compaction_policy(state: Any, address: SessionAddress, agent: Any) -> JsonObject:
    """Return the Session's effective Compaction Policy (Session -> Agent -> global).

    Accessors use it to relate Current Context Usage to automatic Compaction.
    """
    session_policy = state.runtime.chat_sessions.metadata_value(address, COMPACTION_POLICY_META_KEY)
    return effective_compaction_policy(
        session_policy,
        getattr(agent, "compaction_policy", None),
        state.runtime.storage.load_compaction_settings,
    )


def _active_reflection_runs(state: Any) -> list[Run]:
    """Capture live reviews; the worker resolves their canonical source lineage."""
    chat_runs: ChatRunManager = state.chat_runs
    return [run for run in chat_runs.active_runs() if run.source_session_id]


def _read_reflection_runs(
    state: Any, session: ChatSession, active_reviews: list[Run]
) -> list[JsonObject]:
    reviews = (
        set(state.runtime.chat_sessions.review_forks(session.address, recursive=False))
        if active_reviews
        else set()
    )
    rows = {
        (run.project_id, run.agent_id, run.session_id, run.id): {
            "run_id": run.id,
            "session_id": run.session_id,
            "agent_id": run.agent_id,
            "project_id": run.project_id,
            "run_kind": run.run_kind.value,
            "status": "running",
            "started_at": run.created_at,
        }
        for run in active_reviews
        if SessionAddress(run.project_id, run.agent_id, run.session_id) in reviews
    }
    rows.update(
        {
            (row["project_id"], row["agent_id"], row["session_id"], row["run_id"]): row
            for row in session.reflection_runs()
        }
    )
    return list(rows.values())


async def _with_learning_outcomes(
    state: Any, address: SessionAddress, rows: list[JsonObject]
) -> list[JsonObject]:
    """Read changes from each review's original Identity Agent, including after a source move."""
    by_agent: dict[str, list[str]] = {}
    for row in rows:
        if row.get("status") != "running" and row.get("project_id") is None:
            by_agent.setdefault(str(row["agent_id"]), []).append(str(row["run_id"]))
    outcomes: dict[tuple[str, str], JsonObject] = {}
    for agent_id, run_ids in by_agent.items():
        try:
            summaries = await _CHAT_RPC_WORKERS.run(
                state.runtime.learning_changes.summaries, agent_id, run_ids
            )
        except LearningError as exc:
            _LOGGER.warning(
                "Review outcomes unavailable (agent=%s session=%s): %s",
                agent_id,
                address.session_id,
                exc,
            )
            continue
        outcomes.update(
            {(agent_id, run_id): summary.to_dict() for run_id, summary in summaries.items()}
        )
    return [
        {**row, "outcome": outcome}
        if (outcome := outcomes.get((str(row["agent_id"]), str(row["run_id"])))) is not None
        else row
        for row in rows
    ]


async def _chat_reflections(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"agent_id", "session_id"}, "chat.reflections")
    address = _required_session_address(params)
    # Capture running reviews before the durable read. If one finishes during
    # that read, its persisted terminal status wins over the active snapshot.
    active_reviews = _active_reflection_runs(state)

    def read() -> list[JsonObject]:
        session = state.runtime.chat_sessions.get(address)
        return _read_reflection_runs(state, session, active_reviews)

    try:
        rows = await state.runtime.chat_sessions.run_async(read)
        return {"reflection_runs": await _with_learning_outcomes(state, address, rows)}
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def _current_command_statuses(
    state: Any, address: SessionAddress, statuses: JsonObject
) -> tuple[JsonObject, list[str]]:
    """The folded statuses made current, and the commands whose processes still run.

    Every background command of the Session that the command manager knows
    reports its current status, also one whose hand-off result is not saved
    yet (its Tool batch still runs). A recorded ``running`` that is not one of
    them has no status: the command ended without a delivered result (vBot
    restarted), or it belongs to another Session, as in a fork, which shares
    its source's History but not its commands. The list names the Session's
    commands whose shell exited while processes they started still run.
    """
    live = state.runtime.command_manager.commands([address])
    current: JsonObject = {
        command_id: status for command_id, status in statuses.items() if status != "running"
    }
    for report in live:
        current[report.command_id] = report.status
    return current, [report.command_id for report in live if report.still_running]


def _subagent_background_commands(
    state: Any, sessions: dict[SessionAddress, str]
) -> list[JsonObject]:
    """The background commands that still run in the Sessions of *sessions*.

    Running ones, and ended ones whose processes still run: what the Session's
    Stop all would stop below it. Each names the Session's own Sub-Agent
    above the Session that runs it, so the caller can show it on that
    Sub-Agent's row and follow its live status by its Session.
    """
    if not sessions:
        return []
    return [
        {
            "subagent_id": sessions[report.owner],
            "session_id": report.owner.session_id,
            "command_id": report.command_id,
            "label": report.label,
        }
        for report in state.runtime.command_manager.commands(sessions.keys())
        if not report.finished
    ]


def _project_chat_history(
    history: SessionChatHistorySnapshot, *, file_delivery: Any
) -> _ChatHistoryProjection:
    page = history.page
    messages = [
        {
            **record,
            **({"editable": True} if message.id in page.editable_message_ids else {}),
            **({"history_sequence": page.record_sequences[index]} if page.record_sequences else {}),
            **({"history_run_id": page.record_run_ids[index]} if page.record_run_ids else {}),
        }
        for index, message in enumerate(page.messages)
        if (record := history_message(message, file_delivery=file_delivery)) is not None
    ]
    return _ChatHistoryProjection(
        messages=messages,
        background_command_statuses=background_command_statuses(history.background_records),
        context_usage=latest_session_context_usage(list(history.context_messages)),
    )


def _current_session_id(state: Any, agent_id: str) -> str | None:
    """An Identity Agent's History defaults to its current Session, if it has one."""
    return cast(str, state.runtime.agents.get(agent_id).current_session_id) or None


def _empty_chat_history(agent_id: str) -> JsonObject:
    """The History page of a new conversation, whose Session does not exist yet."""
    return {
        "agent_id": agent_id,
        "session_id": None,
        "messages": [],
        "history_generation": None,
        "runs": [],
        "next_after": None,
        "incremental": False,
        "history_reset": False,
        "has_newer": False,
        "has_more": False,
    }


async def _subagent_inspect(state: Any, params: JsonObject) -> JsonObject:
    supported_fields = {"id", "agent_id", "session_id"}
    _reject_unsupported(params, supported_fields, "subagent.inspect")
    work_id = _required_string(params, "id")
    address = _required_session_address(params)
    try:
        inspection = await state.runtime.subagents.inspect(
            address.agent_id,
            address.session_id,
            work_id,
            project_id=address.project_id,
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    if inspection is None:
        raise RpcError(RPC_ERROR_RUN_NOT_FOUND, f"sub-agent work not found: {work_id}")
    return cast(JsonObject, inspection)


async def _subagent_tree(state: Any, params: JsonObject) -> JsonObject:
    """List one Session's Sub-Agents at every depth, breadth first, as they are now.

    Each entry names its Session (``agent_id`` as a qualified Agent address,
    ``session_id``) and the ``parent_id`` of the Sub-Agent it works for,
    ``None`` for a direct Sub-Agent of the Session.
    """
    _reject_unsupported(params, {"agent_id", "session_id"}, "subagent.tree")
    address = _required_session_address(params)
    try:
        subagents = await state.runtime.subagents.subagents(address)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {
        "subagents": [
            {
                "id": subagent.id,
                "title": subagent.title,
                "agent_id": subagent.agent_id,
                "session_id": subagent.session.session_id,
                "parent_id": subagent.parent_id,
                "status": subagent.status,
            }
            for subagent in subagents
        ]
    }


async def _chat_work(state: Any, params: JsonObject) -> JsonObject:
    """Current stoppable work of the complete Session subtree, without History."""
    _reject_unsupported(params, {"agent_id", "session_id"}, "chat.work")
    address = _required_session_address(params)
    try:
        return cast(
            JsonObject,
            await state.runtime.subagents.work_snapshot(address, include_entries=True),
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
