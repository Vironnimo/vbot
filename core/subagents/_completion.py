"""Child completion watching, result projection and Parent cancellation linkage."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

from core.chat import (
    ChatMessage,
    ChatSessionError,
)
from core.projects import format_agent_address
from core.runs import (
    Run,
    RunCancelledError,
    RunInterruptedError,
    RunNotFoundError,
    RunStatus,
)
from core.sessions import SessionAddress
from core.subagents._constants import (
    SESSION_RESULT_RETRY_ATTEMPTS,
    SESSION_RESULT_RETRY_DELAY_SECONDS,
    SUBAGENT_ACTIVITY_NOTE_TEMPLATE,
    SUBAGENT_CANCELLED_NOTE_TEMPLATE,
    SUBAGENT_CONTINUATION_CALL_TEMPLATE,
    SUBAGENT_INTERRUPTED_WITHOUT_OUTPUT_NOTE_TEMPLATE,
    SUBAGENT_PARTIAL_RESULT_NOTE_TEMPLATE,
    SUBAGENT_REMOVED_FROM_QUEUE_MESSAGE,
    SUBAGENT_START_FAILED_MESSAGE_TEMPLATE,
    SUBAGENT_USER_CANCEL_MESSAGE,
    USER_CANCEL_REASON,
)
from core.subagents.activity import SubAgentActivity
from core.subagents.tracker import (
    ParentKey,
    SubAgentBatchTracker,
    _log_background_task_result,
)
from core.tools.tools import (
    JsonObject,
    ToolContext,
)
from core.utils.logging import get_logger
from core.utils.paths import model_path

if TYPE_CHECKING:
    from core.runtime.interfaces import RuntimeServices

_LOGGER = get_logger("subagents")


def _register_result_acknowledgement_after_parent_persistence(
    context: ToolContext,
    runtime: RuntimeServices,
    batch_tracker: SubAgentBatchTracker,
    parent_key: ParentKey,
    agent_id: str,
    session_id: str,
    run_id: str | None,
    project_id: str | None,
) -> None:
    """Acknowledge one exact result only after it is durable in the Parent."""
    if not run_id:
        return

    def acknowledge() -> None:
        batch_tracker.mark_fetched(
            parent_key,
            session_id,
            run_id,
            sub_agent_id=agent_id,
            project_id=project_id,
        )
        runtime.chat_sessions.mark_terminal_run_read(
            SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id),
            run_id,
        )

    context.after_result_persisted(acknowledge)


def _track_subagent_completion(
    batch_tracker: SubAgentBatchTracker,
    parent_key: ParentKey,
    run: Run,
    activity_file: str | None,
) -> None:
    async def complete_when_terminal() -> None:
        result = await _wait_for_subagent_result(run, activity_file)
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
    activity: SubAgentActivity | None,
    activity_file: str | None,
    *,
    background: bool,
) -> None:
    """Watch one queued child until it starts and finishes, or never starts.

    A foreground Parent learns inline why queued work never started. A background
    Parent was promised automatic delivery, so unstarted work that the
    coordinator did not remove itself (for example a user removing the Queue item)
    completes with an explanatory result through the ordinary completion notice.
    """

    async def complete_when_started_and_terminal() -> None:
        try:
            run = await item.future
        except asyncio.CancelledError:
            if activity is not None:
                activity.finish_unstarted()
            if not background or not batch_tracker.complete_unstarted(
                parent_key,
                item.item_id,
                _unstarted_result_dict(activity_file, note=SUBAGENT_REMOVED_FROM_QUEUE_MESSAGE),
            ):
                batch_tracker.remove_queued(parent_key, item.item_id)
            return
        except Exception as error:
            if activity is not None:
                activity.finish_unstarted("failed before start")
            _LOGGER.warning(
                "Queued sub-agent failed before start (queue_item=%s): %s",
                item.item_id,
                error,
            )
            if not background or not batch_tracker.complete_unstarted(
                parent_key,
                item.item_id,
                _unstarted_result_dict(
                    activity_file,
                    status=RunStatus.FAILED.value,
                    note=SUBAGENT_START_FAILED_MESSAGE_TEMPLATE.format(error=error),
                ),
            ):
                batch_tracker.remove_queued(parent_key, item.item_id)
            return
        if activity is not None:
            activity.attach(run)
        if not batch_tracker.mark_started(parent_key, item.item_id, run.id):
            return
        result = await _wait_for_subagent_result(run, activity_file)
        batch_tracker.on_sub_agent_complete(parent_key, run.id, result)

    task = asyncio.create_task(complete_when_started_and_terminal())
    task.add_done_callback(
        lambda completed: _log_background_task_result(
            completed,
            "Queued sub-agent completion tracker failed for "
            f"queue_item={item.item_id} parent={parent_key[0]}/{parent_key[1]}/{parent_key[2]}",
        )
    )


def _unstarted_result_dict(
    activity_file: str | None,
    *,
    note: str,
    status: str = RunStatus.CANCELLED.value,
) -> JsonObject:
    return {
        "run_id": None,
        "status": status,
        "result": None,
        "usage": None,
        "activity_file": activity_file,
        "note": note,
    }


async def _wait_for_subagent_result(
    run: Run,
    activity_file: str | None = None,
) -> JsonObject:
    try:
        result = await run.wait()
    except RunCancelledError:
        return _cancelled_result_dict(run, activity_file)
    except RunInterruptedError as error:
        return _result_dict(
            run,
            status=RunStatus.INTERRUPTED.value,
            message=error.result,
            activity_file=activity_file,
        )
    except Exception as error:
        return _result_dict(
            run,
            status=RunStatus.FAILED.value,
            message=str(error),
            activity_file=activity_file,
        )

    return _result_dict(
        run,
        status=run.status.value,
        message=result,
        activity_file=activity_file,
    )


def _cancelled_result_dict(run: Run, activity_file: str | None = None) -> JsonObject:
    """Build the result dict for a cancelled child run, threading the cancel reason."""
    if run.cancel_reason == USER_CANCEL_REASON:
        return _result_dict(
            run,
            status=RunStatus.CANCELLED.value,
            message=SUBAGENT_USER_CANCEL_MESSAGE,
            cancelled_by_user=True,
            activity_file=activity_file,
        )
    return _result_dict(
        run,
        status=RunStatus.CANCELLED.value,
        message=None,
        activity_file=activity_file,
    )


async def _result_from_session(
    runtime: RuntimeServices,
    agent_id: str,
    session_id: str,
    run_id: str | None,
    project_id: str | None = None,
    activity_file: str | None = None,
) -> tuple[JsonObject, bool]:
    try:
        # Read the child session under its target project anchor;
        # ``None`` keeps the identity layout.
        session = await runtime.chat_sessions.get_async(
            SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)
        )
        run_result = await session.load_run_result_async(
            run_id=run_id, require_latest=run_id is None
        )
    except ChatSessionError as error:
        return (
            _with_target_project(
                {
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "run_id": run_id,
                    "status": RunStatus.FAILED.value,
                    "result": None,
                    "usage": None,
                    "activity_file": activity_file,
                    "note": str(error),
                },
                project_id,
            ),
            False,
        )

    if run_result is None:
        return (
            _with_target_project(
                {
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "run_id": run_id,
                    "status": RunStatus.FAILED.value,
                    "result": None,
                    "usage": None,
                    "activity_file": activity_file,
                    "note": "No terminal Run summary found in sub-agent session.",
                },
                project_id,
            ),
            False,
        )

    assistant, summary = run_result.assistant, run_result.summary
    result = _with_target_project(
        {
            "agent_id": agent_id,
            "session_id": session_id,
            "run_id": summary.run_id,
            "status": summary.status,
            "result": assistant.content if assistant is not None else None,
            "usage": assistant.usage if assistant is not None else None,
            "activity_file": activity_file,
        },
        project_id,
    )
    if assistant is not None:
        _add_interruption_details(result, assistant)
    if assistant is None:
        result["note"] = "Sub-agent Run finished without assistant output."
    return result, True


async def _poll_result_from_session(
    runtime: RuntimeServices,
    agent_id: str,
    session_id: str,
    run_id: str | None,
    *,
    project_id: str | None = None,
    activity_file: str | None = None,
    attempts: int = SESSION_RESULT_RETRY_ATTEMPTS,
    delay_seconds: float = SESSION_RESULT_RETRY_DELAY_SECONDS,
) -> tuple[JsonObject, bool]:
    bounded_attempts = max(1, attempts)
    result, terminal = await _result_from_session(
        runtime,
        agent_id,
        session_id,
        run_id,
        project_id,
        activity_file,
    )
    for _ in range(1, bounded_attempts):
        if terminal:
            return result, True
        await asyncio.sleep(delay_seconds)
        result, terminal = await _result_from_session(
            runtime,
            agent_id,
            session_id,
            run_id,
            project_id,
            activity_file,
        )
    return result, terminal


def _result_dict(
    run: Run,
    *,
    status: str,
    message: Any,
    cancelled_by_user: bool = False,
    activity_file: str | None = None,
) -> JsonObject:
    content: str | None
    usage: JsonObject | None
    assistant_message: ChatMessage | None = None
    if isinstance(message, ChatMessage):
        assistant_message = message
        message_content = message.content
        content = message_content if isinstance(message_content, str) else None
        usage = message.usage
    elif message is None:
        content = None
        usage = None
    else:
        content = str(message)
        usage = None

    data = _with_target_project(
        {
            "agent_id": run.agent_id,
            "session_id": run.session_id,
            "run_id": run.id,
            "status": status,
            "result": content,
            "usage": usage,
            "activity_file": activity_file,
        },
        run.project_id,
    )
    if assistant_message is not None:
        _add_interruption_details(data, assistant_message)
    if cancelled_by_user:
        data["cancelled_by_user"] = True
    if status == RunStatus.FAILED.value and not content:
        data["note"] = "No assistant output found in sub-agent session."
    if status == RunStatus.INTERRUPTED.value and not content:
        data["note"] = SUBAGENT_INTERRUPTED_WITHOUT_OUTPUT_NOTE_TEMPLATE.format(
            continuation=_continuation_call(run.agent_id, run.project_id, run.session_id)
        )
    if status == RunStatus.CANCELLED.value and not content and not cancelled_by_user:
        data["note"] = _cancelled_continuation_note(run.agent_id, run.project_id, run.session_id)
    return data


def _continuation_call(agent_id: str, project_id: str | None, session_id: str) -> str:
    """Name the exact ``subagent`` arguments that continue one child Session.

    The Tool resolves a bare id in the caller's scope, so the note names the
    same qualified address that the result's ``agent_id`` field carries.
    """
    return SUBAGENT_CONTINUATION_CALL_TEMPLATE.format(
        agent_id=format_agent_address(agent_id, project_id),
        session_id=session_id,
    )


def _cancelled_continuation_note(agent_id: str, project_id: str | None, session_id: str) -> str:
    return SUBAGENT_CANCELLED_NOTE_TEMPLATE.format(
        continuation=_continuation_call(agent_id, project_id, session_id)
    )


def _add_interruption_details(data: JsonObject, message: ChatMessage) -> None:
    """Make a preserved partial unmistakable to Sub-Agent result consumers."""
    if not message.interrupted:
        return
    data["interrupted"] = True
    if message.interruption_cause is not None:
        data["interruption_cause"] = message.interruption_cause
        cause_text = f" by {message.interruption_cause}"
    else:
        cause_text = ""
    data["note"] = SUBAGENT_PARTIAL_RESULT_NOTE_TEMPLATE.format(
        cause=cause_text,
        continuation=_continuation_call(
            data["agent_id"], data.get("project_id"), data["session_id"]
        ),
    )


def _should_poll_session_result(result: JsonObject) -> bool:
    return result.get("status") in {
        RunStatus.FAILED.value,
        RunStatus.CANCELLED.value,
        RunStatus.INTERRUPTED.value,
    } or (result.get("status") == RunStatus.COMPLETED.value and not result.get("result"))


def _session_result_has_output(result: JsonObject) -> bool:
    return bool(result.get("result"))


def _started_run_from_queue_item(item: Any) -> Run | None:
    if not item.future.done() or item.future.cancelled():
        return None
    return cast(Run, item.future.result())


def _attach_parent_cancellation(
    runtime: RuntimeServices,
    parent_run_id: str,
    *,
    sub_run: Run | None = None,
    queued_item: Any | None = None,
    queued_agent_id: str | None = None,
    queued_session_id: str | None = None,
    queued_project_id: str | None = None,
    batch_tracker: SubAgentBatchTracker | None = None,
    parent_key: ParentKey | None = None,
) -> None:
    try:
        parent_run = runtime.chat_run_manager.get(parent_run_id)
    except RunNotFoundError:
        return
    parent_run.add_cancel_callback(
        lambda: _cancel_subagent_child(
            runtime,
            sub_run=sub_run,
            queued_item=queued_item,
            queued_agent_id=queued_agent_id,
            queued_session_id=queued_session_id,
            queued_project_id=queued_project_id,
            batch_tracker=batch_tracker,
            parent_key=parent_key,
            parent_reason=parent_run.cancel_reason,
        )
    )


def _cancel_subagent_child(
    runtime: RuntimeServices,
    *,
    sub_run: Run | None,
    queued_item: Any | None,
    queued_agent_id: str | None,
    queued_session_id: str | None,
    queued_project_id: str | None = None,
    batch_tracker: SubAgentBatchTracker | None,
    parent_key: ParentKey | None,
    parent_reason: str | None = None,
) -> None:
    if sub_run is not None:
        sub_run.request_cancel(reason=parent_reason)
        return
    if queued_item is None or queued_agent_id is None or queued_session_id is None:
        return
    if not queued_item.future.done():
        runtime.chat_run_manager.remove_queued(
            queued_agent_id,
            queued_session_id,
            queued_item.item_id,
            project_id=queued_project_id,
        )
        if batch_tracker is not None and parent_key is not None:
            batch_tracker.remove_queued(parent_key, queued_item.item_id)
        return
    try:
        started_run = cast(Run, queued_item.future.result())
    except (asyncio.CancelledError, Exception):
        return
    started_run.request_cancel(reason=parent_reason)


def _with_target_project(data: JsonObject, project_id: str | None) -> JsonObject:
    if project_id is not None:
        data["project_id"] = project_id
    return data


def _activity_file(activity: SubAgentActivity | None) -> str | None:
    if activity is None:
        return None
    return model_path(activity.path.resolve())


def _with_activity_note(data: JsonObject, activity_file: str | None) -> JsonObject:
    """Project one spawn result's activity reference onto its single carrier.

    The note is the only spawn-result carrier of the path: the bare
    ``activity_file`` field rides along in the internal result dicts (status
    snapshots keep it as their documented contract), so it is removed here to
    keep one path from appearing twice in one result.
    """
    data.pop("activity_file", None)
    if activity_file is not None:
        data["activity_note"] = SUBAGENT_ACTIVITY_NOTE_TEMPLATE.format(path=activity_file)
    return data


_INTERNAL_HANDLE_FIELDS = frozenset({"run_id", "queue_item_id"})
_PUBLIC_IDENTITY_FIELDS = frozenset({"id", "agent_id", "session_id", "project_id"})


def _public_subagent_result(
    work_id: str,
    agent_id: str,
    project_id: str | None,
    session_id: str,
    result: JsonObject,
    *,
    delivery: str | None = None,
    note: str | None = None,
) -> JsonObject:
    """Project one child descriptor or result for the calling Agent.

    ``agent_id`` is exactly the value the Tool's ``agent_id`` argument accepts
    for this child: a bare id resolves in the caller's scope, so a Project child
    is addressed as ``agent@project``. ``project_id`` stays its own field.
    Internal Run and Queue handles never appear; events and Session metadata
    keep their bare ids and do not pass through here.
    """
    data: JsonObject = {
        "id": work_id,
        "agent_id": format_agent_address(agent_id, project_id),
        "session_id": session_id,
    }
    data.update(
        (key, value)
        for key, value in result.items()
        if key not in _INTERNAL_HANDLE_FIELDS and key not in _PUBLIC_IDENTITY_FIELDS
    )
    if project_id is not None:
        data["project_id"] = project_id
    if delivery is not None:
        data["delivery"] = delivery
    if note is not None:
        data["note"] = note
    return data
