"""Owned Sub-Agent status, inspection and exact-child cancellation."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from core.runs import (
    Run,
    RunNotFoundError,
    RunStatus,
)
from core.sessions import SessionAddress
from core.subagents._completion import (
    _add_interruption_details,
    _poll_result_from_session,
    _public_subagent_result,
    _register_result_acknowledgement_after_parent_persistence,
    _result_dict,
    _session_result_has_output,
    _should_poll_session_result,
    _wait_for_subagent_result,
    _with_target_project,
    _without_internal_handles,
)
from core.subagents._constants import (
    PARENT_AGENT_CANCEL_REASON,
    SUBAGENT_STATUS_CHANGED_EVENT,
    SUBAGENT_STATUS_QUEUED,
    SUBAGENT_STATUS_QUEUED_NOTE,
    SUBAGENT_STATUS_RUNNING_NOTE,
)
from core.subagents.tracker import (
    ParentKey,
    SubAgentBatchTracker,
    _SubAgentEntry,
)
from core.tools.arguments import (
    ToolArgumentError,
    required_string,
)
from core.tools.tools import (
    JsonObject,
    ToolContext,
    tool_failure,
    tool_success,
)

if TYPE_CHECKING:
    from core.runtime.interfaces import RuntimeServices


def _inspect_subagent_work(
    runtime: RuntimeServices,
    agent_id: str,
    session_id: str,
    work_id: str,
    *,
    project_id: str | None = None,
) -> JsonObject | None:
    address = SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)
    session = runtime.chat_sessions.get(address)
    active_run = runtime.chat_run_manager.active_run(
        agent_id=agent_id,
        session_id=session_id,
        project_id=project_id,
    )
    if active_run is not None and active_run.work_id == work_id:
        return _subagent_work_inspection(
            work_id,
            agent_id,
            session_id,
            project_id,
            run_id=active_run.id,
            status=active_run.status.value,
            started_at=active_run.created_at,
            tool_name=_latest_tool_name_from_run(active_run),
        )

    queued_item = next(
        (
            item
            for item in reversed(
                runtime.chat_run_manager.list_queued(
                    agent_id,
                    session_id,
                    project_id=project_id,
                )
            )
            if item.admission.work_id == work_id
        ),
        None,
    )
    if queued_item is not None:
        return _subagent_work_inspection(
            work_id,
            agent_id,
            session_id,
            project_id,
            run_id=None,
            status=SUBAGENT_STATUS_QUEUED,
        )

    run_result = session.load_run_result(work_id=work_id)
    if run_result is None:
        return None
    assistant, summary = run_result.assistant, run_result.summary
    if summary.run_id is None or summary.status is None:
        return None
    inspection = _subagent_work_inspection(
        work_id,
        agent_id,
        session_id,
        project_id,
        run_id=summary.run_id,
        status=summary.status,
        result=assistant.content if assistant is not None else None,
        usage=assistant.usage if assistant is not None else None,
        timing=summary.timing,
        tool_name=run_result.latest_tool_name,
    )
    if assistant is not None:
        _add_interruption_details(inspection, assistant)
    return inspection


def _subagent_work_inspection(
    work_id: str,
    agent_id: str,
    session_id: str,
    project_id: str | None,
    *,
    run_id: str | None,
    status: str,
    result: str | list[Any] | None = None,
    usage: JsonObject | None = None,
    timing: JsonObject | None = None,
    started_at: str | None = None,
    tool_name: str | None = None,
) -> JsonObject:
    inspection: JsonObject = {
        "id": work_id,
        "agent_id": agent_id,
        "session_id": session_id,
        "run_id": run_id,
        "status": status,
        "result": result,
        "usage": usage,
        "timing": timing,
        "started_at": started_at,
        "tool_name": tool_name,
    }
    return _with_target_project(inspection, project_id)


def _latest_tool_name_from_run(run: Run) -> str | None:
    for event in reversed(run.events):
        if event.type != "tool_call_started":
            continue
        tool_call = event.payload.get("tool_call")
        if not isinstance(tool_call, dict):
            continue
        name = tool_call.get("name")
        if isinstance(name, str) and name:
            return name
    return None


async def _handle_subagent_cancel(
    context: ToolContext,
    arguments: JsonObject,
    *,
    runtime: RuntimeServices,
    batch_tracker: SubAgentBatchTracker,
) -> JsonObject:
    """Cancel one exact owned child through its stable public work id."""
    unknown_arguments = set(arguments) - {"action", "id"}
    if unknown_arguments:
        names = ", ".join(sorted(unknown_arguments))
        return tool_failure("invalid_arguments", f"Unknown argument(s): {names}")

    try:
        work_id = required_string(arguments.get("id"), field_name="id")
    except ToolArgumentError as error:
        return tool_failure("invalid_arguments", str(error))

    owned = batch_tracker.owned_entry(
        context.agent_id,
        context.session_id,
        context.project_id,
        work_id,
    )
    if owned is None:
        return _subagent_not_owned_failure(work_id)
    parent_key, entry = owned
    if entry.complete:
        return tool_failure(
            "subagent_not_running",
            f"Sub-Agent work is already complete: {work_id}",
        )

    if entry.run_id is not None:
        return await _cancel_owned_subagent_run(
            context,
            runtime,
            work_id,
            entry,
        )

    queue_item_id = entry.queue_item_id
    if queue_item_id is None:
        return tool_failure(
            "subagent_not_running",
            f"Sub-Agent work has no queued item or active Run: {work_id}",
        )
    removed = runtime.chat_run_manager.remove_queued(
        entry.agent_id,
        entry.session_id,
        queue_item_id,
        project_id=entry.project_id,
    )
    if removed:
        batch_tracker.remove_queued(parent_key, queue_item_id)
        data = _cancelled_subagent_descriptor(
            work_id,
            entry.agent_id,
            entry.project_id,
            entry.session_id,
            queue_item_id=queue_item_id,
        )
        await _emit_subagent_status_changed(context, data)
        return tool_success(_without_internal_handles(data))

    # The queued work may have become a Run while cancellation was resolving.
    if entry.run_id is None:
        await asyncio.sleep(0)
    if entry.run_id is None:
        return tool_failure(
            "subagent_not_running",
            f"Sub-Agent work is no longer queued and has no active Run: {work_id}",
        )
    return await _cancel_owned_subagent_run(
        context,
        runtime,
        work_id,
        entry,
    )


async def _cancel_owned_subagent_run(
    context: ToolContext,
    runtime: RuntimeServices,
    work_id: str,
    entry: _SubAgentEntry,
) -> JsonObject:
    run_id = entry.run_id
    if run_id is None:
        return tool_failure("subagent_not_running", f"Sub-Agent work has not started: {work_id}")
    try:
        run = runtime.chat_run_manager.get(run_id)
    except RunNotFoundError:
        return tool_failure("subagent_not_running", f"Sub-Agent work is not running: {work_id}")
    if not _run_matches_target(
        run,
        entry.agent_id,
        entry.session_id,
        entry.project_id,
    ):
        return tool_failure(
            "subagent_target_mismatch",
            f"Sub-Agent work resolved to the wrong Session: {work_id}",
        )
    if run.status != RunStatus.RUNNING:
        return tool_failure(
            "subagent_not_running",
            f"Sub-Agent work is already {run.status.value}: {work_id}",
        )

    cancelled_run = await runtime.chat_run_manager.cancel(
        run_id,
        reason=PARENT_AGENT_CANCEL_REASON,
    )
    if cancelled_run.status != RunStatus.CANCELLED:
        return tool_failure(
            "subagent_not_running",
            f"Sub-Agent work reached {cancelled_run.status.value} before cancellation: {work_id}",
        )

    data = _cancelled_subagent_descriptor(
        work_id,
        entry.agent_id,
        entry.project_id,
        entry.session_id,
        run_id=run_id,
        queue_item_id=entry.queue_item_id,
    )
    await _emit_subagent_status_changed(context, data)
    return tool_success(_without_internal_handles(data))


def _cancelled_subagent_descriptor(
    work_id: str,
    target_agent_id: str,
    target_project_id: str | None,
    session_id: str,
    *,
    run_id: str | None = None,
    queue_item_id: str | None = None,
) -> JsonObject:
    data: JsonObject = {
        "id": work_id,
        "agent_id": target_agent_id,
        "session_id": session_id,
        "status": RunStatus.CANCELLED.value,
    }
    if target_project_id is not None:
        data["project_id"] = target_project_id
    if run_id is not None:
        data["run_id"] = run_id
    if queue_item_id is not None:
        data["queue_item_id"] = queue_item_id
    return data


def _subagent_not_owned_failure(work_id: str) -> JsonObject:
    return tool_failure(
        "subagent_not_owned",
        f"Sub-Agent work is not owned by this Parent Agent Session: {work_id}",
    )


async def _handle_subagent_status(
    context: ToolContext,
    arguments: JsonObject,
    *,
    runtime: RuntimeServices,
    batch_tracker: SubAgentBatchTracker,
) -> JsonObject:
    unknown_arguments = set(arguments) - {"action", "id"}
    if unknown_arguments:
        names = ", ".join(sorted(unknown_arguments))
        return tool_failure("invalid_arguments", f"Unknown argument(s): {names}")

    if "id" not in arguments:
        entries = batch_tracker.owned_entries(
            context.agent_id, context.session_id, context.project_id
        )
        snapshots: list[JsonObject] = []
        for parent_key, entry in entries:
            snapshot = await _subagent_status_snapshot(
                context, parent_key, entry, runtime=runtime, batch_tracker=batch_tracker
            )
            snapshots.append(
                snapshot["data"]
                if snapshot["ok"]
                else {"id": entry.work_id, "error": snapshot["error"]}
            )
        return tool_success({"subagents": snapshots})

    try:
        work_id = required_string(arguments.get("id"), field_name="id")
    except ToolArgumentError as error:
        return tool_failure("invalid_arguments", str(error))

    owned = batch_tracker.owned_entry(
        context.agent_id,
        context.session_id,
        context.project_id,
        work_id,
    )
    if owned is None:
        return _subagent_not_owned_failure(work_id)
    parent_key, entry = owned
    return await _subagent_status_snapshot(
        context, parent_key, entry, runtime=runtime, batch_tracker=batch_tracker
    )


async def _subagent_status_snapshot(
    context: ToolContext,
    parent_key: ParentKey,
    entry: _SubAgentEntry,
    *,
    runtime: RuntimeServices,
    batch_tracker: SubAgentBatchTracker,
) -> JsonObject:
    """Resolve one captured entry and acknowledge only after Parent persistence."""
    work_id = entry.work_id

    if entry.run_id is None:
        return tool_success(
            _public_subagent_result(
                work_id,
                entry.agent_id,
                entry.project_id,
                entry.session_id,
                {
                    "status": SUBAGENT_STATUS_QUEUED,
                    "activity_file": entry.activity_file,
                },
                note=SUBAGENT_STATUS_QUEUED_NOTE,
            )
        )

    result: JsonObject
    terminal_result = False
    try:
        run = runtime.chat_run_manager.get(entry.run_id)
    except RunNotFoundError:
        if entry.complete and entry.result is not None:
            result = dict(entry.result)
            terminal_result = True
        else:
            result, terminal_result = await _poll_result_from_session(
                runtime,
                entry.agent_id,
                entry.session_id,
                run_id=entry.run_id,
                project_id=entry.project_id,
                activity_file=entry.activity_file,
            )
    else:
        if not _run_matches_target(run, entry.agent_id, entry.session_id, entry.project_id):
            return tool_failure(
                "run_not_found",
                f"Sub-Agent work resolved to the wrong Session: {work_id}",
            )
        if run.status == RunStatus.RUNNING:
            return tool_success(
                _public_subagent_result(
                    work_id,
                    entry.agent_id,
                    entry.project_id,
                    entry.session_id,
                    _result_dict(
                        run,
                        status=RunStatus.RUNNING.value,
                        message=None,
                        activity_file=entry.activity_file,
                    ),
                    note=SUBAGENT_STATUS_RUNNING_NOTE,
                )
            )
        result = await _wait_for_subagent_result(run, entry.activity_file)
        terminal_result = True
        if _should_poll_session_result(result):
            session_result, session_terminal = await _poll_result_from_session(
                runtime,
                entry.agent_id,
                entry.session_id,
                run_id=entry.run_id,
                project_id=entry.project_id,
                activity_file=entry.activity_file,
            )
            if session_terminal and (
                _session_result_has_output(session_result) or not result.get("result")
            ):
                result = session_result

    if terminal_result:
        _register_result_acknowledgement_after_parent_persistence(
            context,
            runtime,
            batch_tracker,
            parent_key,
            entry.agent_id,
            entry.session_id,
            entry.run_id,
            entry.project_id,
        )
    return tool_success(
        _public_subagent_result(
            work_id,
            entry.agent_id,
            entry.project_id,
            entry.session_id,
            result,
        )
    )


async def _emit_subagent_status_changed(
    context: ToolContext,
    data: JsonObject,
) -> None:
    await context.emit(
        SUBAGENT_STATUS_CHANGED_EVENT,
        {
            "tool_call": {
                "id": context.tool_call_id,
                "index": context.tool_call_index,
                "name": context.tool_name,
            },
            "data": dict(data),
        },
    )


def _run_matches_target(
    run: Run,
    agent_id: str,
    session_id: str,
    project_id: str | None,
) -> bool:
    return (
        run.agent_id == agent_id and run.session_id == session_id and run.project_id == project_id
    )
