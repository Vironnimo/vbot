"""Sub-Agent admission, target resolution and child Run construction."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, cast

from core.chat import ChatSessionError
from core.projects import (
    AgentResolutionError,
    AgentRunOverrides,
    InvalidAgentAddressError,
    ModelConfigurationError,
    ResolutionAgentNotFoundError,
    ResolutionProjectNotFoundError,
    format_agent_address,
    parse_agent_address,
    resolve_working_project_id,
)
from core.runs import (
    ActiveRunError,
    Run,
    RunAdmission,
    RunExecutor,
    RunKind,
    RunNotFoundError,
    RunStatus,
)
from core.sessions import SessionAddress, TemporarySessionBinding
from core.settings import SettingsValidationError, validate_thinking_effort
from core.subagents._completion import (
    _activity_file,
    _attach_parent_cancellation,
    _cancel_subagent_child,
    _continuation_call,
    _public_subagent_result,
    _register_result_acknowledgement_after_parent_persistence,
    _result_dict,
    _started_run_from_queue_item,
    _track_queued_subagent_completion,
    _track_subagent_completion,
    _wait_for_subagent_result,
    _with_activity_note,
)
from core.subagents._constants import (
    CASCADE_BACKGROUND_CHILDREN,
    DEFAULT_MAX_SUBAGENT_DEPTH,
    DEFAULT_MAX_SUBAGENTS_PER_TURN,
    DEFAULT_SUBAGENT_TIMEOUT_MINUTES,
    SECONDS_PER_MINUTE,
    SUBAGENT_BACKGROUND_UNAVAILABLE_NOTE,
    SUBAGENT_DEPTH_LIMIT_MESSAGE_TEMPLATE,
    SUBAGENT_FOREGROUND_ONLY_NOTE,
    SUBAGENT_MISSING_TASK_MESSAGE,
    SUBAGENT_PARENT_METADATA_KEY,
    SUBAGENT_QUEUED_TIMEOUT_MESSAGE_TEMPLATE,
    SUBAGENT_REMOVED_FROM_QUEUE_MESSAGE,
    SUBAGENT_SESSION_METADATA_FLAG,
    SUBAGENT_SESSION_NOT_FOUND_MESSAGE_TEMPLATE,
    SUBAGENT_SESSION_OWNER_HINT,
    SUBAGENT_SESSION_STARTED_EVENT,
    SUBAGENT_SESSION_TITLE_MAX_CHARACTERS,
    SUBAGENT_STAND_IN_SESSION_NOTE_TEMPLATE,
    SUBAGENT_START_FAILED_MESSAGE_TEMPLATE,
    SUBAGENT_STATUS_QUEUED,
    SUBAGENT_TARGET_NOT_ALLOWED_MESSAGE_TEMPLATE,
    SUBAGENT_TARGET_UNAVAILABLE_MESSAGE_TEMPLATE,
    SUBAGENT_TIMEOUT_MESSAGE_TEMPLATE,
    SUBAGENT_TURN_LIMIT_MESSAGE_TEMPLATE,
    TOP_LEVEL_BACKGROUND_NOTE,
    TOP_LEVEL_QUEUED_BACKGROUND_NOTE,
)
from core.subagents._interpretation import (
    RunTarget,
    has_task,
    implied_action,
    interpret_run,
    reads_as_new_session,
    target_choices,
    tracked_work_text,
)
from core.subagents._status import (
    _handle_subagent_cancel,
    _handle_subagent_status,
    _inspect_subagent_work,
)
from core.subagents.activity import SubAgentActivity
from core.subagents.catalog import SubAgentPromptTarget, build_subagent_prompt_targets
from core.subagents.tracker import (
    ParentKey,
    SubAgentBatchTracker,
)
from core.tools.arguments import (
    ToolArgumentError,
    optional_string,
)
from core.tools.availability import subagent_allowed_agents
from core.tools.tools import (
    JsonObject,
    ToolContext,
    tool_failure,
    tool_success,
)

if TYPE_CHECKING:
    from core.chat import ChatLoop
    from core.runtime.interfaces import RuntimeServices
    from core.sessions.session import ChatSession


def _joined_note(notes: list[str], state_note: str | None) -> str | None:
    """Put the call's interpretation notes before the note about the work's state."""
    parts = [*notes, state_note] if state_note else notes
    return " ".join(parts) or None


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

    def foreground_timeout_minutes(self) -> int:
        """Return the configured bound on a nested caller's foreground work, queue included."""
        return _load_subagent_settings(self._runtime)["subagent_timeout_minutes"]

    async def spawn(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        """Handle a public Sub-Agent lifecycle operation."""
        return await _handle_subagent(
            context,
            arguments,
            runtime=self._runtime,
            batch_tracker=self._batch_tracker,
        )

    async def inspect(
        self,
        agent_id: str,
        session_id: str,
        work_id: str,
        *,
        project_id: str | None = None,
    ) -> JsonObject | None:
        """Return the exact UI projection for one durable Sub-Agent work id."""
        return await _inspect_subagent_work(
            self._runtime,
            agent_id,
            session_id,
            work_id,
            project_id=project_id,
        )


async def _handle_subagent(
    context: ToolContext,
    arguments: JsonObject,
    *,
    runtime: RuntimeServices,
    batch_tracker: SubAgentBatchTracker,
) -> JsonObject:
    action = arguments.get("action")
    if action is None:
        implied = implied_action(arguments)
        if not isinstance(implied, str):
            return implied
        action = implied

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

    if not has_task(arguments):
        return tool_failure("invalid_arguments", SUBAGENT_MISSING_TASK_MESSAGE)
    content = cast(str, arguments["content"])

    try:
        description = optional_string(arguments.get("description"), field_name="description")
        run_target = interpret_run(context, arguments, runtime=runtime, batch_tracker=batch_tracker)
        if not isinstance(run_target, RunTarget):
            return run_target
        session_id = run_target.session_id
        if run_target.tracked_target is not None:
            target_agent_id, target_project_id = run_target.tracked_target
        else:
            target_agent_id, target_project_id = _resolve_target_address(
                run_target.agent_address or context.agent_id, context.project_id
            )
        run_overrides = _parse_agent_run_overrides(arguments)
    except (ToolArgumentError, InvalidAgentAddressError, SettingsValidationError) as error:
        return tool_failure("invalid_arguments", str(error))
    notes = run_target.notes
    target_address = run_target.agent_address or format_agent_address(
        target_agent_id, target_project_id
    )

    background = context.nesting_depth == 0
    requested_background = arguments.get("background")
    if isinstance(requested_background, bool) and requested_background != background:
        notes.append(
            SUBAGENT_BACKGROUND_UNAVAILABLE_NOTE if background else SUBAGENT_FOREGROUND_ONLY_NOTE
        )
    if not _target_is_allowed(context, target_agent_id, target_project_id):
        return tool_failure(
            "agent_not_allowed",
            SUBAGENT_TARGET_NOT_ALLOWED_MESSAGE_TEMPLATE.format(
                target=target_address, choices=target_choices(runtime, context)
            ),
        )

    if (
        session_id is not None
        and target_agent_id == context.agent_id
        and target_project_id == context.project_id
        and session_id == context.session_id
    ):
        return tool_failure(
            "invalid_arguments",
            "session_id is your own active Session; a Sub-Agent needs another Session. "
            'Repeat this call without "session_id" to start a new one.',
        )

    temporary_parent = None
    if (
        context.execution_owner is not None
        and target_agent_id == context.agent_id
        and target_project_id == context.project_id
    ):
        owner = context.execution_owner
        temporary_parent = await runtime.chat_sessions.run_async(
            runtime.chat_sessions.temporary_binding_by_participant,
            owner_name=owner.extension,
            group_id=owner.group_id,
            participant_id=owner.participant_id,
        )
        if temporary_parent is not None and (
            temporary_parent.generation_id != owner.generation_id
            or temporary_parent.address.agent_id != target_agent_id
            or temporary_parent.address.project_id != target_project_id
        ):
            temporary_parent = None
    validation_error = await _validate_target_agent(
        runtime,
        target_agent_id,
        target_project_id,
        run_overrides=run_overrides,
        temporary_parent_binding=temporary_parent,
    )
    if validation_error is not None:
        failure = validation_error["error"]
        if failure["code"] in {"agent_not_found", "project_not_found"}:
            reason = str(failure["message"]).rstrip(".")
            return tool_failure(failure["code"], f"{reason}. {target_choices(runtime, context)}")
        return validation_error

    settings = _load_subagent_settings(runtime)
    parent_key = _parent_key(context)
    if context.nesting_depth >= settings["max_subagent_depth"]:
        return tool_failure(
            "subagent_depth_exceeded",
            SUBAGENT_DEPTH_LIMIT_MESSAGE_TEMPLATE.format(limit=settings["max_subagent_depth"]),
        )
    try:
        parent_run = runtime.chat_run_manager.get(context.run_id)
    except RunNotFoundError:
        parent_run = None
    if not batch_tracker.reserve_slot(
        parent_key,
        settings["max_subagents_per_turn"],
        context.project_id,
        execution_owner=context.execution_owner,
        parent_run=parent_run,
    ):
        return tool_failure(
            "subagent_limit_exceeded",
            SUBAGENT_TURN_LIMIT_MESSAGE_TEMPLATE.format(limit=settings["max_subagents_per_turn"]),
        )

    slot_registered = False
    work_id = batch_tracker.allocate_work_id(parent_key)
    activity: SubAgentActivity | None = None
    activity_handed_off = False
    try:
        if context.is_cancelled():
            return tool_failure("run_cancelled", "Parent run was cancelled before sub-agent spawn")

        title = _subagent_session_title(description, content)
        try:
            session = await runtime.chat_sessions.run_async(
                _open_subagent_session,
                runtime,
                target_agent_id,
                target_project_id,
                session_id,
                title,
                work_id,
                context,
            )
        except ChatSessionError:
            if session_id is None or not reads_as_new_session(session_id):
                owner_unnamed = "agent_id" not in arguments and run_target.tracked_target is None
                return tool_failure(
                    "session_not_found",
                    SUBAGENT_SESSION_NOT_FOUND_MESSAGE_TEMPLATE.format(
                        session_id=session_id,
                        target=target_address,
                        tracked=(SUBAGENT_SESSION_OWNER_HINT if owner_unnamed else "")
                        + tracked_work_text(batch_tracker, context),
                    ),
                )
            notes.append(
                SUBAGENT_STAND_IN_SESSION_NOTE_TEMPLATE.format(
                    session_id=json.dumps(session_id, ensure_ascii=False)
                )
            )
            session = await runtime.chat_sessions.run_async(
                _open_subagent_session,
                runtime,
                target_agent_id,
                target_project_id,
                None,
                title,
                work_id,
                context,
            )

        activity = SubAgentActivity.create(
            runtime.storage.temporary_files,
            agent_id=target_agent_id,
            session_id=session.id,
        )
        activity_file = _activity_file(activity)
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

        # One foreground bound covers both waiting in a busy Session's Queue and
        # the child's execution, so the Parent never blocks longer than the limit.
        loop = asyncio.get_running_loop()
        foreground_deadline = (
            loop.time() + settings["subagent_timeout_minutes"] * SECONDS_PER_MINUTE
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
                temporary_parent_binding=temporary_parent,
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
                temporary_parent_binding=temporary_parent,
            )
            target_agent = (
                await runtime.agent_resolver.resolve_temporary_agent_async(
                    temporary_parent.address,
                    generation_id=temporary_parent.generation_id,
                    run_overrides=run_overrides,
                )
                if temporary_parent is not None
                else await runtime.agent_resolver.resolve_agent_async(
                    target_project_id, target_agent_id
                )
            )
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
                    owner=context.execution_owner,
                    contributes_to_agent_activity=context.execution_owner is None,
                ),
            )
            if activity is not None:
                activity.mark_queued()
            # Admission already accepted this work. Install ownership and its
            # watcher before notification can suspend or fail, even when enqueue
            # started the Run immediately after the previous busy check.
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
            if _should_register_parent_cascade(background=background):
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
                batch_tracker, parent_key, item, activity, activity_file, background=background
            )
            activity_handed_off = activity is not None
            try:
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
                        return tool_success(
                            _with_activity_note(
                                _public_subagent_result(
                                    work_id,
                                    target_agent_id,
                                    target_project_id,
                                    session.id,
                                    {"status": SUBAGENT_STATUS_QUEUED},
                                    delivery="automatic",
                                    note=_joined_note(notes, TOP_LEVEL_QUEUED_BACKGROUND_NOTE),
                                ),
                                activity_file,
                            )
                        )
                    sub_run = queued_run
                else:
                    queued_outcome = await _await_queued_foreground_start(
                        runtime,
                        item,
                        context=context,
                        parent_run=parent_run,
                        batch_tracker=batch_tracker,
                        parent_key=parent_key,
                        agent_id=target_agent_id,
                        project_id=target_project_id,
                        session_id=session.id,
                        timeout_seconds=max(0.0, foreground_deadline - loop.time()),
                        timeout_minutes=settings["subagent_timeout_minutes"],
                    )
                    if not isinstance(queued_outcome, Run):
                        return queued_outcome
                    sub_run = queued_outcome
            except BaseException:
                if not background:
                    _cancel_subagent_child(
                        runtime,
                        sub_run=None,
                        queued_item=item,
                        queued_agent_id=target_agent_id,
                        queued_session_id=session.id,
                        queued_project_id=target_project_id,
                        batch_tracker=batch_tracker,
                        parent_key=parent_key,
                        parent_reason=parent_run.cancel_reason if parent_run is not None else None,
                    )
                raise

        if not slot_registered:
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
                    _public_subagent_result(
                        work_id,
                        target_agent_id,
                        target_project_id,
                        session.id,
                        {"status": RunStatus.RUNNING.value},
                        delivery="automatic",
                        note=_joined_note(notes, TOP_LEVEL_BACKGROUND_NOTE),
                    ),
                    activity_file,
                )
            )

        try:
            result = await asyncio.wait_for(
                _wait_for_subagent_result(sub_run, activity_file),
                timeout=max(0.0, foreground_deadline - loop.time()),
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
                SUBAGENT_TIMEOUT_MESSAGE_TEMPLATE.format(
                    minutes=settings["subagent_timeout_minutes"],
                    continuation=_continuation_call(target_agent_id, target_project_id, session.id),
                ),
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
        if notes:
            public_result["note"] = _joined_note(notes, public_result.get("note"))
        return tool_success(_with_activity_note(public_result, activity_file))
    finally:
        if activity is not None and not activity_handed_off:
            activity.finish_unstarted()
        if not slot_registered:
            batch_tracker.release_slot(parent_key)


async def _await_queued_foreground_start(
    runtime: RuntimeServices,
    item: Any,
    *,
    context: ToolContext,
    parent_run: Run | None,
    batch_tracker: SubAgentBatchTracker,
    parent_key: ParentKey,
    agent_id: str,
    project_id: str | None,
    session_id: str,
    timeout_seconds: float,
    timeout_minutes: int,
) -> Run | JsonObject:
    """Wait inline for a queued foreground child to start, or explain why it never will.

    The shield keeps Parent cancellation from cancelling the Queue item directly
    (the Parent cascade owns that). The item's own future is cancelled only when
    the item is removed from the Queue; that is an ordinary Tool outcome for the
    Parent, while a cancellation of the Parent itself keeps propagating.
    """
    try:
        return cast(
            Run, await asyncio.wait_for(asyncio.shield(item.future), timeout=timeout_seconds)
        )
    except asyncio.CancelledError:
        if not item.future.cancelled() or _parent_cancellation_requested(context, parent_run):
            raise
        batch_tracker.remove_queued(parent_key, item.item_id)
        return tool_failure("subagent_removed", SUBAGENT_REMOVED_FROM_QUEUE_MESSAGE)
    except TimeoutError:
        runtime.chat_run_manager.remove_queued(
            agent_id, session_id, item.item_id, project_id=project_id
        )
        if not item.future.done():
            item.future.cancel()
        if item.future.cancelled():
            batch_tracker.remove_queued(parent_key, item.item_id)
            return tool_failure(
                "subagent_timeout",
                SUBAGENT_QUEUED_TIMEOUT_MESSAGE_TEMPLATE.format(minutes=timeout_minutes),
            )
        if item.future.exception() is None:
            # The child started just as the bound expired; the caller's run wait
            # has no time left and cancels it through the ordinary timeout path.
            return cast(Run, item.future.result())
        error = item.future.exception()
        batch_tracker.remove_queued(parent_key, item.item_id)
        return tool_failure(
            "subagent_start_failed",
            SUBAGENT_START_FAILED_MESSAGE_TEMPLATE.format(error=error),
        )
    except Exception as error:
        batch_tracker.remove_queued(parent_key, item.item_id)
        return tool_failure(
            "subagent_start_failed",
            SUBAGENT_START_FAILED_MESSAGE_TEMPLATE.format(error=error),
        )


def _parent_cancellation_requested(context: ToolContext, parent_run: Run | None) -> bool:
    task = asyncio.current_task()
    return (
        (task is not None and task.cancelling() > 0)
        or context.is_cancelled()
        or (parent_run is not None and parent_run.cancel_requested)
    )


async def _start_subagent_run(
    runtime: RuntimeServices,
    agent_id: str,
    project_id: str | None,
    session_id: str,
    content: str,
    context: ToolContext,
    run_overrides: AgentRunOverrides | None,
    work_id: str,
    *,
    temporary_parent_binding: TemporarySessionBinding | None = None,
) -> Run:
    _, executor = _make_subagent_executor(
        runtime,
        content,
        context,
        run_overrides,
        temporary_parent_binding=temporary_parent_binding,
    )
    target_agent = (
        await runtime.agent_resolver.resolve_temporary_agent_async(
            temporary_parent_binding.address,
            generation_id=temporary_parent_binding.generation_id,
            run_overrides=run_overrides,
        )
        if temporary_parent_binding is not None
        else await runtime.agent_resolver.resolve_agent_async(project_id, agent_id)
    )
    return await runtime.chat_run_manager.start(
        SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id),
        executor,
        admission=RunAdmission(
            working_project_id=resolve_working_project_id(project_id, target_agent),
            run_kind=RunKind.SUBAGENT,
            work_id=work_id,
            owner=context.execution_owner,
            contributes_to_agent_activity=context.execution_owner is None,
        ),
    )


def _make_subagent_executor(
    runtime: RuntimeServices,
    content: str,
    context: ToolContext,
    run_overrides: AgentRunOverrides | None = None,
    *,
    temporary_parent_binding: TemporarySessionBinding | None = None,
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
    if temporary_parent_binding is not None:
        return sub_loop, sub_loop.run_executor(
            content,
            agent_overrides=run_overrides,
            temporary_parent_binding=temporary_parent_binding,
        )
    return sub_loop, sub_loop.run_executor(
        content,
        agent_overrides=run_overrides,
    )


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


async def _validate_target_agent(
    runtime: RuntimeServices,
    target_agent_id: str,
    project_id: str | None,
    *,
    run_overrides: AgentRunOverrides | None = None,
    temporary_parent_binding: TemporarySessionBinding | None = None,
) -> JsonObject | None:
    """Validate the spawn target resolves under its addressed project.

    Routes through the one resolver seam: ``project_id=None`` resolves the store
    identity agent, while a set ``project_id`` requires the target to be on that
    project's Team with a usable model. Every resolver failure becomes a failure
    envelope instead of escaping the tool boundary: only a missing Agent (unknown
    or off-Team) or Project reports ``agent_not_found`` / ``project_not_found``;
    a target that cannot run (for example, a model chain that fell through)
    reports ``agent_unavailable`` with the resolver's reason.
    """
    try:
        if temporary_parent_binding is not None:
            await runtime.agent_resolver.resolve_temporary_agent_async(
                temporary_parent_binding.address,
                generation_id=temporary_parent_binding.generation_id,
                run_overrides=run_overrides,
            )
        else:
            await runtime.agent_resolver.resolve_agent_async(
                project_id, target_agent_id, run_overrides=run_overrides
            )
    except ResolutionProjectNotFoundError as error:
        return tool_failure("project_not_found", str(error))
    except ResolutionAgentNotFoundError as error:
        return tool_failure("agent_not_found", str(error))
    except AgentResolutionError as error:
        return tool_failure(
            "agent_unavailable",
            SUBAGENT_TARGET_UNAVAILABLE_MESSAGE_TEMPLATE.format(
                target=format_agent_address(target_agent_id, project_id), reason=error
            ),
            retryable=False,
        )
    except ModelConfigurationError as error:
        return tool_failure("invalid_arguments", str(error))
    return None


def _open_subagent_session(
    runtime: RuntimeServices,
    agent_id: str,
    project_id: str | None,
    session_id: str | None,
    title: str,
    work_id: str,
    context: ToolContext,
) -> ChatSession:
    """Create or load the child Session and link it to its Parent. Blocking.

    One unit of Session work on the Session database's pool, so a cancelled
    Parent never leaves a created child Session without its Parent link.
    """
    sessions = runtime.chat_sessions
    if session_id is None:
        session = sessions.create(agent_id, project_id=project_id)
        sessions.set_auto_title(
            SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session.id),
            title,
        )
    else:
        # Raises ChatSessionError for an unknown Session; nothing is linked then.
        session = sessions.get(
            SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)
        )
    _mark_subagent_session(runtime, agent_id, project_id, session.id, work_id, context)
    return session


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
