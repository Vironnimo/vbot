"""Chat RPC handlers."""

from __future__ import annotations

from typing import Any, cast

from core.chat import CommandAction, CommandHandled, parse_handoff_argument
from core.chat.content_blocks import ContentBlock, TextBlock
from core.projects import (
    AgentResolutionError,
    InvalidAgentAddressError,
    format_agent_address,
    parse_agent_address,
)
from core.runs import ActiveRunError, ChatRunManager
from server.events import RESOURCE_KIND_QUEUE
from server.rpc.agent_methods import _create_session
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
    _optional_positive_integer,
    _optional_string,
    _parse_chat_content,
    _required_agent_address,
    _required_string,
)

JsonObject = dict[str, Any]
MAX_CHAT_HISTORY_LIMIT = 500

# Instruction sent to the current agent to write the handoff. Plain text, not
# i18n: it is delivered as an internal note to the model and never shown to the
# user. The wording is deliberate — do not paraphrase.
HANDOFF_INSTRUCTION = (
    "You are handing off this conversation to another agent who will continue it in a "
    "fresh session with none of its context. Write a handoff so they can carry on "
    "seamlessly, as if they had been here the whole time.\n"
    "\n"
    "Capture whatever actually matters in this conversation so far — what it has been "
    "about, what has been said, established, or decided, and where things currently "
    "stand. What that includes depends entirely on the conversation: it might be a "
    "task in progress, a discussion, a decision being worked through, or anything "
    "else. Include only what is genuinely relevant here and leave out the rest; do not "
    "force it into a fixed structure or invent things that are not there.\n"
    "\n"
    "Write it entirely from this conversation — do not use tools or go check anything. "
    "Write it as a briefing to the next agent, in the language of this conversation, "
    "and output only the handoff itself, with no preamble and no sign-off, because "
    "your reply becomes their first message."
)


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


def _chat_history(state: Any, params: JsonObject) -> JsonObject:
    supported_fields = {"agent_id", "session_id", "limit", "before"}
    unsupported_fields = sorted(set(params) - supported_fields)
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported chat.history fields: {', '.join(unsupported_fields)}",
        )

    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _optional_string(params, "session_id")
    limit = _optional_positive_integer(params, "limit", max_value=MAX_CHAT_HISTORY_LIMIT)
    before = _optional_string(params, "before")
    try:
        active_session_id = _resolve_history_session_id(state, agent_id, session_id, project_id)
        session = state.runtime.chat_sessions.get(agent_id, active_session_id, project_id)
        visible_messages = [
            _visible_message(message)
            for message in session.load()
            if _is_visible_history_message(message)
        ]
        messages, has_more = _history_page(visible_messages, limit=limit, before=before)
        active_run = _active_run_response(state, agent_id, active_session_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    response: JsonObject = {
        "agent_id": agent_id,
        "session_id": active_session_id,
        "messages": messages,
        "has_more": has_more,
    }
    if active_run is not None:
        response["active_run"] = active_run
    return response


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

    page = page_source[-limit:]
    return page, len(page_source) > len(page)


def _extract_command_text(content: str | list[ContentBlock]) -> str | None:
    if isinstance(content, str):
        return content

    if len(content) != 1:
        return None

    block = content[0]
    if isinstance(block, TextBlock):
        return block.text
    return None


def _command_handled_response(
    result: CommandHandled | str | None,
    *,
    output: str | None = None,
) -> JsonObject:
    if isinstance(result, CommandHandled):
        reply = result.reply
        data = result.data
        channel = output or result.output
    else:
        reply = result
        data = None
        channel = output

    response: JsonObject = {
        "command_handled": True,
        "reply": reply or "",
        # The output channel travels with the handled command so the frontend
        # presents it (toast / transient card / action) without a second lookup
        # by command name. Defaults to "toast" for replies that carry no channel.
        "output": channel or "toast",
    }
    if data:
        response["data"] = dict(data)
    return response


async def _dispatch_chat_command(
    state: Any,
    agent_id: str,
    session_id: str,
    command_text: str,
    *,
    streaming: bool,
    project_id: str | None = None,
) -> JsonObject | None:
    try:
        command_result = _state_command_dispatcher(state).dispatch(
            agent_id,
            session_id,
            command_text,
            project_id,
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    if isinstance(command_result, CommandHandled):
        return _command_handled_response(command_result)
    if isinstance(command_result, CommandAction):
        return await _handle_command_action(
            state,
            agent_id,
            session_id,
            command_result,
            streaming=streaming,
            project_id=project_id,
        )
    return None


async def _handle_command_action(
    state: Any,
    agent_id: str,
    session_id: str,
    command_action: CommandAction,
    *,
    streaming: bool,
    project_id: str | None = None,
) -> JsonObject:
    match command_action.name:
        case "compact":
            return await _handle_compact_command(
                state, agent_id, session_id, command_action.argument
            )
        case "handoff":
            return await _handle_handoff_command(
                state, agent_id, session_id, command_action.argument, project_id=project_id
            )
        case "new_session":
            return _handle_new_session_command(state, agent_id, session_id, project_id=project_id)
        case "retry_last_turn":
            return await _retry_chat_for_ids(
                state, agent_id, session_id, streaming=streaming, project_id=project_id
            )
    raise AssertionError(f"unsupported command action: {command_action.name}")


def _format_session_agent(agent_id: str, project_id: str | None) -> str:
    """Build the ``session.create`` ``agent_id`` value for an (agent, project) pair.

    ``session.create`` re-parses the address at its own entry (the single seam),
    so chat hands it back the outside spelling: a bare id for an identity
    session, ``agent@projekt`` for a project session.
    """
    return format_agent_address(agent_id, project_id)


def _handle_new_session_command(
    state: Any, agent_id: str, session_id: str, *, project_id: str | None = None
) -> JsonObject:
    try:
        active_run = _state_chat_runs(state).active_run(agent_id=agent_id, session_id=session_id)
        if active_run is not None:
            return _command_handled_response(
                "A new session can be started after the current run finishes.",
            )

        response = _create_session(
            state,
            {"agent_id": _format_session_agent(agent_id, project_id), "make_current": True},
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    new_session_id = _required_string(response, "session_id")
    return _command_handled_response(
        CommandHandled(
            reply=f"New session started: {new_session_id}",
            data={"command": "new", "session_id": new_session_id},
        ),
        output="action",
    )


def _build_handoff_prompt(instruction: str | None) -> str:
    """Weave an optional user instruction into the base handoff prompt.

    Mirrors the `/compact <instruction>` pattern: the bare handoff prompt is
    unchanged when no instruction is given, so the no-argument path is identical
    to before.
    """
    cleaned = (instruction or "").strip()
    if not cleaned:
        return HANDOFF_INSTRUCTION
    return (
        f"{HANDOFF_INSTRUCTION}\n"
        "\n"
        "The user added a specific instruction for this handoff. Follow it while "
        "writing, without dropping anything else that genuinely matters:\n"
        f"{cleaned}"
    )


async def _start_handoff_run(
    state: Any,
    agent_id: str,
    message: str,
    *,
    session_id: str,
    project_id: str | None,
    internal: bool,
) -> Any:
    """Start a handoff run, identity through the trigger service, project on the loop.

    An identity handoff (``project_id is None``) stays byte-identical to before:
    it runs through ``trigger_service.trigger_run`` (with its queue-on-busy
    fallback). A project handoff has no project-aware trigger service yet
    (automation is a separate task), so it goes straight through the chat loop,
    which already threads ``project_id`` into the session anchor and run.
    """
    if project_id is None:
        return await state.runtime.trigger_service.trigger_run(
            agent_id,
            message,
            session_id=session_id,
            internal=internal,
        )
    return await state.chat_loop.start_run(
        agent_id,
        message,
        session_id=session_id,
        internal=internal,
        project_id=project_id,
    )


async def _handle_handoff_command(
    state: Any,
    agent_id: str,
    session_id: str,
    argument: str | None,
    *,
    project_id: str | None = None,
) -> JsonObject:
    parsed = parse_handoff_argument(argument)

    # The handoff target may itself be project-qualified (``agent:orchestrator@vbot``).
    # Parse it through the single address seam; with no explicit target the handoff
    # stays in the source's (agent, project) scope.
    try:
        if parsed.target_agent_id is not None:
            target_agent_id, target_project_id = parse_agent_address(parsed.target_agent_id)
        else:
            target_agent_id, target_project_id = agent_id, project_id
    except InvalidAgentAddressError:
        return _command_handled_response(
            f"Cannot handoff to invalid agent address: {parsed.target_agent_id}",
        )

    target_display = format_agent_address(target_agent_id, target_project_id)
    try:
        active_run = _state_chat_runs(state).active_run(agent_id=agent_id, session_id=session_id)
        if active_run is not None:
            return _command_handled_response(
                "A handoff can be started after the current run finishes.",
            )

        if (target_agent_id, target_project_id) != (agent_id, project_id):
            try:
                state.runtime.agent_resolver.resolve_agent(target_project_id, target_agent_id)
            except AgentResolutionError:
                return _command_handled_response(
                    f"Cannot handoff to unknown agent: {target_display}",
                )

        handoff_run = await _start_handoff_run(
            state,
            agent_id,
            _build_handoff_prompt(parsed.instruction),
            session_id=session_id,
            project_id=project_id,
            internal=True,
        )
        handoff_message = await handoff_run.wait()
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    handoff_text = _extract_handoff_text(handoff_message.content)
    if not handoff_text:
        return _command_handled_response("Handoff could not be generated.")

    try:
        response = _create_session(
            state,
            {
                "agent_id": _format_session_agent(target_agent_id, target_project_id),
                "make_current": True,
            },
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    new_session_id = _required_string(response, "session_id")

    try:
        run = await _start_handoff_run(
            state,
            target_agent_id,
            handoff_text,
            session_id=new_session_id,
            project_id=target_project_id,
            internal=False,
        )
        _bridge_run_to_event_bus(state, run)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    return _command_handled_response(
        CommandHandled(
            reply=f"Handoff sent to {target_display}, session {new_session_id}.",
            data={
                "command": "handoff",
                "session_id": new_session_id,
                "agent_id": target_display,
            },
        ),
        output="action",
    )


def _extract_handoff_text(content: str | list[ContentBlock] | None) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(block.text for block in content if isinstance(block, TextBlock)).strip()
    return ""


async def _send_chat(state: Any, params: JsonObject) -> JsonObject:
    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _required_string(params, "session_id")
    content = _parse_chat_content(params, "content")
    input_origin = _optional_chat_input_origin(params)

    command_text = _extract_command_text(content)
    if command_text is not None:
        command_response = await _dispatch_chat_command(
            state,
            agent_id,
            session_id,
            command_text,
            streaming=False,
            project_id=project_id,
        )
        if command_response is not None:
            return command_response

    try:
        if input_origin is None:
            run = await state.chat_loop.start_run(
                agent_id, content, session_id=session_id, project_id=project_id
            )
        else:
            run = await state.chat_loop.start_run(
                agent_id,
                content,
                session_id=session_id,
                input_origin=input_origin,
                project_id=project_id,
            )
    except ActiveRunError:
        try:
            if input_origin is None:
                queued_item = await state.chat_loop.queue_run(
                    agent_id,
                    content,
                    session_id=session_id,
                    project_id=project_id,
                )
            else:
                queued_item = await state.chat_loop.queue_run(
                    agent_id,
                    content,
                    session_id=session_id,
                    input_origin=input_origin,
                    project_id=project_id,
                )
            _bridge_queued_item_to_event_bus(state, queued_item)
            _publish_queue_changed(state, agent_id, session_id)
        except Exception as exc:
            raise _map_expected_error(exc) from exc
        return _queued_response(queued_item)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    try:
        _bridge_run_to_event_bus(state, run)
        assistant_message = await run.wait()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _run_response(run, final_message=assistant_message)


async def _stream_chat(state: Any, params: JsonObject) -> JsonObject:
    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _required_string(params, "session_id")
    content = _parse_chat_content(params, "content")
    input_origin = _optional_chat_input_origin(params)

    command_text = _extract_command_text(content)
    if command_text is not None:
        command_response = await _dispatch_chat_command(
            state,
            agent_id,
            session_id,
            command_text,
            streaming=True,
            project_id=project_id,
        )
        if command_response is not None:
            return command_response

    streaming_chat_loop = _streaming_chat_loop(state)
    try:
        if input_origin is None:
            run = await streaming_chat_loop.start_run(
                agent_id, content, session_id=session_id, project_id=project_id
            )
        else:
            run = await streaming_chat_loop.start_run(
                agent_id,
                content,
                session_id=session_id,
                input_origin=input_origin,
                project_id=project_id,
            )
    except ActiveRunError:
        try:
            if input_origin is None:
                queued_item = await streaming_chat_loop.queue_run(
                    agent_id,
                    content,
                    session_id=session_id,
                    project_id=project_id,
                )
            else:
                queued_item = await streaming_chat_loop.queue_run(
                    agent_id,
                    content,
                    session_id=session_id,
                    input_origin=input_origin,
                    project_id=project_id,
                )
            _bridge_queued_item_to_event_bus(state, queued_item)
            _publish_queue_changed(state, agent_id, session_id)
        except Exception as exc:
            raise _map_expected_error(exc) from exc
        return _queued_response(queued_item)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    try:
        _bridge_run_to_event_bus(state, run)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _run_response(run, sse_url=f"/api/runs/{run.id}/events")


async def _handle_compact_command(
    state: Any, agent_id: str, session_id: str, instruction: str | None = None
) -> JsonObject:
    try:
        reply = await state.runtime.trigger_service.compact_session(
            agent_id, session_id, instruction
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _command_handled_response(reply, output="toast")


async def _retry_chat(state: Any, params: JsonObject) -> JsonObject:
    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _required_string(params, "session_id")
    return await _retry_chat_for_ids(
        state, agent_id, session_id, streaming=True, project_id=project_id
    )


async def _retry_chat_for_ids(
    state: Any,
    agent_id: str,
    session_id: str,
    *,
    streaming: bool,
    project_id: str | None = None,
) -> JsonObject:
    try:
        chat_loop = _streaming_chat_loop(state) if streaming else state.chat_loop
        run = await chat_loop.retry_run(agent_id, session_id, project_id=project_id)
        _bridge_run_to_event_bus(state, run)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    if streaming:
        return _run_response(run, sse_url=f"/api/runs/{run.id}/events")

    try:
        assistant_message = await run.wait()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _run_response(run, final_message=assistant_message)


async def _cancel_chat(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"run_id", "reason"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported chat.cancel fields: {', '.join(unsupported_fields)}",
        )

    run_id = _required_string(params, "run_id")
    reason = _optional_string(params, "reason")
    try:
        run = await state.chat_runs.cancel(run_id, reason=reason)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _run_response(run)


async def _cancel_tool_call_chat(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"agent_id", "run_id", "tool_call_id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported chat.cancel_tool_call fields: {', '.join(unsupported_fields)}",
        )

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


def _chat_queue_list(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"agent_id", "session_id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported chat.queue_list fields: {', '.join(unsupported_fields)}",
        )

    agent_id = _required_string(params, "agent_id")
    session_id = _required_string(params, "session_id")
    try:
        items = [
            item
            for item in _state_chat_runs(state).list_queued(agent_id, session_id)
            if not item.internal
        ]
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"items": [item.to_dict() for item in items]}


def _chat_queue_remove(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"agent_id", "session_id", "item_id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported chat.queue_remove fields: {', '.join(unsupported_fields)}",
        )

    agent_id = _required_string(params, "agent_id")
    session_id = _required_string(params, "session_id")
    item_id = _required_string(params, "item_id")
    try:
        chat_runs = _state_chat_runs(state)
        if not _queue_item_is_public(chat_runs, agent_id, session_id, item_id):
            raise RpcError(RPC_ERROR_QUEUE_ITEM_NOT_FOUND, f"queued item not found: {item_id}")
        removed = chat_runs.remove_queued(agent_id, session_id, item_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    if not removed:
        raise RpcError(RPC_ERROR_QUEUE_ITEM_NOT_FOUND, f"queued item not found: {item_id}")
    _publish_queue_changed(state, agent_id, session_id)
    return {"ok": True}


def _chat_queue_update(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(
        set(params) - {"agent_id", "session_id", "item_id", "content", "input_origin"}
    )
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported chat.queue_update fields: {', '.join(unsupported_fields)}",
        )

    agent_id = _required_string(params, "agent_id")
    session_id = _required_string(params, "session_id")
    item_id = _required_string(params, "item_id")
    content = _parse_chat_content(params, "content")
    input_origin = _optional_chat_input_origin(params)

    try:
        chat_runs = _state_chat_runs(state)
        if not _queue_item_is_public(chat_runs, agent_id, session_id, item_id):
            raise RpcError(RPC_ERROR_QUEUE_ITEM_NOT_FOUND, f"queued item not found: {item_id}")

        if input_origin is None:
            (
                resolved_session_id,
                updated_executor,
                updated_display_content,
            ) = _build_streaming_queue_update(state, agent_id, session_id, content)
        else:
            (
                resolved_session_id,
                updated_executor,
                updated_display_content,
            ) = _build_streaming_queue_update(
                state,
                agent_id,
                session_id,
                content,
                input_origin=input_origin,
            )
        updated = chat_runs.update_queued(
            agent_id,
            resolved_session_id,
            item_id,
            updated_executor,
            updated_display_content,
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    if not updated:
        raise RpcError(RPC_ERROR_QUEUE_ITEM_NOT_FOUND, f"queued item not found: {item_id}")
    # Scope on the resolved session id — content can move an item to a different
    # session, and that resolved id (not the raw input) is what was mutated.
    _publish_queue_changed(state, agent_id, resolved_session_id)
    return {"ok": True}


def _active_run_response(state: Any, agent_id: str, session_id: str) -> JsonObject | None:
    run = _state_chat_runs(state).active_run(agent_id=agent_id, session_id=session_id)
    if run is None:
        return None
    return _run_response(run, sse_url=f"/api/runs/{run.id}/events")


def _queue_item_is_public(
    chat_runs: ChatRunManager,
    agent_id: str,
    session_id: str,
    item_id: str,
) -> bool:
    for item in chat_runs.list_queued(agent_id, session_id):
        if item.item_id == item_id:
            return not item.internal
    return False


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return chat RPC handlers."""

    return {
        "chat.history": _chat_history,
        "chat.send": _send_chat,
        "chat.stream": _stream_chat,
        "chat.retry_last_turn": _retry_chat,
        "chat.cancel": _cancel_chat,
        "chat.cancel_tool_call": _cancel_tool_call_chat,
        "chat.queue_list": _chat_queue_list,
        "chat.queue_remove": _chat_queue_remove,
        "chat.queue_update": _chat_queue_update,
    }
