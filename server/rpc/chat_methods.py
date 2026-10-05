"""Chat RPC handlers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from core.automation import LearningError
from core.chat import (
    CommandExecutionContext,
    CommandOutcome,
    CommandResourceChange,
    NewSessionCommandContext,
    PreparedCommand,
    ReplySurface,
    latest_session_context_usage,
    queue_content_is_editable,
)
from core.chat.content_blocks import ContentBlock
from core.chat.file_mentions import expand_file_mentions, resolve_mention_root
from core.chat.usage import with_context_window
from core.compaction import COMPACTION_POLICY_META_KEY, effective_compaction_policy
from core.projects import AgentOverrides, AgentResolutionError, format_agent_address
from core.runs import ActiveRunError, ChatRunManager, QueuedRunItem, Run, RunCancelledError
from core.sessions import (
    ChatSession,
    SessionAddress,
    SessionChatHistorySnapshot,
    new_session_id,
)
from core.tools.shell import (
    COMMAND_STATUS_NOTE_MARKER,
    COMMAND_STATUS_TOOL_NAMES,
    background_command_statuses,
)
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool
from server.events import RESOURCE_KIND_AGENTS, RESOURCE_KIND_QUEUE
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import (
    RPC_ERROR_INVALID_REQUEST,
    RPC_ERROR_QUEUE_ITEM_NOT_FOUND,
    RPC_ERROR_QUEUE_ITEM_STEERING,
    RPC_ERROR_RUN_NOT_FOUND,
    RpcError,
)
from server.rpc.event_bridge import (
    _bridge_queued_item_to_event_bus,
    _bridge_run_to_event_bus,
    publish_resource_changed,
    publish_session_changed,
)
from server.rpc.payloads import (
    _queued_response,
    _resolve_context_window,
    _run_response,
    history_message,
)
from server.rpc.runtime_access import (
    _build_streaming_queue_update,
    _state_chat_runs,
    _state_command_dispatcher,
    _streaming_chat_loop,
)
from server.rpc.validation import (
    ChatInputOrigin,
    _optional_agent_overrides,
    _optional_chat_input_origin,
    _optional_file_mentions,
    _optional_positive_integer,
    _optional_string,
    _parse_chat_content,
    _reject_unsupported,
    _required_agent_address,
    _required_string,
)

JsonObject = dict[str, Any]
MAX_CHAT_HISTORY_LIMIT = 500
# CPU and file work of Chat RPCs: History projection (file capabilities) and
# ``@``-mention expansion. Session reads run on the Session database's pool.
_CHAT_RPC_WORKERS = BoundedWorkerPool(name="chat-rpc", max_workers=4)
_LOGGER = get_logger("server.rpc.chat")

WEBUI_REPLY_SURFACE = ReplySurface.webui()


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


def _publish_queue_changed(state: Any, agent_id: str, session_id: str) -> None:
    """Signal that one session's queue changed so other windows reload it live.

    Scoped to the affected session (bare agent id, as the queue is keyed) so
    windows on a different session ignore it. Only the browser/RPC send surface
    emits this — core enqueues (automation, channels, sub-agents) deliberately
    do not, keeping the chat core untouched; those windows still catch up on the
    next terminal event.
    """
    publish_resource_changed(
        state,
        RESOURCE_KIND_QUEUE,
        scope={"agent_id": agent_id, "session_id": session_id},
    )


async def _chat_run_result(state: Any, params: JsonObject) -> JsonObject:
    """Read the exact completed Run, even when the Session has already continued."""
    _reject_unsupported(params, {"agent_id", "session_id", "run_id"}, "chat.run_result")
    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _required_string(params, "session_id")
    run_id = _required_string(params, "run_id")

    def read() -> JsonObject:
        session = state.runtime.chat_sessions.get(SessionAddress(project_id, agent_id, session_id))
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


async def _chat_history(state: Any, params: JsonObject) -> JsonObject:
    """Read one History page of a Session.

    Without ``before`` or ``after`` the response is the newest page with the
    Session's whole-Session facts, reflection Runs and Compaction Policy.
    Without ``session_id`` an Identity Agent's current Session is read; an
    Identity Agent without one (a new conversation, no Session yet) returns an
    empty page with ``session_id: null``. An
    ``after`` read appends to the caller's page (``incremental``); its
    ``background_command_statuses`` then cover only the appended records, for the
    caller to merge. An ``after`` read with nothing appended returns an empty
    page without ``session_usage``, ``context_usage``,
    ``background_command_statuses`` or ``compaction_policy``: the caller's values
    stay current. A ``before`` page carries neither background statuses nor
    the Compaction Policy.
    """
    supported_fields = {"agent_id", "session_id", "limit", "before", "after"}
    _reject_unsupported(params, supported_fields, "chat.history")

    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _optional_string(params, "session_id")
    limit = _optional_positive_integer(params, "limit", max_value=MAX_CHAT_HISTORY_LIMIT)
    before = _optional_string(params, "before")
    after = _optional_string(params, "after")
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
                return _empty_chat_history(agent_id)
        address = SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)
        chat_runs = _state_chat_runs(state)
        while True:
            active_run_object = chat_runs.active_run(
                agent_id=agent_id, session_id=session_id, project_id=project_id
            )
            # Capture running reviews before the durable read. If one finishes
            # during that read, its persisted terminal status wins.
            active_reviews = (
                _active_reflection_runs(state, address)
                if before is None and after is None
                else None
            )
            read = await state.runtime.chat_sessions.run_async(
                _read_chat_history,
                state,
                address,
                limit=limit,
                before=before,
                after=after,
                active_reviews=active_reviews,
            )
            latest_run = chat_runs.active_run(
                agent_id=agent_id, session_id=session_id, project_id=project_id
            )
            if latest_run is active_run_object:
                break
        # Freeze the live projection at the same boundary as durable history.
        # A completion during projection must not turn an earlier page into idle history.
        active_run = (
            _run_response(
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
            response["background_command_statuses"] = _current_command_statuses(
                state, projection.background_command_statuses
            )
    if reflection_runs is not None:
        response["reflection_runs"] = reflection_runs
    if read.compaction_policy is not None:
        response["compaction_policy"] = read.compaction_policy
    if active_run is not None:
        response["active_run"] = active_run
    return response


def _read_chat_history(
    state: Any,
    address: SessionAddress,
    *,
    limit: int | None,
    before: str | None,
    after: str | None,
    active_reviews: list[Run] | None,
) -> _ChatHistoryRead:
    """Read one History page and the Session facts it carries in one Session-pool hop."""
    session = state.runtime.chat_sessions.get(address)
    history = session.read_chat_history_snapshot(
        limit=limit,
        before=before,
        after=after,
        excluded_roles=("note", "history_edit"),
        complete_run_segment=True,
        background_tool_names=COMMAND_STATUS_TOOL_NAMES if before is None else (),
        background_note_marker=COMMAND_STATUS_NOTE_MARKER if before is None else None,
        skip_unchanged=True,
    )
    if history.unchanged:
        return _ChatHistoryRead(history, None, None)
    agent = _session_agent(state, address) if before is None else None
    return _ChatHistoryRead(
        history,
        _session_compaction_policy(state, address, agent) if agent is not None else None,
        None if active_reviews is None else _read_reflection_runs(session, active_reviews),
        _resolve_context_window(state, str(getattr(agent, "model", "") or ""))
        if agent is not None
        else None,
    )


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


def _active_reflection_runs(state: Any, address: SessionAddress) -> list[Run]:
    """Active Review Runs of this Session.

    Review Runs execute in same-scope forks and carry the Session they examine.
    """
    return [
        run
        for run in _state_chat_runs(state).active_runs()
        if run.agent_id == address.agent_id
        and run.project_id == address.project_id
        and run.source_session_id == address.session_id
    ]


def _read_reflection_runs(session: ChatSession, active_reviews: list[Run]) -> list[JsonObject]:
    rows = {
        run.id: {
            "run_id": run.id,
            "session_id": run.session_id,
            "run_kind": run.run_kind.value,
            "status": "running",
            "started_at": run.created_at,
        }
        for run in active_reviews
    }
    rows.update({row["run_id"]: row for row in session.reflection_runs()})
    return list(rows.values())


async def _with_learning_outcomes(
    state: Any, address: SessionAddress, rows: list[JsonObject]
) -> list[JsonObject]:
    """Add each finished review's ``outcome``: what it changed in Memory and Skills.

    The counts are derived from the Agent's Memory and Skill histories by the
    review's Run id, so they stay current after an undo. Only Identity Agents
    learn; rows of a Project Session and rows whose histories cannot be read
    carry no outcome.
    """
    finished = [row["run_id"] for row in rows if row.get("status") != "running"]
    if address.project_id is not None or not finished:
        return rows
    try:
        summaries = await _CHAT_RPC_WORKERS.run(
            state.runtime.learning_changes.summaries, address.agent_id, finished
        )
    except LearningError as exc:
        _LOGGER.warning(
            "Review outcomes unavailable (agent=%s session=%s): %s",
            address.agent_id,
            address.session_id,
            exc,
        )
        return rows
    return [
        {**row, "outcome": summaries[row["run_id"]].to_dict()}
        if row["run_id"] in summaries
        else row
        for row in rows
    ]


async def _chat_reflections(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"agent_id", "session_id"}, "chat.reflections")
    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _required_string(params, "session_id")
    address = SessionAddress(project_id, agent_id, session_id)
    # Capture running reviews before the durable read. If one finishes during
    # that read, its persisted terminal status wins over the active snapshot.
    active_reviews = _active_reflection_runs(state, address)

    def read() -> list[JsonObject]:
        session = state.runtime.chat_sessions.get(address)
        return _read_reflection_runs(session, active_reviews)

    try:
        rows = await state.runtime.chat_sessions.run_async(read)
        return {"reflection_runs": await _with_learning_outcomes(state, address, rows)}
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def _current_command_statuses(state: Any, statuses: JsonObject) -> JsonObject:
    """Replace each recorded ``running`` status by the command's current one.

    A command no terminal runs any longer ended without a delivered result
    (vBot restarted), so it has no status to show.
    """
    terminals = state.runtime.terminal_manager
    current: JsonObject = {}
    for terminal_id, status in statuses.items():
        if status == "running":
            status = terminals.command_status(terminal_id)
            if status is None:
                continue
        current[terminal_id] = status
    return current


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
    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _required_string(params, "session_id")
    try:
        inspection = await state.runtime.subagents.inspect(
            agent_id,
            session_id,
            work_id,
            project_id=project_id,
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    if inspection is None:
        raise RpcError(RPC_ERROR_RUN_NOT_FOUND, f"sub-agent work not found: {work_id}")
    return cast(JsonObject, inspection)


def _command_output(outcome: CommandOutcome) -> str:
    if outcome.navigation is not None:
        return "action"
    if outcome.feedback is not None and outcome.feedback.kind == "detail":
        return "transient"
    return "toast"


def _command_change_key(change: CommandResourceChange) -> tuple[str, tuple[tuple[str, str], ...]]:
    return change.kind, tuple(sorted(change.scope.items()))


def _publish_command_change(state: Any, change: CommandResourceChange) -> None:
    publish_resource_changed(state, change.kind, scope=dict(change.scope) or None)


def _command_outcome_response(outcome: CommandOutcome, session_id: str | None) -> JsonObject:
    """Project a command outcome; ``session_id`` is the Session the command ran in.

    A command sent for a new Session runs in none unless it created one, and
    its response then carries no ``session_id``.
    """
    response: JsonObject = {
        "command_handled": True,
        "reply": outcome.feedback.text if outcome.feedback is not None else "",
        "output": _command_output(outcome),
    }
    if session_id is not None:
        response["session_id"] = session_id
    data: JsonObject = {"command": outcome.command, **dict(outcome.facts)}
    if outcome.navigation is not None:
        navigation = outcome.navigation
        if navigation.kind == "open_extension_page":
            data["navigation"] = {
                "kind": navigation.kind,
                "extension": navigation.extension,
                "page": navigation.page,
                "route": navigation.route,
            }
        elif navigation.kind == "new_session":
            data["navigation"] = {
                "kind": navigation.kind,
                "agent_id": format_agent_address(navigation.agent_id, navigation.project_id),
            }
        else:
            data.setdefault("session_id", navigation.session_id)
            data.setdefault(
                "agent_id",
                format_agent_address(navigation.agent_id, navigation.project_id),
            )
    if len(data) > 1:
        response["data"] = data
    return response


def _primary_command_run(outcome: CommandOutcome) -> Run | None:
    primary_runs = [
        command_run.run for command_run in outcome.runs if command_run.role == "primary"
    ]
    if len(primary_runs) > 1:
        raise ValueError("A command outcome may expose at most one primary Run")
    return primary_runs[0] if primary_runs else None


def _command_change_publisher(state: Any) -> Callable[[CommandResourceChange], None]:
    """Publish each distinct resource change of one command once."""
    emitted_changes: set[tuple[str, tuple[tuple[str, str], ...]]] = set()

    def on_change(change: CommandResourceChange) -> None:
        key = _command_change_key(change)
        if key in emitted_changes:
            return
        emitted_changes.add(key)
        _publish_command_change(state, change)

    return on_change


async def _execute_chat_command(
    state: Any,
    agent_id: str,
    session_id: str,
    prepared: PreparedCommand,
    *,
    project_id: str | None = None,
) -> Run | JsonObject:
    dispatcher = _state_command_dispatcher(state)
    on_change = _command_change_publisher(state)
    try:
        outcome = await dispatcher.execute(
            prepared,
            CommandExecutionContext(
                agent_id=agent_id,
                session_id=session_id,
                project_id=project_id,
                reply_surface=WEBUI_REPLY_SURFACE,
                on_change=on_change,
            ),
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    for change in outcome.resource_changes:
        on_change(change)
    primary_run = _primary_command_run(outcome)
    if primary_run is not None:
        return primary_run
    return _command_outcome_response(outcome, session_id)


async def _execute_new_session_chat_command(
    state: Any,
    agent_id: str,
    prepared: PreparedCommand,
    agent_overrides: AgentOverrides,
    *,
    project_id: str | None,
) -> Run | JsonObject:
    """Run a command sent for a new Session; the command decides whether it creates one."""
    on_change = _command_change_publisher(state)
    try:
        result = await _state_command_dispatcher(state).execute_for_new_session(
            prepared,
            NewSessionCommandContext(
                agent_id=agent_id,
                project_id=project_id,
                reply_surface=WEBUI_REPLY_SURFACE,
                agent_overrides=agent_overrides,
                on_change=on_change,
            ),
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    for change in result.outcome.resource_changes:
        on_change(change)
    if result.session_id is not None and project_id is None:
        await _mark_current_session(state, agent_id, result.session_id)
    primary_run = _primary_command_run(result.outcome)
    if primary_run is not None:
        return primary_run
    return _command_outcome_response(result.outcome, result.session_id)


async def _expand_content_file_mentions(
    state: Any,
    agent_id: str,
    project_id: str | None,
    session_id: str,
    content: str | list[ContentBlock],
    file_mentions: list[str],
) -> str | list[ContentBlock]:
    """Snapshot ``@``-mentioned files into the outgoing content, if any.

    Runs before Run start *and* before busy-session enqueue, so a queued message
    carries the files as they were when the user hit send. The root resolution
    reads the Agent (whose current-Session pointer it verifies) on the Session
    database's pool; the file I/O runs on the Chat RPC pool.
    """
    if not file_mentions:
        return content
    runtime = state.runtime
    try:
        root = await runtime.chat_sessions.run_async(
            resolve_mention_root, runtime, agent_id, project_id
        )
        return await _CHAT_RPC_WORKERS.run(
            expand_file_mentions,
            content,
            file_mentions,
            root=root,
            session_id=session_id,
            file_state=runtime.file_read_state,
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc


async def _mark_current_session(state: Any, agent_id: str, session_id: str) -> None:
    """Re-aim an identity agent's current-session pointer to the session the
    user just wrote to.

    Best-effort: the pointer is a convenience for re-opening the last active
    session after a restart, so a failure must never block the user's message.
    The agent store is the single writer; other windows learn about the new
    current marking through the agents channel.

    Skipped when the session is already current: re-marking the same session on
    every message would emit a redundant ``resource_changed(kind="agents")``
    signal that tears down the chat view in every connected window.
    """
    runtime = state.runtime
    agents = runtime.agents
    # Agent reads verify, and updates validate, the pointer against Sessions.
    chat_sessions = runtime.chat_sessions
    try:
        agent = await chat_sessions.run_async(agents.get, agent_id)
    except Exception as exc:
        _LOGGER.warning(
            "Failed to read agent for current-session mark (agent=%s): %s",
            agent_id,
            exc,
        )
        return
    if agent.current_session_id == session_id:
        return
    try:
        await chat_sessions.run_async(agents.update, agent_id, current_session_id=session_id)
    except Exception as exc:
        _LOGGER.warning(
            "Failed to mark current session (agent=%s session=%s): %s",
            agent_id,
            session_id,
            exc,
        )
    else:
        publish_resource_changed(state, RESOURCE_KIND_AGENTS)
        _LOGGER.debug(
            "Current session marked (agent=%s session=%s)",
            agent_id,
            session_id,
        )


async def _submit_chat(
    state: Any,
    params: JsonObject,
    *,
    streaming: bool,
) -> Run | JsonObject:
    """Submit one accessor chat request and return its immediate disposition.

    Commands and queued work already have complete RPC payloads, while a Run
    still needs the caller-specific response treatment: ``chat.send`` waits for
    its final message and ``chat.stream`` returns the SSE location immediately.
    Everything before that presentation split is one submission path so command
    dispatch, file snapshots, queue fallback, and busy-to-idle handling cannot
    drift between the two RPC methods. The target is an existing Session
    (``session_id``) or a new one (``new_session``, see :func:`_chat_target`).
    """

    agent_id, project_id = _required_agent_address(params, "agent_id")
    target = _chat_target(params)
    content = _parse_chat_content(params, "content")
    input_origin = _optional_chat_input_origin(params)
    file_mentions = _optional_file_mentions(params)

    prepared_command = _state_command_dispatcher(state).prepare(content)
    if isinstance(target, AgentOverrides):
        if prepared_command is not None:
            return await _execute_new_session_chat_command(
                state, agent_id, prepared_command, target, project_id=project_id
            )
        return await _start_chat_in_new_session(
            state,
            agent_id,
            project_id,
            content,
            target,
            input_origin=input_origin,
            file_mentions=file_mentions,
            streaming=streaming,
        )
    session_id = target
    if prepared_command is not None:
        return await _execute_chat_command(
            state,
            agent_id,
            session_id,
            prepared_command,
            project_id=project_id,
        )

    content = await _expand_content_file_mentions(
        state, agent_id, project_id, session_id, content, file_mentions
    )

    # A user message makes this session the agent's current one: after a server
    # restart the accessor re-opens the session the user last wrote to, not the
    # one they last viewed. Identity agents only — a project (config) agent has
    # no anchor-level current pointer. Runs triggered by automation, channels,
    # or sub-agents never pass through here, so they cannot move the pointer.
    if project_id is None:
        await _mark_current_session(state, agent_id, session_id)

    chat_loop = _streaming_chat_loop(state) if streaming else state.chat_loop
    try:
        if input_origin is None:
            run = await chat_loop.start_run(
                agent_id,
                content,
                session_id=session_id,
                reply_surface=WEBUI_REPLY_SURFACE,
                project_id=project_id,
            )
        else:
            run = await chat_loop.start_run(
                agent_id,
                content,
                session_id=session_id,
                input_origin=input_origin,
                reply_surface=WEBUI_REPLY_SURFACE,
                project_id=project_id,
            )
    except ActiveRunError:
        try:
            if input_origin is None:
                queued_item = await chat_loop.queue_run(
                    agent_id,
                    content,
                    session_id=session_id,
                    reply_surface=WEBUI_REPLY_SURFACE,
                    project_id=project_id,
                )
            else:
                queued_item = await chat_loop.queue_run(
                    agent_id,
                    content,
                    session_id=session_id,
                    input_origin=input_origin,
                    reply_surface=WEBUI_REPLY_SURFACE,
                    project_id=project_id,
                )
            started_run = _run_started_during_enqueue(queued_item)
        except Exception as exc:
            raise _map_expected_error(exc) from exc
        if started_run is None:
            _bridge_queued_item_to_event_bus(state, queued_item)
            _publish_queue_changed(state, agent_id, session_id)
            return _queued_response(queued_item, session_id)
        run = started_run
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    return run


def _chat_target(params: JsonObject) -> str | AgentOverrides:
    """Read where a chat submission goes: exactly one of two fields.

    ``session_id`` names an existing Session and is returned. ``new_session``
    is an object asking for a new Session that the submission creates; its
    optional ``agent_overrides`` (validated like ``session.create``'s) are
    returned as the new Session's Agent overrides.
    """
    session_id = _optional_string(params, "session_id")
    new_session = params.get("new_session")
    if (session_id is None) == (new_session is None):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params must name exactly one of session_id (an existing Session) "
            "and new_session (a new Session)",
        )
    if session_id is not None:
        return session_id
    if not isinstance(new_session, dict):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.new_session must be an object")
    _reject_unsupported(new_session, {"agent_overrides"}, "params.new_session")
    overrides = _optional_agent_overrides(
        new_session, allow_clear=False, label="params.new_session"
    )
    return AgentOverrides(**overrides) if overrides else AgentOverrides()


async def _start_chat_in_new_session(
    state: Any,
    agent_id: str,
    project_id: str | None,
    content: str | list[ContentBlock],
    agent_overrides: AgentOverrides,
    *,
    input_origin: ChatInputOrigin | None,
    file_mentions: list[str],
    streaming: bool,
) -> Run:
    """Create the Session of a new conversation with its first Run, or nothing.

    Chat core validates the target before it creates the Session, so a
    rejected message leaves no Session. The new Session becomes an Identity
    Agent's current one, like a message to an existing Session does.
    """
    # A file snapshot stamps the Session that read the file, so the id comes first.
    session_id = new_session_id() if file_mentions else None
    if session_id is not None:
        content = await _expand_content_file_mentions(
            state, agent_id, project_id, session_id, content, file_mentions
        )
    chat_loop = _streaming_chat_loop(state) if streaming else state.chat_loop
    try:
        run = await chat_loop.start_run_in_new_session(
            agent_id,
            content,
            session_id=session_id,
            agent_overrides=agent_overrides,
            actor="rpc",
            input_origin=input_origin,
            reply_surface=WEBUI_REPLY_SURFACE,
            project_id=project_id,
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    publish_session_changed(state, project_id, agent_id, run.session_id)
    if project_id is None:
        await _mark_current_session(state, agent_id, run.session_id)
    return cast(Run, run)


async def _send_chat(state: Any, params: JsonObject) -> JsonObject:
    submission = await _submit_chat(state, params, streaming=False)
    if isinstance(submission, dict):
        return submission

    try:
        _bridge_run_to_event_bus(state, submission)
        assistant_message = await submission.wait()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _run_response(
        submission,
        final_message=assistant_message,
        file_delivery=state.file_delivery,
    )


async def _stream_chat(state: Any, params: JsonObject) -> JsonObject:
    submission = await _submit_chat(state, params, streaming=True)
    if isinstance(submission, dict):
        return submission

    try:
        _bridge_run_to_event_bus(state, submission)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _run_response(
        submission,
        sse_url=f"/api/runs/{submission.id}/events",
        file_delivery=state.file_delivery,
    )


async def _edit_chat(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(
        params,
        {"agent_id", "session_id", "message_id", "content"},
        "chat.edit",
    )
    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _required_string(params, "session_id")
    message_id = _required_string(params, "message_id")
    content = _required_string(params, "content")

    try:
        run = await _streaming_chat_loop(state).edit_run(
            agent_id,
            content,
            session_id=session_id,
            message_id=message_id,
            reply_surface=WEBUI_REPLY_SURFACE,
            project_id=project_id,
        )
        if project_id is None:
            await _mark_current_session(state, agent_id, session_id)
        _bridge_run_to_event_bus(state, run)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _run_response(
        run,
        sse_url=f"/api/runs/{run.id}/events",
        file_delivery=state.file_delivery,
    )


def _run_started_during_enqueue(item: QueuedRunItem) -> Run | None:
    """Return the Run when enqueue won a busy-to-idle race, else None."""
    if not item.future.done():
        return None
    if item.future.cancelled():
        raise RunCancelledError(f"queued run cancelled: {item.item_id}")
    return item.future.result()


async def _cancel_chat(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"run_id", "reason"}, "chat.cancel")

    run_id = _required_string(params, "run_id")
    reason = _optional_string(params, "reason")
    try:
        run = await state.chat_runs.cancel(run_id, reason=reason, initiator="rpc")
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _run_response(run, file_delivery=state.file_delivery)


async def _stop_all_chat(state: Any, params: JsonObject) -> JsonObject:
    """Stop a Session's Run, its background commands and terminals, and its Sub-Agents."""
    _reject_unsupported(params, {"agent_id", "session_id"}, "chat.stop_all")
    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _required_string(params, "session_id")
    address = SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)
    try:
        stopped = await state.runtime.subagents.stop_tree(address)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"ok": True, "stopped": stopped}


async def _cancel_tool_call_chat(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"agent_id", "run_id", "tool_call_id"}, "chat.cancel_tool_call")

    run_id = _required_string(params, "run_id")
    tool_call_id = _required_string(params, "tool_call_id")
    try:
        run = state.chat_runs.get(run_id)
        cancelled = run.cancel_tool_call(tool_call_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    if not cancelled:
        raise RpcError(
            RPC_ERROR_RUN_NOT_FOUND,
            f"tool call not found: {tool_call_id}",
        )
    return {"ok": True}


async def _control_run_chat(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(
        params, {"agent_id", "session_id", "run_id", "action", "tool_call_id"}, "chat.control_run"
    )
    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _required_string(params, "session_id")
    run_id = _required_string(params, "run_id")
    action = _required_string(params, "action")
    try:
        run = state.chat_runs.get(run_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    if (run.agent_id, run.project_id, run.session_id) != (agent_id, project_id, session_id):
        raise RpcError(RPC_ERROR_RUN_NOT_FOUND, "Run not found.")
    if action == "compact" and "tool_call_id" not in params:
        accepted = run.request_compaction()
    elif action == "background_tool":
        accepted = run.background_tool_call(_required_string(params, "tool_call_id"))
    else:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "Invalid Run control action.")
    if not accepted:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "This action is no longer available.")
    return _run_response(run)


def _chat_queue_list(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"agent_id", "session_id"}, "chat.queue_list")

    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _required_string(params, "session_id")
    try:
        items = [
            item
            for item in _state_chat_runs(state).list_queued(
                agent_id, session_id, project_id=project_id
            )
            if not item.internal
        ]
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"items": [item.to_dict() for item in items]}


def _chat_queue_steer(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"agent_id", "session_id", "item_id"}, "chat.queue_steer")
    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _required_string(params, "session_id")
    item_id = _required_string(params, "item_id")
    try:
        manager = _state_chat_runs(state)
        if not _queue_item_is_public(manager, agent_id, session_id, item_id, project_id):
            raise RpcError(RPC_ERROR_QUEUE_ITEM_NOT_FOUND, f"queued item not found: {item_id}")
        item = manager.steer_queued(agent_id, session_id, item_id, project_id=project_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    _publish_queue_changed(state, agent_id, session_id)
    return {"item": item.to_dict()}


def _chat_queue_remove(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"agent_id", "session_id", "item_id"}, "chat.queue_remove")

    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _required_string(params, "session_id")
    item_id = _required_string(params, "item_id")
    try:
        chat_runs = _state_chat_runs(state)
        queued_item = _public_queue_item(chat_runs, agent_id, session_id, item_id, project_id)
        if queued_item is None:
            raise RpcError(RPC_ERROR_QUEUE_ITEM_NOT_FOUND, f"queued item not found: {item_id}")
        if queued_item.steering_in_flight:
            raise RpcError(
                RPC_ERROR_QUEUE_ITEM_STEERING,
                "queued item is being delivered into the running Run and can no longer be removed",
            )
        removed = chat_runs.remove_queued(agent_id, session_id, item_id, project_id=project_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    if not removed:
        raise RpcError(RPC_ERROR_QUEUE_ITEM_NOT_FOUND, f"queued item not found: {item_id}")
    _publish_queue_changed(state, agent_id, session_id)
    return {"ok": True}


async def _chat_queue_update(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(
        params,
        {"agent_id", "session_id", "item_id", "content", "input_origin", "file_mentions"},
        "chat.queue_update",
    )

    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _required_string(params, "session_id")
    item_id = _required_string(params, "item_id")
    content = _parse_chat_content(params, "content")
    input_origin = _optional_chat_input_origin(params)
    # An edit replaces the queued content wholesale, so mentions are re-expanded
    # against the edited text — a fresh snapshot at edit time.
    content = await _expand_content_file_mentions(
        state, agent_id, project_id, session_id, content, _optional_file_mentions(params)
    )

    try:
        chat_runs = _state_chat_runs(state)
        queued_item = _public_queue_item(chat_runs, agent_id, session_id, item_id, project_id)
        if queued_item is None:
            raise RpcError(RPC_ERROR_QUEUE_ITEM_NOT_FOUND, f"queued item not found: {item_id}")
        if queued_item.steering:
            raise RpcError(
                RPC_ERROR_QUEUE_ITEM_STEERING,
                "queued item is selected for steering and can no longer be edited",
            )
        if not queued_item.editable:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "queued item content cannot be edited losslessly",
            )

        # The address's project is the anchor the item was queued under — the queue
        # key carries it, and the item above was found via that key. Rebuild against
        # the same anchor; otherwise a project session is looked up in the identity
        # anchor and the rebuild fails with session-not-found.
        (
            resolved_session_id,
            updated_executor,
            updated_display_content,
        ) = await _build_streaming_queue_update(
            state,
            agent_id,
            session_id,
            content,
            queued_item,
            input_origin=input_origin,
            project_id=project_id,
        )
        updated = chat_runs.update_queued(
            agent_id,
            resolved_session_id,
            item_id,
            updated_executor,
            updated_display_content,
            project_id=project_id,
            editable=queue_content_is_editable(content),
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    if not updated:
        raise RpcError(RPC_ERROR_QUEUE_ITEM_NOT_FOUND, f"queued item not found: {item_id}")
    # Scope on the resolved session id — content can move an item to a different
    # session, and that resolved id (not the raw input) is what was mutated.
    _publish_queue_changed(state, agent_id, resolved_session_id)
    return {"ok": True}


def _public_queue_item(
    chat_runs: ChatRunManager,
    agent_id: str,
    session_id: str,
    item_id: str,
    project_id: str | None,
) -> QueuedRunItem | None:
    """Return the queued item if it exists and is public (not internal), else ``None``.

    Internal items (e.g. subagent-driven) stay hidden from the queue RPCs, so they are
    treated as absent here just like a missing id.
    """
    for item in chat_runs.list_queued(agent_id, session_id, project_id=project_id):
        if item.item_id == item_id:
            return item if not item.internal else None
    return None


def _queue_item_is_public(
    chat_runs: ChatRunManager,
    agent_id: str,
    session_id: str,
    item_id: str,
    project_id: str | None,
) -> bool:
    return _public_queue_item(chat_runs, agent_id, session_id, item_id, project_id) is not None


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return chat RPC handlers."""

    return {
        "chat.history": _chat_history,
        "chat.run_result": _chat_run_result,
        "chat.reflections": _chat_reflections,
        "chat.send": _send_chat,
        "chat.stream": _stream_chat,
        "chat.edit": _edit_chat,
        "chat.cancel": _cancel_chat,
        "chat.stop_all": _stop_all_chat,
        "chat.cancel_tool_call": _cancel_tool_call_chat,
        "chat.control_run": _control_run_chat,
        "chat.queue_steer": _chat_queue_steer,
        "chat.queue_list": _chat_queue_list,
        "chat.queue_remove": _chat_queue_remove,
        "chat.queue_update": _chat_queue_update,
        "subagent.inspect": _subagent_inspect,
    }
