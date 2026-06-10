"""Sub-agent orchestration: spawning, result lookup, and parent-run linkage."""

from __future__ import annotations

import asyncio
from typing import Any, cast

from core.agents import AgentNotFoundError, InvalidAgentIdError
from core.chat import (
    ChatMessage,
    ChatSessionError,
)
from core.runs import (
    ActiveRunError,
    ChatRunManager,
    Run,
    RunCancelledError,
    RunExecutor,
    RunNotFoundError,
    RunStatus,
)
from core.subagents.tracker import (
    _LOGGER as _LOGGER,
)
from core.subagents.tracker import (
    ParentKey,
    SubAgentBatchTracker,
    _log_background_task_result,
    _SubAgentEntry,
)
from core.tools.tools import (
    JsonObject,
    ToolContext,
    tool_failure,
    tool_success,
)

DEFAULT_MAX_SUBAGENT_DEPTH = 4
DEFAULT_MAX_SUBAGENTS_PER_TURN = 8
DEFAULT_SUBAGENT_TIMEOUT_MINUTES = 60
SECONDS_PER_MINUTE = 60
SESSION_RESULT_RETRY_ATTEMPTS = 3
SESSION_RESULT_RETRY_DELAY_SECONDS = 0.05
SUBAGENT_STATUS_QUEUED = "queued"
SUBAGENT_SESSION_STARTED_EVENT = "subagent_session_started"
SUBAGENT_SESSION_METADATA_FLAG = "is_subagent_session"
SUBAGENT_PARENT_METADATA_KEY = "subagent_parent"
USER_CANCEL_REASON = "user"
SUBAGENT_USER_CANCEL_MESSAGE = "Cancelled by the user"

# Cascade policy switch: when True, a parent Run cancellation cascades to every
# sub-agent child including non-blocking ones (legacy behaviour). When False,
# only blocking sub-agent spawns (and queued-then-started blocking waits) get
# the cascade; non-blocking spawns survive the parent cancel.
# FLIP-BACK: set CASCADE_NON_BLOCKING_CHILDREN = True to restore the old behaviour.
CASCADE_NON_BLOCKING_CHILDREN = False


def _should_register_parent_cascade(blocking: bool) -> bool:
    """Return whether a spawn should register a parent-cancel cascade callback.

    The cascade policy is a single flip point: see ``CASCADE_NON_BLOCKING_CHILDREN``.
    """
    return blocking or CASCADE_NON_BLOCKING_CHILDREN


class SubAgentCoordinator:
    """Coordinate sub-agent run spawning, result lookup, and parent linkage."""

    def __init__(
        self,
        runtime: Any,
        trigger_service: Any,
        *,
        batch_tracker: SubAgentBatchTracker | None = None,
    ) -> None:
        self._runtime = runtime
        self._batch_tracker = batch_tracker or SubAgentBatchTracker(trigger_service)

    @property
    def batch_tracker(self) -> SubAgentBatchTracker:
        """Return the in-memory tracker used for this runtime instance."""
        return self._batch_tracker

    async def spawn(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        """Spawn or queue a sub-agent run for a tool invocation."""
        return await _handle_subagent(
            context,
            arguments,
            runtime=self._runtime,
            batch_tracker=self._batch_tracker,
        )

    async def result(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        """Return a spawned sub-agent result for a tool invocation."""
        return await _handle_subagent_result(
            context,
            arguments,
            runtime=self._runtime,
            batch_tracker=self._batch_tracker,
        )


async def _handle_subagent(
    context: ToolContext,
    arguments: JsonObject,
    *,
    runtime: Any,
    batch_tracker: SubAgentBatchTracker,
) -> JsonObject:
    content = arguments.get("content")
    if not isinstance(content, str) or not content.strip():
        return tool_failure(
            "invalid_arguments", "content is required and must be a non-empty string"
        )

    target_agent_id = arguments.get("agent_id", context.agent_id)
    if not isinstance(target_agent_id, str) or not target_agent_id:
        return tool_failure("invalid_arguments", "agent_id must be a non-empty string")
    blocking = arguments.get("blocking", False)
    if not isinstance(blocking, bool):
        return tool_failure("invalid_arguments", "blocking must be a boolean")
    session_id = arguments.get("session_id")
    if session_id is not None and (not isinstance(session_id, str) or not session_id):
        return tool_failure("invalid_arguments", "session_id must be a non-empty string")
    if (
        session_id is not None
        and target_agent_id == context.agent_id
        and session_id == context.session_id
    ):
        return tool_failure(
            "invalid_arguments",
            "cannot target the calling agent's own active session",
        )

    settings = _load_subagent_settings(runtime)
    parent_key = _parent_key(context)
    if context.nesting_depth >= settings["max_subagent_depth"]:
        return tool_failure(
            "subagent_depth_exceeded",
            f"Sub-agent nesting depth limit exceeded: {settings['max_subagent_depth']}",
        )
    if not batch_tracker.reserve_slot(parent_key, settings["max_subagents_per_turn"]):
        return tool_failure(
            "subagent_limit_exceeded",
            f"Sub-agent per-turn limit exceeded: {settings['max_subagents_per_turn']}",
        )

    slot_registered = False
    try:
        if context.is_cancelled():
            return tool_failure("run_cancelled", "Parent run was cancelled before sub-agent spawn")

        validation_error = _validate_target_agent(runtime, target_agent_id)
        if validation_error is not None:
            return validation_error

        if session_id is None:
            session = runtime.chat_sessions.create(target_agent_id)
        else:
            try:
                session = runtime.chat_sessions.get(target_agent_id, session_id)
            except ChatSessionError:
                return tool_failure("session_not_found", f"session does not exist: {session_id}")

        _mark_subagent_session(runtime, target_agent_id, session.id, context)
        await _emit_subagent_session_started(
            context,
            target_agent_id,
            session.id,
            status=RunStatus.RUNNING.value,
        )

        try:
            sub_run = await _start_subagent_run(
                runtime, target_agent_id, session.id, content, context
            )
        except ActiveRunError:
            if session_id is None:
                return tool_failure(
                    "session_busy",
                    f"session already has an active run: {session.id}",
                )

            _, executor = _make_subagent_executor(
                runtime,
                target_agent_id,
                session.id,
                content,
                context,
            )
            item = await _chat_run_manager(runtime).enqueue(
                agent_id=target_agent_id,
                session_id=session.id,
                executor=executor,
                display_content=content,
            )
            await _emit_subagent_session_started(
                context,
                target_agent_id,
                session.id,
                queue_item_id=item.item_id,
                status=SUBAGENT_STATUS_QUEUED,
            )
            if not blocking:
                queued_run = _started_run_from_queue_item(item)
                if queued_run is None:
                    batch_tracker.register_queued(
                        parent_key, target_agent_id, session.id, item.item_id
                    )
                    slot_registered = True
                    if _should_register_parent_cascade(blocking=False):
                        _attach_parent_cancellation(
                            runtime,
                            context.run_id,
                            queued_item=item,
                            queued_agent_id=target_agent_id,
                            queued_session_id=session.id,
                            batch_tracker=batch_tracker,
                            parent_key=parent_key,
                        )
                    _track_queued_subagent_completion(batch_tracker, parent_key, item)
                    return tool_success(
                        {
                            "agent_id": target_agent_id,
                            "session_id": session.id,
                            "queue_item_id": item.item_id,
                            "status": SUBAGENT_STATUS_QUEUED,
                        }
                    )
                sub_run = queued_run
            else:
                # Blocking parents always cascade so an awaited queued child
                # honours the parent cancel, even if it has not started yet.
                _attach_parent_cancellation(
                    runtime,
                    context.run_id,
                    queued_item=item,
                    queued_agent_id=target_agent_id,
                    queued_session_id=session.id,
                )
                try:
                    sub_run = await item.future
                except asyncio.CancelledError:
                    _chat_run_manager(runtime).remove_queued(
                        target_agent_id, session.id, item.item_id
                    )
                    raise

        await _emit_subagent_session_started(
            context,
            target_agent_id,
            session.id,
            run_id=sub_run.id,
            status=RunStatus.RUNNING.value,
        )
        batch_tracker.register_reserved(parent_key, target_agent_id, session.id, sub_run.id)
        slot_registered = True
        if _should_register_parent_cascade(blocking=blocking):
            _attach_parent_cancellation(
                runtime,
                context.run_id,
                sub_run=sub_run,
                batch_tracker=batch_tracker,
                parent_key=parent_key,
            )

        if not blocking:
            _track_subagent_completion(batch_tracker, parent_key, sub_run)
            return tool_success(
                {
                    "agent_id": target_agent_id,
                    "session_id": session.id,
                    "run_id": sub_run.id,
                    "status": RunStatus.RUNNING.value,
                }
            )

        timeout_seconds = settings["subagent_timeout_minutes"] * SECONDS_PER_MINUTE
        try:
            result = await asyncio.wait_for(
                _wait_for_subagent_result(sub_run), timeout=timeout_seconds
            )
        except TimeoutError:
            sub_run.request_cancel()
            timeout_message = (
                f"Sub-agent run timed out after {settings['subagent_timeout_minutes']} minutes"
            )
            result = _result_dict(
                sub_run,
                status=RunStatus.FAILED.value,
                message=timeout_message,
            )
            batch_tracker.mark_fetched(parent_key, session.id, sub_run.id)
            batch_tracker.on_sub_agent_complete(parent_key, sub_run.id, result)
            return tool_failure(
                "subagent_timeout",
                f"Sub-agent run timed out after {settings['subagent_timeout_minutes']} minutes",
            )

        batch_tracker.mark_fetched(parent_key, session.id, sub_run.id)
        batch_tracker.on_sub_agent_complete(parent_key, sub_run.id, result)
        return tool_success(result)
    finally:
        if not slot_registered:
            batch_tracker.release_slot(parent_key)


async def _handle_subagent_result(
    context: ToolContext,
    arguments: JsonObject,
    *,
    runtime: Any,
    batch_tracker: SubAgentBatchTracker,
) -> JsonObject:
    session_id = arguments.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return tool_failure("invalid_arguments", "session_id is required and must be a string")

    agent_id = arguments.get("agent_id", context.agent_id)
    if not isinstance(agent_id, str) or not agent_id:
        return tool_failure("invalid_arguments", "agent_id must be a non-empty string")

    run_id = arguments.get("run_id")
    if run_id is not None and not isinstance(run_id, str):
        return tool_failure("invalid_arguments", "run_id must be a string")

    parent_key = _parent_key(context)
    resolved_run_id = run_id or batch_tracker.run_id_for_session(parent_key, session_id)
    if resolved_run_id is None:
        queued_entry = batch_tracker.queued_entry_for_session(parent_key, session_id)
        if queued_entry is not None:
            return tool_success(_queued_result_dict(queued_entry))

    batch_tracker.mark_fetched(parent_key, session_id, resolved_run_id)
    result: JsonObject
    if resolved_run_id:
        try:
            run = _chat_run_manager(runtime).get(resolved_run_id)
        except RunNotFoundError:
            result = await _poll_result_from_session(
                runtime, agent_id, session_id, run_id=resolved_run_id
            )
        else:
            result = await _wait_for_subagent_result(run)
            if _should_poll_session_result(result):
                session_result = await _poll_result_from_session(
                    runtime, agent_id, session_id, run_id=resolved_run_id
                )
                if _session_result_has_output(session_result) or not result.get("result"):
                    result = session_result
    else:
        result = await _poll_result_from_session(runtime, agent_id, session_id, run_id=None)

    return tool_success(result)


async def _start_subagent_run(
    runtime: Any,
    agent_id: str,
    session_id: str,
    content: str,
    context: ToolContext,
) -> Run:
    _, executor = _make_subagent_executor(
        runtime,
        agent_id,
        session_id,
        content,
        context,
    )
    return await _chat_run_manager(runtime).start(
        agent_id=agent_id,
        session_id=session_id,
        executor=executor,
    )


def _make_subagent_executor(
    runtime: Any,
    agent_id: str,
    session_id: str,
    content: str,
    context: ToolContext,
) -> tuple[Any, RunExecutor]:
    from core.chat import ChatLoop

    # Child Runs must match normal live Runs: without the live loop's
    # attachment resolver, persisted media blocks reach the adapter
    # unresolved; without its compaction service, child runs never
    # auto-compact. Only the nesting depth needs a private loop instance.
    parent_loop = _runtime_streaming_chat_loop(runtime)
    sub_loop = ChatLoop(
        runtime,
        streaming=True,
        attachment_resolver=getattr(parent_loop, "_attachment_resolver", None),
        compaction_service=getattr(parent_loop, "_compaction_service", None),
    )
    sub_loop._nesting_depth = context.nesting_depth + 1  # noqa: SLF001 - planned depth handoff.
    return sub_loop, lambda run: sub_loop._execute_run(run, content)  # noqa: SLF001


def _runtime_streaming_chat_loop(runtime: Any) -> Any | None:
    try:
        return getattr(runtime, "streaming_chat_loop", None)
    except RuntimeError:
        return None


def _track_subagent_completion(
    batch_tracker: SubAgentBatchTracker,
    parent_key: ParentKey,
    run: Run,
) -> None:
    async def complete_when_terminal() -> None:
        result = await _wait_for_subagent_result(run)
        batch_tracker.on_sub_agent_complete(parent_key, run.id, result)

    task = asyncio.create_task(complete_when_terminal())
    task.add_done_callback(
        lambda completed: _log_background_task_result(
            completed,
            "Sub-agent completion tracker failed for "
            f"agent={run.agent_id} session={run.session_id} run={run.id}",
        )
    )


def _track_queued_subagent_completion(
    batch_tracker: SubAgentBatchTracker,
    parent_key: ParentKey,
    item: Any,
) -> None:
    async def complete_when_started_and_terminal() -> None:
        try:
            run = await item.future
        except asyncio.CancelledError:
            batch_tracker.remove_queued(parent_key, item.item_id)
            return
        if not batch_tracker.mark_started(parent_key, item.item_id, run.id):
            return
        result = await _wait_for_subagent_result(run)
        batch_tracker.on_sub_agent_complete(parent_key, run.id, result)

    task = asyncio.create_task(complete_when_started_and_terminal())
    task.add_done_callback(
        lambda completed: _log_background_task_result(
            completed,
            "Queued sub-agent completion tracker failed for "
            f"queue_item={item.item_id} parent={parent_key[0]}/{parent_key[1]}/{parent_key[2]}",
        )
    )


async def _wait_for_subagent_result(run: Run) -> JsonObject:
    try:
        result = await run.wait()
    except RunCancelledError:
        return _cancelled_result_dict(run)
    except Exception as error:
        return _result_dict(run, status=RunStatus.FAILED.value, message=str(error))

    return _result_dict(run, status=run.status.value, message=result)


def _cancelled_result_dict(run: Run) -> JsonObject:
    """Build the result dict for a cancelled child run, threading the cancel reason."""
    if run.cancel_reason == USER_CANCEL_REASON:
        return _result_dict(
            run,
            status=RunStatus.CANCELLED.value,
            message=SUBAGENT_USER_CANCEL_MESSAGE,
            cancelled_by_user=True,
        )
    return _result_dict(run, status=RunStatus.CANCELLED.value, message=None)


def _result_from_session(
    runtime: Any, agent_id: str, session_id: str, run_id: str | None
) -> JsonObject:
    try:
        session = runtime.chat_sessions.get(agent_id, session_id)
        messages = session.load()
    except ChatSessionError as error:
        return {
            "agent_id": agent_id,
            "session_id": session_id,
            "run_id": run_id,
            "status": RunStatus.FAILED.value,
            "result": None,
            "usage": None,
            "note": str(error),
        }

    assistant = _last_assistant_with_content(messages)
    if assistant is None:
        return {
            "agent_id": agent_id,
            "session_id": session_id,
            "run_id": run_id,
            "status": RunStatus.FAILED.value,
            "result": None,
            "usage": None,
            "note": "No assistant output found in sub-agent session.",
        }

    return {
        "agent_id": agent_id,
        "session_id": session_id,
        "run_id": run_id,
        "status": RunStatus.COMPLETED.value,
        "result": assistant.content,
        "usage": assistant.usage,
    }


async def _poll_result_from_session(
    runtime: Any,
    agent_id: str,
    session_id: str,
    run_id: str | None,
    *,
    attempts: int = SESSION_RESULT_RETRY_ATTEMPTS,
    delay_seconds: float = SESSION_RESULT_RETRY_DELAY_SECONDS,
) -> JsonObject:
    bounded_attempts = max(1, attempts)
    result = _result_from_session(runtime, agent_id, session_id, run_id)
    for _ in range(1, bounded_attempts):
        if _session_result_has_output(result):
            return result
        await asyncio.sleep(delay_seconds)
        result = _result_from_session(runtime, agent_id, session_id, run_id)
    return result


def _result_dict(
    run: Run,
    *,
    status: str,
    message: Any,
    cancelled_by_user: bool = False,
) -> JsonObject:
    content: str | None
    usage: JsonObject | None
    if isinstance(message, ChatMessage):
        message_content = message.content
        content = message_content if isinstance(message_content, str) else None
        usage = message.usage
    elif message is None:
        content = None
        usage = None
    else:
        content = str(message)
        usage = None

    data: JsonObject = {
        "agent_id": run.agent_id,
        "session_id": run.session_id,
        "run_id": run.id,
        "status": status,
        "result": content,
        "usage": usage,
    }
    if cancelled_by_user:
        data["cancelled_by_user"] = True
    if status == RunStatus.FAILED.value and not content:
        data["note"] = "No assistant output found in sub-agent session."
    return data


def _queued_result_dict(entry: _SubAgentEntry) -> JsonObject:
    return {
        "agent_id": entry.agent_id,
        "session_id": entry.session_id,
        "run_id": None,
        "queue_item_id": entry.queue_item_id,
        "status": SUBAGENT_STATUS_QUEUED,
        "result": None,
        "usage": None,
    }


def _should_poll_session_result(result: JsonObject) -> bool:
    if result.get("status") == RunStatus.FAILED.value:
        return True
    return result.get("status") == RunStatus.COMPLETED.value and not result.get("result")


def _session_result_has_output(result: JsonObject) -> bool:
    return result.get("status") == RunStatus.COMPLETED.value and bool(result.get("result"))


def _last_assistant_with_content(messages: list[ChatMessage]) -> ChatMessage | None:
    for message in reversed(messages):
        if message.role == "assistant" and message.content:
            return message
    return None


def _load_subagent_settings(runtime: Any) -> dict[str, int]:
    storage = getattr(runtime, "storage", None)
    load_settings = getattr(storage, "load_subagent_settings", None)
    settings = load_settings() if callable(load_settings) else {}
    return {
        "max_subagent_depth": _positive_int(
            settings.get("max_subagent_depth"), DEFAULT_MAX_SUBAGENT_DEPTH
        ),
        "max_subagents_per_turn": _positive_int(
            settings.get("max_subagents_per_turn"), DEFAULT_MAX_SUBAGENTS_PER_TURN
        ),
        "subagent_timeout_minutes": _positive_int(
            settings.get("subagent_timeout_minutes"), DEFAULT_SUBAGENT_TIMEOUT_MINUTES
        ),
    }


def _positive_int(value: Any, default: int) -> int:
    if isinstance(value, int) and value > 0:
        return value
    return default


def _validate_target_agent(runtime: Any, target_agent_id: str) -> JsonObject | None:
    agent_store = getattr(runtime, "agents", None)
    if agent_store is None:
        return None
    get_agent = getattr(agent_store, "get", None)
    if not callable(get_agent):
        return None

    try:
        get_agent(target_agent_id)
    except (AgentNotFoundError, InvalidAgentIdError) as error:
        return tool_failure("agent_not_found", str(error))
    return None


def _mark_subagent_session(
    runtime: Any,
    sub_agent_id: str,
    sub_session_id: str,
    context: ToolContext,
) -> None:
    session_manager = getattr(runtime, "chat_sessions", None)
    get_metadata = getattr(session_manager, "get_metadata", None)
    set_metadata = getattr(session_manager, "set_metadata", None)
    if not callable(get_metadata) or not callable(set_metadata):
        return

    metadata = dict(get_metadata(sub_agent_id, sub_session_id))
    metadata[SUBAGENT_SESSION_METADATA_FLAG] = True
    metadata[SUBAGENT_PARENT_METADATA_KEY] = {
        "agent_id": context.agent_id,
        "session_id": context.session_id,
        "run_id": context.run_id,
        "tool_call_id": context.tool_call_id,
        "tool_call_index": context.tool_call_index,
    }
    set_metadata(sub_agent_id, sub_session_id, metadata)


async def _emit_subagent_session_started(
    context: ToolContext,
    sub_agent_id: str,
    sub_session_id: str,
    *,
    run_id: str | None = None,
    queue_item_id: str | None = None,
    status: str,
) -> None:
    data: JsonObject = {
        "agent_id": sub_agent_id,
        "session_id": sub_session_id,
        "status": status,
    }
    if run_id:
        data["run_id"] = run_id
    if queue_item_id:
        data["queue_item_id"] = queue_item_id

    await context.emit(
        SUBAGENT_SESSION_STARTED_EVENT,
        {
            "tool_call": {
                "id": context.tool_call_id,
                "index": context.tool_call_index,
                "name": context.tool_name,
            },
            "data": data,
        },
    )


def _started_run_from_queue_item(item: Any) -> Run | None:
    if not item.future.done() or item.future.cancelled():
        return None
    return cast(Run, item.future.result())


def _attach_parent_cancellation(
    runtime: Any,
    parent_run_id: str,
    *,
    sub_run: Run | None = None,
    queued_item: Any | None = None,
    queued_agent_id: str | None = None,
    queued_session_id: str | None = None,
    batch_tracker: SubAgentBatchTracker | None = None,
    parent_key: ParentKey | None = None,
) -> None:
    try:
        parent_run = _chat_run_manager(runtime).get(parent_run_id)
    except RunNotFoundError:
        return
    parent_run.add_cancel_callback(
        lambda: _cancel_subagent_child(
            runtime,
            sub_run=sub_run,
            queued_item=queued_item,
            queued_agent_id=queued_agent_id,
            queued_session_id=queued_session_id,
            batch_tracker=batch_tracker,
            parent_key=parent_key,
            parent_reason=parent_run.cancel_reason,
        )
    )


def _cancel_subagent_child(
    runtime: Any,
    *,
    sub_run: Run | None,
    queued_item: Any | None,
    queued_agent_id: str | None,
    queued_session_id: str | None,
    batch_tracker: SubAgentBatchTracker | None,
    parent_key: ParentKey | None,
    parent_reason: str | None = None,
) -> None:
    if sub_run is not None:
        if batch_tracker is not None and parent_key is not None:
            batch_tracker.discard_parent(parent_key)
        sub_run.request_cancel(reason=parent_reason)
        return
    if queued_item is None or queued_agent_id is None or queued_session_id is None:
        return
    if not queued_item.future.done():
        _chat_run_manager(runtime).remove_queued(
            queued_agent_id,
            queued_session_id,
            queued_item.item_id,
        )
        if batch_tracker is not None and parent_key is not None:
            batch_tracker.remove_queued(parent_key, queued_item.item_id)
        return
    try:
        started_run = cast(Run, queued_item.future.result())
    except (asyncio.CancelledError, Exception):
        return
    if batch_tracker is not None and parent_key is not None:
        batch_tracker.discard_parent(parent_key)
    started_run.request_cancel(reason=parent_reason)


def _chat_run_manager(runtime: Any) -> ChatRunManager:
    manager = getattr(runtime, "chat_run_manager", None)
    if manager is not None:
        return cast(ChatRunManager, manager)
    return cast(ChatRunManager, runtime.chat_runs)


def _parent_key(context: ToolContext) -> ParentKey:
    return (context.agent_id, context.session_id, context.run_id)
