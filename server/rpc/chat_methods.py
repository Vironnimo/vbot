"""Chat submission, Run control and RPC registration."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from core.chat import AccessorChatSubmission, ChatLoop, ChatMessage, CommandDispatcher, ReplySurface
from core.chat.content_blocks import ContentBlock
from core.chat.messages import WidgetState
from core.chat.widget_states import widget_state_note
from core.projects import format_agent_address
from core.runs import Run
from core.sessions import (
    SessionAddress,
    new_session_id,
)
from core.tools import builtin_payload_owner
from core.tools.command_manager import (
    COMMAND_RECENT_LINES,
    CommandManager,
    CommandNotFoundError,
    CommandReport,
    CommandStopError,
)
from core.tools.show_widget import SHOW_WIDGET_TOOL_NAME, WIDGET_PAYLOAD_TITLE
from core.utils.ids import new_id
from server.rpc._chat_input import (
    _chat_target,
    _execute_chat_command,
    _execute_new_session_chat_command,
    _expand_content_file_mentions,
    _mark_current_session,
    _NewSessionTarget,
    session_is_ephemeral,
)
from server.rpc._chat_queue import (
    _chat_queue_list,
    _chat_queue_remove,
    _chat_queue_steer,
    _chat_queue_update,
    _publish_queue_changed,
)
from server.rpc._chat_reads import (
    _chat_history,
    _chat_reflections,
    _chat_run_result,
    _chat_timeline,
    _chat_timeline_children,
    _chat_tool_call,
    _chat_work,
    _subagent_inspect,
    _subagent_tree,
)
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import (
    RPC_ERROR_COMMAND_NOT_FOUND,
    RPC_ERROR_INVALID_REQUEST,
    RPC_ERROR_RUN_NOT_FOUND,
    RpcError,
)
from server.rpc.event_bridge import (
    _bridge_queued_item_to_event_bus,
    _bridge_run_to_event_bus,
    publish_session_changed,
)
from server.rpc.payloads import (
    _queued_response,
    _run_response,
)
from server.rpc.validation import (
    ChatInputOrigin,
    _chat_reply_surface,
    _optional_chat_input_origin,
    _optional_file_mentions,
    _optional_string,
    _optional_widget_states,
    _parse_chat_content,
    _reject_unsupported,
    _required_agent_address,
    _required_session_address,
    _required_string,
)

JsonObject = dict[str, Any]


async def _submit_chat(
    state: Any,
    params: JsonObject,
    *,
    method: str,
) -> AccessorChatSubmission | Run | JsonObject:
    """Submit one accessor chat request and return its immediate disposition.

    Commands and queued work already have complete RPC payloads, while a Run
    still needs the caller-specific response treatment: ``chat.send`` waits for
    its final message and ``chat.stream`` returns the SSE location immediately.
    Everything before that presentation split is one submission path so command
    dispatch, file snapshots, queue fallback, and busy-to-idle handling cannot
    drift between the two RPC methods. The target is an existing Session
    (``session_id``) or a new one (``new_session``, see :func:`_chat_target`).
    """

    _reject_unsupported(
        params,
        {
            "agent_id",
            "session_id",
            "new_session",
            "content",
            "input_origin",
            "file_mentions",
            "reply_surface",
            "widget_states",
            "input_id",
        },
        method,
    )
    agent_id, project_id = _required_agent_address(params, "agent_id")
    target = _chat_target(params, agent_id, project_id, state=state)
    content = _parse_chat_content(params, "content")
    input_origin = _optional_chat_input_origin(params)
    reply_surface = _chat_reply_surface(params)
    file_mentions = _optional_file_mentions(params)
    widget_states = _optional_widget_states(params)

    dispatcher: CommandDispatcher = state.command_dispatcher
    prepared_command = dispatcher.prepare(content)
    # A new Session shows no widget yet. A command takes no widget values; the
    # client keeps them for the next message.
    if widget_states and isinstance(target, _NewSessionTarget):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.widget_states can accompany only a message to an existing Session",
        )
    chat_loop: ChatLoop = state.chat_loop
    input_id = _accessor_input_id(params)
    request_hash = _accessor_request_hash(params)
    if prepared_command is None:
        try:
            previous = await chat_loop.lookup_accessor_input(input_id, request_hash=request_hash)
        except Exception as exc:
            raise _map_expected_error(exc) from exc
        if previous is not None:
            return previous
    if isinstance(target, _NewSessionTarget):
        if prepared_command is not None:
            return await _execute_new_session_chat_command(
                state,
                agent_id,
                prepared_command,
                target,
                project_id=project_id,
                reply_surface=reply_surface,
            )
        return await _start_chat_in_new_session(
            state,
            agent_id,
            project_id,
            content,
            target,
            input_origin=input_origin,
            file_mentions=file_mentions,
            reply_surface=reply_surface,
            input_id=input_id,
            request_hash=request_hash,
        )
    address = target
    if prepared_command is not None:
        return await _execute_chat_command(
            state,
            address,
            prepared_command,
            reply_surface=reply_surface,
        )

    run_options: JsonObject = {}
    if input_origin is not None:
        run_options["input_origin"] = input_origin
    if widget_states and (note := await _widget_state_note(state, address, widget_states)):
        run_options["context_note"] = note
    content = await _expand_content_file_mentions(state, address, content, file_mentions)

    if project_id is None:
        await _mark_current_session(state, address)
    try:
        submission = await chat_loop.submit_accessor_input(
            agent_id,
            content,
            input_id=input_id,
            request_hash=request_hash,
            session_id=address.session_id,
            reply_surface=reply_surface,
            project_id=project_id,
            **run_options,
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return submission


def _accessor_input_id(params: JsonObject) -> str:
    if "input_id" not in params:
        return new_id("inp")
    value = _required_string(params, "input_id")
    if len(value) > 128 or any(character.isspace() or ord(character) < 33 for character in value):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.input_id must be a bounded opaque id")
    return value


def _accessor_request_hash(params: JsonObject) -> str:
    original = {key: value for key, value in params.items() if key != "input_id"}
    serialized = json.dumps(original, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("ascii")).hexdigest()


async def _widget_state_note(
    state: Any, address: SessionAddress, entries: list[tuple[str, JsonObject]]
) -> ChatMessage | None:
    """Return the note that hands *entries* to the Agent with the user's message.

    Only widgets the Session's current view shows count: a widget whose Run an
    edit removed is no longer part of the conversation, so its values are
    dropped instead of refusing the message. ``None`` when none is left.
    """
    owner_name = builtin_payload_owner(SHOW_WIDGET_TOOL_NAME)
    widgets: list[WidgetState] = []
    for widget_id, values in entries:
        payload = await state.runtime.chat_sessions.tool_result_payload_async(
            address, widget_id, owner_name=owner_name
        )
        title = payload.get(WIDGET_PAYLOAD_TITLE) if isinstance(payload, dict) else None
        if isinstance(title, str):
            widgets.append(WidgetState(widget_id, title, values))
    return widget_state_note(widgets) if widgets else None


async def _start_chat_in_new_session(
    state: Any,
    agent_id: str,
    project_id: str | None,
    content: str | list[ContentBlock],
    target: _NewSessionTarget,
    *,
    input_origin: ChatInputOrigin | None,
    file_mentions: list[str],
    reply_surface: ReplySurface,
    input_id: str,
    request_hash: str,
) -> AccessorChatSubmission:
    """Create the Session of a new conversation with its first Run, or nothing.

    Chat core validates the target before it creates the Session, so a
    rejected message leaves no Session. The new Session becomes an Identity
    Agent's current one, like a message to an existing Session does, unless it
    is an Ephemeral Session.
    """
    # A file snapshot stamps the Session that read the file, so the id comes first.
    session_id = target.session_id or (new_session_id() if file_mentions else None)
    if file_mentions:
        assert session_id is not None
        content = await _expand_content_file_mentions(
            state,
            SessionAddress(project_id, agent_id, session_id),
            content,
            file_mentions,
            new_session_project=target.working_project_id,
        )
    chat_loop: ChatLoop = state.chat_loop
    try:
        submission = await chat_loop.submit_accessor_input(
            agent_id,
            content,
            input_id=input_id,
            request_hash=request_hash,
            new_session=True,
            session_id=session_id,
            agent_overrides=target.agent_overrides,
            working_project_id=target.working_project_id,
            ephemeral=target.ephemeral,
            fast_mode=target.fast_mode,
            execution=target.execution,
            input_origin=input_origin,
            reply_surface=reply_surface,
            project_id=project_id,
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    address = submission.record.address
    publish_session_changed(state, address)
    if project_id is None:
        await _mark_current_session(state, address)
    return submission


async def _send_chat(state: Any, params: JsonObject) -> JsonObject:
    submission = await _submit_chat(state, params, method="chat.send")
    if isinstance(submission, dict):
        return submission
    if not isinstance(submission, Run):
        disposition = _accessor_disposition(state, submission)
        if disposition is not None:
            return disposition
    run = submission if isinstance(submission, Run) else submission.run
    assert run is not None

    try:
        _bridge_run_to_event_bus(state, run)
        assistant_message = await run.wait()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    response = await _run_response(
        run,
        final_message=assistant_message,
        file_delivery=state.file_delivery,
    )
    if not isinstance(submission, Run):
        response["input_id"] = submission.record.input_id
    return await _with_session_kind(state, run, response)


async def _stream_chat(state: Any, params: JsonObject) -> JsonObject:
    submission = await _submit_chat(state, params, method="chat.stream")
    if isinstance(submission, dict):
        return submission
    if not isinstance(submission, Run):
        disposition = _accessor_disposition(state, submission)
        if disposition is not None:
            return disposition
    run = submission if isinstance(submission, Run) else submission.run
    assert run is not None

    try:
        _bridge_run_to_event_bus(state, run)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    response = await _run_response(
        run,
        sse_url=f"/api/runs/{run.id}/events",
        file_delivery=state.file_delivery,
    )
    if not isinstance(submission, Run):
        response["input_id"] = submission.record.input_id
    return await _with_session_kind(state, run, response)


def _accessor_disposition(state: Any, submission: AccessorChatSubmission) -> JsonObject | None:
    record = submission.record
    if submission.run is not None:
        return None
    if submission.queued_item is not None:
        item = submission.queued_item
        _bridge_queued_item_to_event_bus(state, item)
        _publish_queue_changed(state, record.address)
        return {**_queued_response(item, record.address.session_id), "input_id": record.input_id}
    response: JsonObject = {
        "accepted": True,
        "input_id": record.input_id,
        "session_id": record.address.session_id,
        "input_state": record.state,
        "agent_id": format_agent_address(record.address.agent_id, record.address.project_id),
        "session_available": record.live,
    }
    if record.run_id is not None:
        response["run_id"] = record.run_id
    return response


async def _with_session_kind(state: Any, run: Run, response: JsonObject) -> JsonObject:
    """Add ``ephemeral``: whether the Run's Session is an Ephemeral Session."""
    address = SessionAddress(run.project_id, run.agent_id, run.session_id)
    response["ephemeral"] = await session_is_ephemeral(state, address)
    return response


async def _edit_chat(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(
        params,
        {"agent_id", "session_id", "message_id", "content", "reply_surface"},
        "chat.edit",
    )
    address = _required_session_address(params)
    message_id = _required_string(params, "message_id")
    content = _required_string(params, "content")
    reply_surface = _chat_reply_surface(params)

    try:
        run = await state.chat_loop.edit_run(
            address.agent_id,
            content,
            session_id=address.session_id,
            message_id=message_id,
            reply_surface=reply_surface,
            project_id=address.project_id,
        )
        if address.project_id is None:
            await _mark_current_session(state, address)
        _bridge_run_to_event_bus(state, run)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return await _run_response(
        run,
        sse_url=f"/api/runs/{run.id}/events",
        file_delivery=state.file_delivery,
    )


async def _cancel_chat(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"run_id", "reason"}, "chat.cancel")

    run_id = _required_string(params, "run_id")
    reason = _optional_string(params, "reason")
    try:
        run = await state.chat_runs.cancel(run_id, reason=reason, initiator="rpc")
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return await _run_response(run, file_delivery=state.file_delivery)


async def _stop_all_chat(state: Any, params: JsonObject) -> JsonObject:
    """Stop a Session's Run, its background commands and terminals, and its Sub-Agents."""
    _reject_unsupported(params, {"agent_id", "session_id"}, "chat.stop_all")
    address = _required_session_address(params)
    try:
        stopped = await state.runtime.subagents.stop_tree(address)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"ok": True, "stopped": stopped}


def _session_command(state: Any, params: JsonObject, method: str) -> CommandReport:
    """The background command ``command_id`` of the Session ``session_id``.

    Raises ``command_not_found`` for an unknown or forgotten id, a command of
    another Session and a command still inside its shell call.
    """
    _reject_unsupported(params, {"session_id", "command_id"}, method)
    session_id = _required_string(params, "session_id")
    command_id = _required_string(params, "command_id")
    commands: CommandManager = state.runtime.command_manager
    try:
        report = commands.report(command_id)
    except CommandNotFoundError:
        report = None
    if report is None or not report.background or report.owner.session_id != session_id:
        raise RpcError(
            RPC_ERROR_COMMAND_NOT_FOUND,
            f"No background command {command_id} runs or ran recently in this Session.",
            routine=True,
        )
    return report


async def _stop_command_chat(state: Any, params: JsonObject) -> JsonObject:
    """Stop a background command and every process it started, for the user.

    A running command's Agent receives the stopped result. A command whose
    shell had exited while processes it started still ran loses those
    processes and keeps the status it ended with; its result was delivered
    at its end. ``stopped`` is true when this call stopped the command or such
    processes, false when nothing of it ran any longer or another stop came
    first. ``status`` is the command's status afterwards, when none of its
    processes runs any longer.
    """
    report = _session_command(state, params, "chat.stop_command")
    if report.finished:
        return {"stopped": False, "status": report.status}
    try:
        final = await state.runtime.command_manager.stop(report.command_id, "user")
    except CommandStopError as exc:
        raise _map_expected_error(exc) from exc
    stopped = final.stop_reason == "user" or (report.ended and bool(report.still_running))
    return {"stopped": stopped, "status": final.status}


def _command_output_chat(state: Any, params: JsonObject) -> JsonObject:
    """A background command's status and its newest clean output lines.

    ``still_running`` is true while processes it started still run after its
    shell exited.
    """
    report = _session_command(state, params, "chat.command_output")
    output = state.runtime.command_manager.latest_output(report.command_id, COMMAND_RECENT_LINES)
    return {
        "status": report.status,
        "still_running": bool(report.still_running),
        "output": output,
    }


async def _cancel_tool_call_chat(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"agent_id", "run_id", "tool_call_id"}, "chat.cancel_tool_call")

    owner = _required_agent_address(params, "agent_id") if "agent_id" in params else None
    run_id = _required_string(params, "run_id")
    tool_call_id = _required_string(params, "tool_call_id")
    try:
        run = state.chat_runs.get(run_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    if owner is not None and (run.agent_id, run.project_id) != owner:
        raise RpcError(RPC_ERROR_RUN_NOT_FOUND, "Run not found.")
    cancelled = run.cancel_tool_call(tool_call_id)
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
    address = _required_session_address(params)
    run_id = _required_string(params, "run_id")
    action = _required_string(params, "action")
    try:
        run = state.chat_runs.get(run_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    if (run.agent_id, run.project_id, run.session_id) != (
        address.agent_id,
        address.project_id,
        address.session_id,
    ):
        raise RpcError(RPC_ERROR_RUN_NOT_FOUND, "Run not found.")
    if action == "compact" and "tool_call_id" not in params:
        accepted = run.request_compaction()
    elif action == "background_tool":
        accepted = run.background_tool_call(_required_string(params, "tool_call_id"))
    else:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "Invalid Run control action.")
    if not accepted:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "This action is no longer available.")
    return await _run_response(run)


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return chat RPC handlers."""

    return {
        "chat.history": _chat_history,
        "chat.timeline": _chat_timeline,
        "chat.timeline_children": _chat_timeline_children,
        "chat.tool_call": _chat_tool_call,
        "chat.run_result": _chat_run_result,
        "chat.reflections": _chat_reflections,
        "chat.send": _send_chat,
        "chat.stream": _stream_chat,
        "chat.edit": _edit_chat,
        "chat.cancel": _cancel_chat,
        "chat.stop_all": _stop_all_chat,
        "chat.work": _chat_work,
        "chat.cancel_tool_call": _cancel_tool_call_chat,
        "chat.stop_command": _stop_command_chat,
        "chat.command_output": _command_output_chat,
        "chat.control_run": _control_run_chat,
        "chat.queue_steer": _chat_queue_steer,
        "chat.queue_list": _chat_queue_list,
        "chat.queue_remove": _chat_queue_remove,
        "chat.queue_update": _chat_queue_update,
        "subagent.inspect": _subagent_inspect,
        "subagent.tree": _subagent_tree,
    }
