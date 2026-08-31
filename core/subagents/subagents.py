"""Sub-agent orchestration: lifecycle control, result lookup, and parent-run linkage."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from core.chat import (
    ChatMessage,
    ChatSessionError,
)
from core.projects import (
    AgentResolutionError,
    AgentRunOverrides,
    InvalidAgentAddressError,
    ModelConfigurationError,
    format_agent_address,
    parse_agent_address,
    resolve_working_project_id,
)
from core.runs import (
    ActiveRunError,
    Run,
    RunAdmission,
    RunCancelledError,
    RunExecutor,
    RunInterruptedError,
    RunKind,
    RunNotFoundError,
    RunStatus,
)
from core.sessions import SessionAddress
from core.settings import SettingsValidationError, validate_thinking_effort
from core.subagents.activity import SubAgentActivity
from core.subagents.catalog import SubAgentPromptTarget, build_subagent_prompt_targets
from core.subagents.tracker import (
    _LOGGER as _LOGGER,
)
from core.subagents.tracker import (
    ParentKey,
    SubAgentBatchTracker,
    _log_background_task_result,
    _SubAgentEntry,
)
from core.tools.arguments import (
    ToolArgumentError,
    optional_string,
    required_string,
)
from core.tools.availability import subagent_allowed_agents
from core.tools.tools import (
    JsonObject,
    ToolContext,
    tool_failure,
    tool_success,
)
from core.utils.paths import model_path

if TYPE_CHECKING:
    from core.chat import ChatLoop
    from core.runtime.interfaces import RuntimeServices

DEFAULT_MAX_SUBAGENT_DEPTH = 4
DEFAULT_MAX_SUBAGENTS_PER_TURN = 8
DEFAULT_SUBAGENT_TIMEOUT_MINUTES = 60
SECONDS_PER_MINUTE = 60
SESSION_RESULT_RETRY_ATTEMPTS = 3
SESSION_RESULT_RETRY_DELAY_SECONDS = 0.05
SUBAGENT_STATUS_QUEUED = "queued"
SUBAGENT_SESSION_STARTED_EVENT = "subagent_session_started"
SUBAGENT_STATUS_CHANGED_EVENT = "subagent_status_changed"
SUBAGENT_SESSION_METADATA_FLAG = "is_subagent_session"
SUBAGENT_PARENT_METADATA_KEY = "subagent_parent"
USER_CANCEL_REASON = "user"
PARENT_AGENT_CANCEL_REASON = "parent_agent"
SUBAGENT_USER_CANCEL_MESSAGE = "Cancelled by the user"
SUBAGENT_WORK_ID_PREFIX = "sub_"
SUBAGENT_SESSION_TITLE_MAX_CHARACTERS = 48
SUBAGENT_ACTIVITY_NOTE_TEMPLATE = (
    "Current Sub-Agent activity is available at {path}. Read this file if the Sub-Agent's "
    "status or progress becomes relevant."
)
TOP_LEVEL_BACKGROUND_NOTE = (
    "This Sub-Agent is running in the background; vBot monitors it and notifies you "
    "with the result once it finishes. Continue other work, or finish your turn to "
    "wait for a result."
)
TOP_LEVEL_QUEUED_BACKGROUND_NOTE = (
    "This Sub-Agent is queued because its Session is busy with another task; vBot "
    "starts it automatically when the Session is free and notifies you with the "
    "result once it finishes. Continue other work, or finish your turn to wait for a "
    "result."
)
SUBAGENT_STATUS_RUNNING_NOTE = (
    "Still running; the result is delivered automatically. Continue other work, or "
    "finish your turn to wait for it. Repeated status calls do not make it finish "
    "faster."
)
SUBAGENT_STATUS_QUEUED_NOTE = (
    "Queued: the Sub-Agent's Session is busy with another task; vBot starts this "
    "work automatically when the Session is free. The result is delivered "
    "automatically. Continue other work, or finish your turn to wait for it."
)

# Cascade policy switch: when True, a parent Run cancellation cascades to every
# sub-agent child including background ones (legacy behaviour). When False,
# only foreground sub-agent spawns (and queued-then-started foreground waits) get
# the cascade; background spawns survive the parent cancel.
# FLIP-BACK: set CASCADE_BACKGROUND_CHILDREN = True to restore the old behaviour.
CASCADE_BACKGROUND_CHILDREN = False


def _should_register_parent_cascade(background: bool) -> bool:
    """Return whether a spawn should register a parent-cancel cascade callback.

    The cascade policy is a single flip point: see ``CASCADE_BACKGROUND_CHILDREN``.
    """
    return (not background) or CASCADE_BACKGROUND_CHILDREN


class SubAgentCoordinator:
    """Coordinate sub-agent Run lifecycle, result lookup, and parent linkage."""

    def __init__(
        self,
        runtime: RuntimeServices,
        trigger_service: Any,
        *,
        batch_tracker: SubAgentBatchTracker | None = None,
        sessions: Any | None = None,
    ) -> None:
        self._runtime = runtime
        self._batch_tracker = batch_tracker or SubAgentBatchTracker(
            trigger_service,
            sessions=sessions,
        )

    @property
    def batch_tracker(self) -> SubAgentBatchTracker:
        """Return the in-memory tracker used for this runtime instance."""
        return self._batch_tracker

    def prompt_targets(
        self,
        agent: Any,
        project_id: str | None,
    ) -> list[SubAgentPromptTarget]:
        """Return additional targets for the Tool-owned System Prompt block."""
        return build_subagent_prompt_targets(self._runtime, agent, project_id)

    async def spawn(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        """Handle a public Sub-Agent lifecycle operation."""
        return await _handle_subagent(
            context,
            arguments,
            runtime=self._runtime,
            batch_tracker=self._batch_tracker,
        )

    def inspect(
        self,
        agent_id: str,
        session_id: str,
        work_id: str,
        *,
        project_id: str | None = None,
    ) -> JsonObject | None:
        """Return the exact UI projection for one durable Sub-Agent work id."""
        return _inspect_subagent_work(
            self._runtime,
            agent_id,
            session_id,
            work_id,
            project_id=project_id,
        )


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

    messages = session.load()
    summary = next(
        (
            message
            for message in reversed(messages)
            if message.role == "run_summary" and message.work_id == work_id
        ),
        None,
    )
    if summary is None or summary.run_id is None or summary.status is None:
        return None
    terminal_result = _terminal_session_result(messages, summary.run_id)
    if terminal_result is None:
        return None
    assistant, _ = terminal_result
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
        tool_name=_latest_tool_name_from_segment(messages, summary.run_id),
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


def _latest_tool_name_from_segment(
    messages: list[ChatMessage],
    run_id: str,
) -> str | None:
    summary_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if messages[index].role == "run_summary" and messages[index].run_id == run_id
        ),
        None,
    )
    if summary_index is None:
        return None
    segment_start = next(
        (
            index + 1
            for index in range(summary_index - 1, -1, -1)
            if messages[index].role == "run_summary"
        ),
        0,
    )
    for message in reversed(messages[segment_start:summary_index]):
        for tool_call in reversed(message.tool_calls or []):
            if tool_call.name:
                return tool_call.name
    return None


async def _handle_subagent(
    context: ToolContext,
    arguments: JsonObject,
    *,
    runtime: RuntimeServices,
    batch_tracker: SubAgentBatchTracker,
) -> JsonObject:
    try:
        action = required_string(arguments.get("action"), field_name="action")
    except ToolArgumentError as error:
        return tool_failure("invalid_arguments", str(error))

    if action == "cancel":
        return await _handle_subagent_cancel(
            context,
            arguments,
            runtime=runtime,
            batch_tracker=batch_tracker,
        )
    if action == "status":
        return await _handle_subagent_status(
            context,
            arguments,
            runtime=runtime,
            batch_tracker=batch_tracker,
        )
    if action != "run":
        return tool_failure(
            "invalid_arguments",
            "action must be one of: run, status, cancel",
        )

    unknown_arguments = set(arguments) - {
        "action",
        "content",
        "description",
        "agent_id",
        "session_id",
        "model",
        "thinking_effort",
    }
    if unknown_arguments:
        names = ", ".join(sorted(unknown_arguments))
        return tool_failure("invalid_arguments", f"Unknown argument(s): {names}")

    content = arguments.get("content")
    if not isinstance(content, str) or not content.strip():
        return tool_failure(
            "invalid_arguments", "content is required and must be a non-empty string"
        )

    try:
        explicit_agent_address = optional_string(arguments.get("agent_id"), field_name="agent_id")
        description = optional_string(arguments.get("description"), field_name="description")
        session_id = optional_string(arguments.get("session_id"), field_name="session_id")
        if session_id is not None and explicit_agent_address is None:
            return tool_failure(
                "invalid_arguments",
                "agent_id is required with session_id because Sub-Agent Sessions are "
                "Agent-scoped; repeat both values returned by the original subagent call",
            )
        target_agent_address = explicit_agent_address or context.agent_id
        target_agent_id, target_project_id = _resolve_target_address(
            target_agent_address, context.project_id
        )
        run_overrides = _parse_agent_run_overrides(arguments)
    except (ToolArgumentError, InvalidAgentAddressError, SettingsValidationError) as error:
        return tool_failure("invalid_arguments", str(error))

    background = context.nesting_depth == 0
    if not _target_is_allowed(context, target_agent_id, target_project_id):
        return tool_failure("agent_not_allowed", "target agent is not allowed for this parent")

    if (
        session_id is not None
        and target_agent_id == context.agent_id
        and target_project_id == context.project_id
        and session_id == context.session_id
    ):
        return tool_failure(
            "invalid_arguments",
            "cannot target the calling agent's own active session",
        )

    validation_error = _validate_target_agent(
        runtime,
        target_agent_id,
        target_project_id,
        run_overrides=run_overrides,
    )
    if validation_error is not None:
        return validation_error

    settings = _load_subagent_settings(runtime)
    parent_key = _parent_key(context)
    if context.nesting_depth >= settings["max_subagent_depth"]:
        return tool_failure(
            "subagent_depth_exceeded",
            f"Sub-agent nesting depth limit exceeded: {settings['max_subagent_depth']}",
        )
    if not batch_tracker.reserve_slot(
        parent_key, settings["max_subagents_per_turn"], context.project_id
    ):
        return tool_failure(
            "subagent_limit_exceeded",
            f"Sub-agent per-turn limit exceeded: {settings['max_subagents_per_turn']}",
        )

    slot_registered = False
    work_id = _new_subagent_work_id()
    activity: SubAgentActivity | None = None
    activity_handed_off = False
    try:
        if context.is_cancelled():
            return tool_failure("run_cancelled", "Parent run was cancelled before sub-agent spawn")

        if session_id is None:
            session = runtime.chat_sessions.create(target_agent_id, project_id=target_project_id)
            runtime.chat_sessions.set_auto_title(
                SessionAddress(
                    project_id=target_project_id,
                    agent_id=target_agent_id,
                    session_id=session.id,
                ),
                _subagent_session_title(description, content),
            )
        else:
            try:
                session = runtime.chat_sessions.get(
                    SessionAddress(
                        project_id=target_project_id,
                        agent_id=target_agent_id,
                        session_id=session_id,
                    )
                )
            except ChatSessionError:
                return tool_failure("session_not_found", f"session does not exist: {session_id}")

        activity = SubAgentActivity.create(
            runtime.storage.temporary_files,
            agent_id=target_agent_id,
            session_id=session.id,
        )
        activity_file = _activity_file(activity)
        _mark_subagent_session(
            runtime,
            target_agent_id,
            target_project_id,
            session.id,
            work_id,
            context,
        )
        await _emit_subagent_session_started(
            context,
            work_id,
            target_agent_id,
            target_project_id,
            session.id,
            status=RunStatus.RUNNING.value,
            delivery="automatic" if background else "inline",
            activity_file=activity_file,
        )

        try:
            sub_run = await _start_subagent_run(
                runtime,
                target_agent_id,
                target_project_id,
                session.id,
                content,
                context,
                run_overrides,
                work_id,
            )
        except ActiveRunError:
            if session_id is None:
                return tool_failure(
                    "session_busy",
                    f"session already has an active run: {session.id}",
                )

            _, executor = _make_subagent_executor(
                runtime,
                content,
                context,
                run_overrides,
            )
            target_agent = runtime.agent_resolver.resolve_agent(target_project_id, target_agent_id)
            item = await runtime.chat_run_manager.enqueue(
                SessionAddress(
                    project_id=target_project_id,
                    agent_id=target_agent_id,
                    session_id=session.id,
                ),
                executor,
                display_content=content,
                admission=RunAdmission(
                    working_project_id=resolve_working_project_id(target_project_id, target_agent),
                    run_kind=RunKind.SUBAGENT,
                    work_id=work_id,
                ),
            )
            if activity is not None:
                activity.mark_queued()
            await _emit_subagent_session_started(
                context,
                work_id,
                target_agent_id,
                target_project_id,
                session.id,
                queue_item_id=item.item_id,
                status=SUBAGENT_STATUS_QUEUED,
                delivery="automatic" if background else "inline",
                activity_file=activity_file,
            )
            if background:
                queued_run = _started_run_from_queue_item(item)
                if queued_run is None:
                    batch_tracker.register_queued(
                        parent_key,
                        target_agent_id,
                        session.id,
                        item.item_id,
                        target_project_id,
                        activity_file,
                        work_id=work_id,
                    )
                    slot_registered = True
                    if _should_register_parent_cascade(background=True):
                        _attach_parent_cancellation(
                            runtime,
                            context.run_id,
                            queued_item=item,
                            queued_agent_id=target_agent_id,
                            queued_session_id=session.id,
                            queued_project_id=target_project_id,
                            batch_tracker=batch_tracker,
                            parent_key=parent_key,
                        )
                    _track_queued_subagent_completion(
                        batch_tracker,
                        parent_key,
                        item,
                        activity,
                        activity_file,
                    )
                    activity_handed_off = activity is not None
                    return tool_success(
                        _with_activity_note(
                            _with_target_project(
                                {
                                    "id": work_id,
                                    "agent_id": target_agent_id,
                                    "session_id": session.id,
                                    "status": SUBAGENT_STATUS_QUEUED,
                                    "delivery": "automatic",
                                    "note": TOP_LEVEL_QUEUED_BACKGROUND_NOTE,
                                    "activity_file": activity_file,
                                },
                                target_project_id,
                            ),
                            activity_file,
                        )
                    )
                sub_run = queued_run
            else:
                # Foreground parents always cascade so an awaited queued child
                # honours the parent cancel, even if it has not started yet.
                _attach_parent_cancellation(
                    runtime,
                    context.run_id,
                    queued_item=item,
                    queued_agent_id=target_agent_id,
                    queued_session_id=session.id,
                    queued_project_id=target_project_id,
                )
                try:
                    sub_run = await item.future
                except asyncio.CancelledError:
                    runtime.chat_run_manager.remove_queued(
                        target_agent_id, session.id, item.item_id, project_id=target_project_id
                    )
                    raise

        if activity is not None:
            activity.attach(sub_run)
            activity_handed_off = True
        # Register tracking, parent-cancel cascade, and completion watcher
        # synchronously - before the next await. The child Run is already live,
        # so every await below is an orphan window: a parent cancel landing
        # there must still cascade to and track this child.
        batch_tracker.register_reserved(
            parent_key,
            target_agent_id,
            session.id,
            sub_run.id,
            target_project_id,
            activity_file,
            work_id=work_id,
        )
        slot_registered = True
        if _should_register_parent_cascade(background=background):
            _attach_parent_cancellation(
                runtime,
                context.run_id,
                sub_run=sub_run,
                batch_tracker=batch_tracker,
                parent_key=parent_key,
            )

        _track_subagent_completion(batch_tracker, parent_key, sub_run, activity_file)
        await _emit_subagent_session_started(
            context,
            work_id,
            target_agent_id,
            target_project_id,
            session.id,
            run_id=sub_run.id,
            status=RunStatus.RUNNING.value,
            delivery="automatic" if background else "inline",
            activity_file=activity_file,
        )
        if background:
            return tool_success(
                _with_activity_note(
                    _with_target_project(
                        {
                            "id": work_id,
                            "agent_id": target_agent_id,
                            "session_id": session.id,
                            "status": RunStatus.RUNNING.value,
                            "delivery": "automatic",
                            "note": TOP_LEVEL_BACKGROUND_NOTE,
                            "activity_file": activity_file,
                        },
                        target_project_id,
                    ),
                    activity_file,
                )
            )

        timeout_seconds = settings["subagent_timeout_minutes"] * SECONDS_PER_MINUTE
        try:
            result = await asyncio.wait_for(
                _wait_for_subagent_result(sub_run, activity_file), timeout=timeout_seconds
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
                activity_file=activity_file,
            )
            _register_result_acknowledgement_after_parent_persistence(
                context,
                runtime,
                batch_tracker,
                parent_key,
                target_agent_id,
                session.id,
                sub_run.id,
                target_project_id,
            )
            batch_tracker.on_sub_agent_complete(parent_key, sub_run.id, result)
            return tool_failure(
                "subagent_timeout",
                f"Sub-agent run timed out after {settings['subagent_timeout_minutes']} minutes",
            )

        _register_result_acknowledgement_after_parent_persistence(
            context,
            runtime,
            batch_tracker,
            parent_key,
            target_agent_id,
            session.id,
            sub_run.id,
            target_project_id,
        )
        batch_tracker.on_sub_agent_complete(parent_key, sub_run.id, result)
        public_result = _public_subagent_result(
            work_id,
            target_agent_id,
            target_project_id,
            session.id,
            result,
            delivery="inline",
        )
        return tool_success(_with_activity_note(public_result, activity_file))
    finally:
        if activity is not None and not activity_handed_off:
            activity.finish_unstarted()
        if not slot_registered:
            batch_tracker.release_slot(parent_key)


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


async def _start_subagent_run(
    runtime: RuntimeServices,
    agent_id: str,
    project_id: str | None,
    session_id: str,
    content: str,
    context: ToolContext,
    run_overrides: AgentRunOverrides | None,
    work_id: str,
) -> Run:
    _, executor = _make_subagent_executor(
        runtime,
        content,
        context,
        run_overrides,
    )
    target_agent = runtime.agent_resolver.resolve_agent(project_id, agent_id)
    return await runtime.chat_run_manager.start(
        SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id),
        executor,
        admission=RunAdmission(
            working_project_id=resolve_working_project_id(project_id, target_agent),
            run_kind=RunKind.SUBAGENT,
            work_id=work_id,
        ),
    )


def _make_subagent_executor(
    runtime: RuntimeServices,
    content: str,
    context: ToolContext,
    run_overrides: AgentRunOverrides | None = None,
) -> tuple[ChatLoop, RunExecutor]:
    # Child Runs must match normal live Runs: the parent streaming loop
    # carries its attachment resolver and compaction service into the
    # child; only the nesting depth differs. The target project rides
    # ``run.project_id`` (set when the child run is started/enqueued), so the
    # child executes under its own addressed scope without the executor closure
    # carrying it.
    sub_loop = runtime.streaming_chat_loop.child_loop(
        nesting_depth=context.nesting_depth + 1,
    )
    return sub_loop, sub_loop.run_executor(
        content,
        agent_overrides=run_overrides,
    )


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
) -> None:
    async def complete_when_started_and_terminal() -> None:
        try:
            run = await item.future
        except asyncio.CancelledError:
            if activity is not None:
                activity.finish_unstarted()
            batch_tracker.remove_queued(parent_key, item.item_id)
            return
        except Exception:
            if activity is not None:
                activity.finish_unstarted("failed before start")
            batch_tracker.remove_queued(parent_key, item.item_id)
            raise
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


def _result_from_session(
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
        session = runtime.chat_sessions.get(
            SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)
        )
        messages = session.load()
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

    terminal_result = _terminal_session_result(messages, run_id)
    if terminal_result is None:
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

    assistant, summary = terminal_result
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
    result, terminal = _result_from_session(
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
        result, terminal = _result_from_session(
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
        data["note"] = (
            "The Sub-Agent Run was interrupted before it produced Assistant output. "
            "Continue the same Session by passing both agent_id and session_id from this "
            "result to subagent."
        )
    return data


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
    data["note"] = (
        f"Result is partial: the Sub-Agent Run was interrupted{cause_text}. Continue the "
        "same Session by passing both agent_id and session_id from this result to subagent."
    )


def _queued_result_dict(entry: _SubAgentEntry) -> JsonObject:
    return _with_target_project(
        {
            "agent_id": entry.agent_id,
            "session_id": entry.session_id,
            "run_id": None,
            "queue_item_id": entry.queue_item_id,
            "status": SUBAGENT_STATUS_QUEUED,
            "result": None,
            "usage": None,
            "activity_file": entry.activity_file,
        },
        entry.project_id,
    )


def _queued_manager_result_dict(
    agent_id: str,
    session_id: str,
    item: Any,
    project_id: str | None,
    activity_file: str | None,
) -> JsonObject:
    return _with_target_project(
        {
            "agent_id": agent_id,
            "session_id": session_id,
            "run_id": None,
            "queue_item_id": item.item_id,
            "status": SUBAGENT_STATUS_QUEUED,
            "result": None,
            "usage": None,
            "activity_file": activity_file,
        },
        project_id,
    )


def _should_poll_session_result(result: JsonObject) -> bool:
    return result.get("status") in {
        RunStatus.FAILED.value,
        RunStatus.CANCELLED.value,
        RunStatus.INTERRUPTED.value,
    } or (result.get("status") == RunStatus.COMPLETED.value and not result.get("result"))


def _session_result_has_output(result: JsonObject) -> bool:
    return bool(result.get("result"))


def _terminal_session_result(
    messages: list[ChatMessage],
    run_id: str | None,
) -> tuple[ChatMessage | None, ChatMessage] | None:
    summary_index: int | None = None
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.role != "run_summary":
            continue
        if run_id is None or message.run_id == run_id:
            summary_index = index
            break
    if summary_index is None:
        return None

    summary = messages[summary_index]
    if summary.run_id is None or summary.status not in {
        RunStatus.COMPLETED.value,
        RunStatus.FAILED.value,
        RunStatus.CANCELLED.value,
        RunStatus.INTERRUPTED.value,
    }:
        return None

    if run_id is None and any(
        message.role in {"user", "assistant", "tool", "error"}
        for message in messages[summary_index + 1 :]
    ):
        return None

    segment_start = 0
    for index in range(summary_index - 1, -1, -1):
        if messages[index].role == "run_summary":
            segment_start = index + 1
            break

    assistant = next(
        (
            message
            for message in reversed(messages[segment_start:summary_index])
            if message.role == "assistant" and message.content
        ),
        None,
    )
    return assistant, summary


def _load_subagent_settings(runtime: RuntimeServices) -> dict[str, int]:
    settings = runtime.storage.load_subagent_settings()
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


def _parse_agent_run_overrides(arguments: JsonObject) -> AgentRunOverrides | None:
    """Parse the Run-only override fields.

    An empty ``thinking_effort`` string is the internal ``provider default``
    sentinel used by the Agent configuration chain. As a Tool override it is
    meaningless — omitting the field already inherits the target Agent's value —
    so it is collapsed to ``None`` (no override) rather than treated as an
    explicit request to clear the Agent's configured level.
    """
    model = optional_string(arguments.get("model"), field_name="model")
    thinking_effort: str | None = None
    if "thinking_effort" in arguments:
        raw = arguments["thinking_effort"]
        if isinstance(raw, str) and raw:
            thinking_effort = cast(
                str,
                validate_thinking_effort(
                    raw,
                    label="thinking_effort",
                    allow_none=False,
                ),
            )
    overrides = AgentRunOverrides(
        model=model,
        thinking_effort=thinking_effort,
    )
    return None if overrides.is_empty else overrides


def _validate_target_agent(
    runtime: RuntimeServices,
    target_agent_id: str,
    project_id: str | None,
    *,
    run_overrides: AgentRunOverrides | None = None,
) -> JsonObject | None:
    """Validate the spawn target resolves under its addressed project.

    Routes through the one resolver seam: ``project_id=None`` resolves the store
    identity agent, while a set ``project_id`` requires the target to be on that
    project's Team with a usable model. Any resolver failure (unknown
    agent/project, off-Team target, or a model chain that fell through) becomes
    the validation failure envelope so the tool returns a clean result instead of
    letting the error escape the tool boundary.
    """
    try:
        if run_overrides is None:
            runtime.agent_resolver.resolve_agent(project_id, target_agent_id)
        else:
            runtime.agent_resolver.resolve_agent(
                project_id,
                target_agent_id,
                run_overrides=run_overrides,
            )
    except AgentResolutionError as error:
        return tool_failure("agent_not_found", str(error))
    except ModelConfigurationError as error:
        return tool_failure("invalid_arguments", str(error))
    return None


def _mark_subagent_session(
    runtime: RuntimeServices,
    sub_agent_id: str,
    sub_project_id: str | None,
    sub_session_id: str,
    work_id: str,
    context: ToolContext,
) -> None:
    # The child session's metadata is the durable side of the parent→child link.
    # It is addressed under the target project anchor so a
    # project-scoped child's sidecar lives next to its session, and the link
    # records ``project_id`` so the child session is fully addressable after a
    # restart (its anchor cannot be derived from the parent ids alone).
    session_manager = runtime.chat_sessions
    address = SessionAddress(
        project_id=sub_project_id, agent_id=sub_agent_id, session_id=sub_session_id
    )

    def update(metadata: JsonObject) -> None:
        metadata[SUBAGENT_SESSION_METADATA_FLAG] = True
        metadata[SUBAGENT_PARENT_METADATA_KEY] = {
            "id": work_id,
            "agent_id": context.agent_id,
            "session_id": context.session_id,
            "run_id": context.run_id,
            "tool_call_id": context.tool_call_id,
            "tool_call_index": context.tool_call_index,
            "project_id": context.project_id,
        }

    session_manager.mutate_metadata(address, update)


def _subagent_session_title(description: str | None, content: str) -> str:
    """Build the stable child Session title from the parent-authored task identity."""

    source = description if description else content
    return " ".join(source.split())[:SUBAGENT_SESSION_TITLE_MAX_CHARACTERS]


async def _emit_subagent_session_started(
    context: ToolContext,
    work_id: str,
    sub_agent_id: str,
    sub_project_id: str | None,
    sub_session_id: str,
    *,
    run_id: str | None = None,
    queue_item_id: str | None = None,
    activity_file: str | None = None,
    status: str,
    delivery: str,
) -> None:
    data: JsonObject = {
        "id": work_id,
        "agent_id": sub_agent_id,
        "session_id": sub_session_id,
        "status": status,
        "delivery": delivery,
        "activity_file": activity_file,
    }
    if sub_project_id is not None:
        data["project_id"] = sub_project_id
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


def _parent_key(context: ToolContext) -> ParentKey:
    return (context.agent_id, context.session_id, context.run_id)


def _resolve_target_address(
    address: str,
    caller_project_id: str | None,
) -> tuple[str, str | None]:
    """Resolve the tool's address while preserving bare-target inheritance.

    A qualified address explicitly supplies the target project. A bare address
    keeps the subagent tool's existing behavior by inheriting the caller's
    project scope (or remaining identity-scoped when the caller has none).
    """
    agent_id, addressed_project_id = parse_agent_address(address)
    return agent_id, addressed_project_id or caller_project_id


def _target_is_allowed(
    context: ToolContext,
    target_agent_id: str,
    target_project_id: str | None,
) -> bool:
    """Check the parent snapshot and enforce the Project boundary independently."""
    if context.project_id is not None and target_project_id != context.project_id:
        return False
    if target_agent_id == context.agent_id and target_project_id == context.project_id:
        return True
    allowed = subagent_allowed_agents(context.tool_settings)
    if "*" in allowed:
        return True
    address = (
        target_agent_id
        if context.project_id is not None
        else format_agent_address(target_agent_id, target_project_id)
    )
    return address in allowed


def _with_target_project(data: JsonObject, project_id: str | None) -> JsonObject:
    if project_id is not None:
        data["project_id"] = project_id
    return data


def _activity_file(activity: SubAgentActivity | None) -> str | None:
    if activity is None:
        return None
    return model_path(activity.path.resolve())


def _with_activity_note(data: JsonObject, activity_file: str | None) -> JsonObject:
    if activity_file is not None:
        data["activity_note"] = SUBAGENT_ACTIVITY_NOTE_TEMPLATE.format(path=activity_file)
    return data


def _new_subagent_work_id() -> str:
    return f"{SUBAGENT_WORK_ID_PREFIX}{uuid4().hex}"


def _without_internal_handles(data: JsonObject) -> JsonObject:
    return {key: value for key, value in data.items() if key not in {"run_id", "queue_item_id"}}


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
    data: JsonObject = {
        "id": work_id,
        "agent_id": agent_id,
        "session_id": session_id,
        **_without_internal_handles(result),
    }
    if project_id is not None:
        data["project_id"] = project_id
    if delivery is not None:
        data["delivery"] = delivery
    if note is not None:
        data["note"] = note
    return data


def _run_matches_target(
    run: Run,
    agent_id: str,
    session_id: str,
    project_id: str | None,
) -> bool:
    return (
        run.agent_id == agent_id and run.session_id == session_id and run.project_id == project_id
    )
