"""Sub-Agent admission, target resolution and child Run construction."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

from core.chat import ChatSessionError
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
    RunExecutor,
    RunKind,
    RunStatus,
)
from core.sessions import SessionAddress, TemporarySessionBinding
from core.settings import SettingsValidationError, validate_thinking_effort
from core.subagents._completion import (
    _activity_file,
    _attach_parent_cancellation,
    _public_subagent_result,
    _register_result_acknowledgement_after_parent_persistence,
    _result_dict,
    _started_run_from_queue_item,
    _track_queued_subagent_completion,
    _track_subagent_completion,
    _wait_for_subagent_result,
    _with_activity_note,
    _with_target_project,
)
from core.subagents._constants import (
    CASCADE_BACKGROUND_CHILDREN,
    DEFAULT_MAX_SUBAGENT_DEPTH,
    DEFAULT_MAX_SUBAGENTS_PER_TURN,
    DEFAULT_SUBAGENT_TIMEOUT_MINUTES,
    SECONDS_PER_MINUTE,
    SUBAGENT_PARENT_METADATA_KEY,
    SUBAGENT_SESSION_METADATA_FLAG,
    SUBAGENT_SESSION_STARTED_EVENT,
    SUBAGENT_SESSION_TITLE_MAX_CHARACTERS,
    SUBAGENT_STATUS_QUEUED,
    TOP_LEVEL_BACKGROUND_NOTE,
    TOP_LEVEL_QUEUED_BACKGROUND_NOTE,
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
    required_string,
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

    temporary_parent = None
    if (
        context.execution_owner is not None
        and target_agent_id == context.agent_id
        and target_project_id == context.project_id
    ):
        owner = context.execution_owner
        temporary_parent = await asyncio.to_thread(
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
    validation_error = _validate_target_agent(
        runtime,
        target_agent_id,
        target_project_id,
        run_overrides=run_overrides,
        temporary_parent_binding=temporary_parent,
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
        parent_key,
        settings["max_subagents_per_turn"],
        context.project_id,
        execution_owner=context.execution_owner,
    ):
        return tool_failure(
            "subagent_limit_exceeded",
            f"Sub-agent per-turn limit exceeded: {settings['max_subagents_per_turn']}",
        )

    slot_registered = False
    work_id = batch_tracker.allocate_work_id(parent_key)
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
                runtime.agent_resolver.resolve_temporary_agent(
                    temporary_parent.address,
                    generation_id=temporary_parent.generation_id,
                    run_overrides=run_overrides,
                )
                if temporary_parent is not None
                else runtime.agent_resolver.resolve_agent(target_project_id, target_agent_id)
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
        runtime.agent_resolver.resolve_temporary_agent(
            temporary_parent_binding.address,
            generation_id=temporary_parent_binding.generation_id,
            run_overrides=run_overrides,
        )
        if temporary_parent_binding is not None
        else runtime.agent_resolver.resolve_agent(project_id, agent_id)
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


def _validate_target_agent(
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
    project's Team with a usable model. Any resolver failure (unknown
    agent/project, off-Team target, or a model chain that fell through) becomes
    the validation failure envelope so the tool returns a clean result instead of
    letting the error escape the tool boundary.
    """
    try:
        if temporary_parent_binding is not None:
            runtime.agent_resolver.resolve_temporary_agent(
                temporary_parent_binding.address,
                generation_id=temporary_parent_binding.generation_id,
                run_overrides=run_overrides,
            )
        elif run_overrides is None:
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
