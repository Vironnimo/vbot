"""Chat RPC handlers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from core.chat import (
    CommandExecutionContext,
    CommandOutcome,
    CommandResourceChange,
    PreparedCommand,
    ReplySurface,
    aggregate_session_usage,
    latest_session_context_usage,
    queue_content_is_editable,
)
from core.chat.content_blocks import ContentBlock
from core.chat.file_mentions import expand_file_mentions, resolve_mention_root
from core.projects import format_agent_address
from core.runs import ActiveRunError, ChatRunManager, QueuedRunItem, Run, RunCancelledError
from core.sessions import (
    SessionAddress,
    active_session_messages,
    editable_session_message_ids,
)
from core.tools.bash import background_bash_statuses
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool
from server.events import RESOURCE_KIND_AGENTS, RESOURCE_KIND_QUEUE
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import (
    RPC_ERROR_INVALID_REQUEST,
    RPC_ERROR_QUEUE_ITEM_NOT_FOUND,
    RPC_ERROR_RUN_NOT_FOUND,
    RpcError,
)
from server.rpc.event_bridge import (
    _bridge_queued_item_to_event_bus,
    _bridge_run_to_event_bus,
    publish_resource_changed,
)
from server.rpc.payloads import (
    _is_visible_history_message,
    _queued_response,
    _run_response,
    _visible_message,
)
from server.rpc.runtime_access import (
    _build_streaming_queue_update,
    _state_chat_runs,
    _state_command_dispatcher,
    _streaming_chat_loop,
)
from server.rpc.validation import (
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
_CHAT_RPC_WORKERS = BoundedWorkerPool(name="chat-rpc", max_workers=4)
_LOGGER = get_logger("server.rpc.chat")

WEBUI_REPLY_SURFACE = ReplySurface.webui()


@dataclass(frozen=True)
class _ChatHistoryProjection:
    messages: list[JsonObject]
    has_more: bool
    background_bash_statuses: JsonObject
    session_usage: JsonObject
    context_usage: JsonObject | None


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


async def _chat_history(state: Any, params: JsonObject) -> JsonObject:
    supported_fields = {"agent_id", "session_id", "limit", "before"}
    _reject_unsupported(params, supported_fields, "chat.history")

    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _optional_string(params, "session_id")
    limit = _optional_positive_integer(params, "limit", max_value=MAX_CHAT_HISTORY_LIMIT)
    before = _optional_string(params, "before")
    try:
        active_session_id = await _CHAT_RPC_WORKERS.run(
            _resolve_history_session_id,
            state,
            agent_id,
            session_id,
            project_id,
        )
        session = await _CHAT_RPC_WORKERS.run(
            state.runtime.chat_sessions.get,
            SessionAddress(project_id=project_id, agent_id=agent_id, session_id=active_session_id),
        )
        loaded_messages = await _CHAT_RPC_WORKERS.run(session.load)
        projection = await _CHAT_RPC_WORKERS.run(
            _project_chat_history,
            loaded_messages,
            file_delivery=getattr(state, "file_delivery", None),
            limit=limit,
            before=before,
        )
        active_run_object = _state_chat_runs(state).active_run(
            agent_id=agent_id,
            session_id=active_session_id,
            project_id=project_id,
        )
        active_run = (
            _run_response(
                active_run_object,
                sse_url=f"/api/runs/{active_run_object.id}/events",
                file_delivery=getattr(state, "file_delivery", None),
            )
            if active_run_object is not None
            else None
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    response: JsonObject = {
        "agent_id": agent_id,
        "session_id": active_session_id,
        "messages": projection.messages,
        "has_more": projection.has_more,
        "background_bash_statuses": projection.background_bash_statuses,
        # Whole-session provider-reported token fields — the page above may be a
        # slice, but these always cover the full transcript.
        "session_usage": projection.session_usage,
    }
    context_usage = (
        active_run_object.terminal_payload_extras.get("context_usage")
        if active_run_object is not None
        else None
    )
    if not isinstance(context_usage, dict):
        context_usage = projection.context_usage
    if context_usage is not None:
        response["context_usage"] = context_usage
    if active_run is not None:
        response["active_run"] = active_run
    return response


def _project_chat_history(
    loaded_messages: list[Any],
    *,
    file_delivery: Any,
    limit: int | None,
    before: str | None,
) -> _ChatHistoryProjection:
    active_messages = active_session_messages(loaded_messages)
    editable_message_ids = editable_session_message_ids(loaded_messages)
    visible_messages = [
        {
            **_visible_message(message, file_delivery=file_delivery),
            **({"editable": True} if message.id in editable_message_ids else {}),
        }
        for message in active_messages
        if _is_visible_history_message(message)
    ]
    messages, has_more = _history_page(visible_messages, limit=limit, before=before)
    return _ChatHistoryProjection(
        messages=messages,
        has_more=has_more,
        background_bash_statuses=background_bash_statuses(active_messages),
        session_usage=aggregate_session_usage(loaded_messages),
        context_usage=latest_session_context_usage(active_messages),
    )


def _resolve_history_session_id(
    state: Any, agent_id: str, session_id: str | None, project_id: str | None
) -> str:
    """Pick the session to read history from for an identity or project address.

    Identity (``project_id is None``) keeps today's behavior exactly: an explicit
    ``session_id`` wins, otherwise the identity agent's ``current_session_id``. A
    project session has no anchor-level current pointer (the config agent carries
    none), so an explicit ``session_id`` is required and a missing one is a clean
    client error.
    """
    if session_id is not None:
        return session_id
    if project_id is None:
        return cast(str, state.runtime.agents.get(agent_id).current_session_id)
    raise RpcError(
        RPC_ERROR_INVALID_REQUEST,
        "params.session_id is required for a project agent address",
    )


def _history_page(
    messages: list[JsonObject], *, limit: int | None, before: str | None
) -> tuple[list[JsonObject], bool]:
    page_source = messages
    if before is not None:
        before_index = next(
            (index for index, message in enumerate(messages) if message.get("id") == before),
            None,
        )
        if before_index is None:
            raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.before must reference a message id")
        page_source = messages[:before_index]

    if limit is None:
        return list(page_source), False

    page_start = max(0, len(page_source) - limit)
    page_start = _complete_history_segment_start(page_source, page_start)
    page = page_source[page_start:]
    return page, len(page_source) > len(page)


def _complete_history_segment_start(messages: list[JsonObject], page_start: int) -> int:
    """Expand the oldest page boundary to one complete persisted Run segment."""
    if page_start <= 0 or page_start >= len(messages):
        return page_start
    has_later_summary = any(
        message.get("role") == "run_summary" for message in messages[page_start:]
    )
    for index in range(page_start - 1, -1, -1):
        if messages[index].get("role") == "run_summary":
            return index + 1
    return 0 if has_later_summary else page_start


def _subagent_inspect(state: Any, params: JsonObject) -> JsonObject:
    supported_fields = {"id", "agent_id", "session_id"}
    _reject_unsupported(params, supported_fields, "subagent.inspect")
    work_id = _required_string(params, "id")
    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _required_string(params, "session_id")
    try:
        inspection = state.runtime.subagents.inspect(
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


def _command_outcome_response(outcome: CommandOutcome) -> JsonObject:
    response: JsonObject = {
        "command_handled": True,
        "reply": outcome.feedback.text if outcome.feedback is not None else "",
        "output": _command_output(outcome),
    }
    data: JsonObject = {"command": outcome.command, **dict(outcome.facts)}
    if outcome.navigation is not None:
        data.setdefault("session_id", outcome.navigation.session_id)
        data.setdefault(
            "agent_id",
            format_agent_address(outcome.navigation.agent_id, outcome.navigation.project_id),
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


async def _execute_chat_command(
    state: Any,
    agent_id: str,
    session_id: str,
    prepared: PreparedCommand,
    *,
    project_id: str | None = None,
) -> Run | JsonObject:
    dispatcher = _state_command_dispatcher(state)
    emitted_changes: set[tuple[str, tuple[tuple[str, str], ...]]] = set()

    def on_change(change: CommandResourceChange) -> None:
        key = _command_change_key(change)
        if key in emitted_changes:
            return
        emitted_changes.add(key)
        _publish_command_change(state, change)

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
    return _command_outcome_response(outcome)


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
    carries the files as they were when the user hit send. File I/O runs in a
    worker thread to keep the event loop free.
    """
    if not file_mentions:
        return content
    runtime = state.runtime
    try:
        root = resolve_mention_root(runtime, agent_id, project_id)
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
    agents = getattr(getattr(state, "runtime", None), "agents", None)
    if agents is None:
        return
    try:
        agent = await _CHAT_RPC_WORKERS.run(agents.get, agent_id)
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
        await _CHAT_RPC_WORKERS.run(agents.update, agent_id, current_session_id=session_id)
    except Exception as exc:
        _LOGGER.warning(
            "Failed to mark current session (agent=%s session=%s): %s",
            agent_id,
            session_id,
            exc,
        )
    else:
        publish_resource_changed(state, RESOURCE_KIND_AGENTS)
        _LOGGER.info(
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
    drift between the two RPC methods.
    """

    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _required_string(params, "session_id")
    content = _parse_chat_content(params, "content")
    input_origin = _optional_chat_input_origin(params)
    file_mentions = _optional_file_mentions(params)

    prepared_command = _state_command_dispatcher(state).prepare(content)
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
            return _queued_response(queued_item)
        run = started_run
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    return run


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
        file_delivery=getattr(state, "file_delivery", None),
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
        file_delivery=getattr(state, "file_delivery", None),
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
        file_delivery=getattr(state, "file_delivery", None),
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
        run = await state.chat_runs.cancel(run_id, reason=reason)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _run_response(run, file_delivery=getattr(state, "file_delivery", None))


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


async def _cancel_process_chat(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(
        params,
        {"agent_id", "process_id"},
        "chat.cancel_process",
    )

    agent_id, _project_id = _required_agent_address(params, "agent_id")
    process_id = _required_string(params, "process_id")
    try:
        tracked = await state.runtime.process_manager.cancel_for_user(
            process_id,
            agent_id,
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    status = "cancelled" if tracked.cancelled_by_user else tracked.status
    return {"process_id": process_id, "status": status}


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


def _chat_queue_remove(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"agent_id", "session_id", "item_id"}, "chat.queue_remove")

    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _required_string(params, "session_id")
    item_id = _required_string(params, "item_id")
    try:
        chat_runs = _state_chat_runs(state)
        if not _queue_item_is_public(chat_runs, agent_id, session_id, item_id, project_id):
            raise RpcError(RPC_ERROR_QUEUE_ITEM_NOT_FOUND, f"queued item not found: {item_id}")
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
        ) = _build_streaming_queue_update(
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


def _active_run_response(
    state: Any, agent_id: str, session_id: str, project_id: str | None
) -> JsonObject | None:
    run = _state_chat_runs(state).active_run(
        agent_id=agent_id, session_id=session_id, project_id=project_id
    )
    if run is None:
        return None
    return _run_response(
        run,
        sse_url=f"/api/runs/{run.id}/events",
        file_delivery=getattr(state, "file_delivery", None),
    )


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
        "chat.send": _send_chat,
        "chat.stream": _stream_chat,
        "chat.edit": _edit_chat,
        "chat.cancel": _cancel_chat,
        "chat.cancel_tool_call": _cancel_tool_call_chat,
        "chat.cancel_process": _cancel_process_chat,
        "chat.queue_list": _chat_queue_list,
        "chat.queue_remove": _chat_queue_remove,
        "chat.queue_update": _chat_queue_update,
        "subagent.inspect": _subagent_inspect,
    }
