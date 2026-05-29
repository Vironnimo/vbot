"""Sub-agent orchestration and in-memory batch completion tracking."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
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
from core.tools.tools import (
    JsonObject,
    ToolContext,
    tool_failure,
    tool_success,
)
from core.utils.logging import get_logger

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

_LOGGER = get_logger("subagents")

ParentKey = tuple[str, str, str]


@dataclass
class _SubAgentEntry:
    agent_id: str
    session_id: str
    run_id: str | None
    queue_item_id: str | None = None
    complete: bool = False
    fetched: bool = False
    result: JsonObject | None = None


@dataclass
class _SubAgentBatch:
    entries: dict[str, _SubAgentEntry]
    reserved_count: int = 0
    notification_sent: bool = False


class SubAgentBatchTracker:
    """Track spawned sub-agent batches for one parent run in memory."""

    def __init__(self, trigger_service: Any) -> None:
        self._trigger_service = trigger_service
        self._batches: dict[ParentKey, _SubAgentBatch] = {}

    def register(
        self,
        parent_key: ParentKey,
        sub_agent_id: str,
        sub_session_id: str,
        sub_run_id: str,
    ) -> None:
        """Register one spawned sub-agent run under a parent run batch."""
        batch = self._batches.setdefault(parent_key, _SubAgentBatch(entries={}))
        batch.entries[sub_run_id] = _SubAgentEntry(
            agent_id=sub_agent_id,
            session_id=sub_session_id,
            run_id=sub_run_id,
        )

    def reserve_slot(self, parent_key: ParentKey, max_count: int) -> bool:
        """Reserve one sub-agent slot before async session/run work begins."""
        batch = self._batches.setdefault(parent_key, _SubAgentBatch(entries={}))
        if self._spawn_count(batch) >= max_count:
            self._prune_if_empty(parent_key, batch)
            return False
        batch.reserved_count += 1
        return True

    def release_slot(self, parent_key: ParentKey) -> None:
        """Release one previously reserved sub-agent slot."""
        batch = self._batches.get(parent_key)
        if batch is None:
            return
        if batch.reserved_count > 0:
            batch.reserved_count -= 1
        self._prune_if_empty(parent_key, batch)

    def register_reserved(
        self,
        parent_key: ParentKey,
        sub_agent_id: str,
        sub_session_id: str,
        sub_run_id: str,
    ) -> None:
        """Convert one reserved slot into a live sub-agent run entry."""
        batch = self._batches.setdefault(parent_key, _SubAgentBatch(entries={}))
        if batch.reserved_count > 0:
            batch.reserved_count -= 1
        batch.entries[sub_run_id] = _SubAgentEntry(
            agent_id=sub_agent_id,
            session_id=sub_session_id,
            run_id=sub_run_id,
        )

    def register_queued(
        self,
        parent_key: ParentKey,
        sub_agent_id: str,
        sub_session_id: str,
        queue_item_id: str,
    ) -> None:
        """Convert one reserved slot into a queued sub-agent run entry."""
        batch = self._batches.setdefault(parent_key, _SubAgentBatch(entries={}))
        if batch.reserved_count > 0:
            batch.reserved_count -= 1
        batch.entries[_queue_entry_key(queue_item_id)] = _SubAgentEntry(
            agent_id=sub_agent_id,
            session_id=sub_session_id,
            run_id=None,
            queue_item_id=queue_item_id,
        )

    def mark_started(
        self,
        parent_key: ParentKey,
        queue_item_id: str,
        sub_run_id: str,
    ) -> bool:
        """Move a queued sub-agent entry to its live run id."""
        batch = self._batches.get(parent_key)
        if batch is None:
            return False
        queued_key = _queue_entry_key(queue_item_id)
        entry = batch.entries.pop(queued_key, None)
        if entry is None:
            return False
        entry.run_id = sub_run_id
        batch.entries[sub_run_id] = entry
        return True

    def remove_queued(self, parent_key: ParentKey, queue_item_id: str) -> None:
        """Remove a queued sub-agent entry that will never start."""
        batch = self._batches.get(parent_key)
        if batch is None:
            return
        batch.entries.pop(_queue_entry_key(queue_item_id), None)
        self._prune_if_empty(parent_key, batch)

    def discard_parent(self, parent_key: ParentKey) -> None:
        """Discard all in-memory tracking for a parent run."""
        self._batches.pop(parent_key, None)

    def queued_entry_for_session(
        self,
        parent_key: ParentKey,
        sub_session_id: str,
    ) -> _SubAgentEntry | None:
        """Return the latest queued entry for a sub-agent session, if any."""
        batch = self._batches.get(parent_key)
        if batch is None:
            return None
        for entry in reversed(list(batch.entries.values())):
            if entry.session_id == sub_session_id and entry.run_id is None:
                return entry
        return None

    def on_sub_agent_complete(
        self,
        parent_key: ParentKey,
        sub_run_id: str,
        result_dict: JsonObject,
    ) -> None:
        """Mark one sub-agent complete and notify the parent when the batch is done."""
        batch = self._batches.get(parent_key)
        if batch is None:
            return
        entry = batch.entries.get(sub_run_id)
        if entry is None:
            return

        entry.complete = True
        entry.result = dict(result_dict)
        if batch.notification_sent or not self._all_complete(batch):
            self._prune_if_finished(parent_key, batch)
            return

        batch.notification_sent = True
        pending_entries = [
            candidate for candidate in batch.entries.values() if not candidate.fetched
        ]
        if not pending_entries:
            self._prune_if_finished(parent_key, batch)
            return

        message = _batch_completion_message(pending_entries)
        task = asyncio.create_task(
            self._trigger_service.trigger_run(
                parent_key[0],
                message,
                session_id=parent_key[1],
                internal=True,
            )
        )
        task.add_done_callback(
            lambda completed: _log_background_task_result(
                completed,
                "Sub-agent batch completion trigger failed for "
                f"agent={parent_key[0]} session={parent_key[1]} run={parent_key[2]}",
            )
        )

    def mark_fetched(
        self,
        parent_key: ParentKey,
        sub_session_id: str,
        sub_run_id: str | None = None,
    ) -> None:
        """Mark one sub-agent result as fetched by run id within a session."""
        batch = self._batches.get(parent_key)
        if batch is None:
            return

        target_run_id = sub_run_id or self.run_id_for_session(parent_key, sub_session_id)
        if target_run_id is None:
            return
        entry = batch.entries.get(target_run_id)
        if entry is None or entry.session_id != sub_session_id:
            return
        entry.fetched = True
        self._prune_if_finished(parent_key, batch)

    def run_id_for_session(self, parent_key: ParentKey, sub_session_id: str) -> str | None:
        """Return the registered run id for a sub-agent session in a parent batch."""
        batch = self._batches.get(parent_key)
        if batch is None:
            return None
        for entry in reversed(list(batch.entries.values())):
            if entry.session_id == sub_session_id and entry.run_id is not None:
                return entry.run_id
        return None

    def spawn_count(self, parent_key: ParentKey) -> int:
        """Return the number of sub-agents spawned by the parent run."""
        batch = self._batches.get(parent_key)
        if batch is None:
            return 0
        return self._spawn_count(batch)

    @staticmethod
    def _all_complete(batch: _SubAgentBatch) -> bool:
        return (
            batch.reserved_count == 0
            and bool(batch.entries)
            and all(entry.complete for entry in batch.entries.values())
        )

    @staticmethod
    def _spawn_count(batch: _SubAgentBatch) -> int:
        return len(batch.entries) + batch.reserved_count

    def _prune_if_empty(self, parent_key: ParentKey, batch: _SubAgentBatch) -> None:
        if batch.reserved_count == 0 and not batch.entries:
            self._batches.pop(parent_key, None)

    def _prune_if_finished(self, parent_key: ParentKey, batch: _SubAgentBatch) -> None:
        if (
            batch.reserved_count == 0
            and bool(batch.entries)
            and all(entry.complete and entry.fetched for entry in batch.entries.values())
        ):
            self._batches.pop(parent_key, None)


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

    sub_loop = ChatLoop(runtime, streaming=False)
    sub_loop._nesting_depth = context.nesting_depth + 1  # noqa: SLF001 - planned depth handoff.
    return sub_loop, lambda run: sub_loop._execute_run(run, content)  # noqa: SLF001


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


def _log_background_task_result(task: asyncio.Task[Any], message: str) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is None:
        return
    _LOGGER.error(
        "%s: %s",
        message,
        error,
        exc_info=(type(error), error, error.__traceback__),
    )


async def _wait_for_subagent_result(run: Run) -> JsonObject:
    try:
        result = await run.wait()
    except RunCancelledError:
        return _result_dict(run, status=RunStatus.CANCELLED.value, message=None)
    except Exception as error:
        return _result_dict(run, status=RunStatus.FAILED.value, message=str(error))

    return _result_dict(run, status=run.status.value, message=result)


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


def _result_dict(run: Run, *, status: str, message: Any) -> JsonObject:
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


def _batch_completion_message(entries: list[_SubAgentEntry]) -> str:
    lines = [
        "Sub-agent batch completed. The complete final output of each sub-agent is "
        "included below. Do not call subagent_result to fetch these again.",
        "",
        "Results:",
    ]
    for entry in entries:
        lines.append("")
        lines.append(f"### {entry.agent_id} (session {entry.session_id}) — {_entry_status(entry)}")
        lines.append(_entry_result_text(entry))
    return "\n".join(lines)


def _entry_status(entry: _SubAgentEntry) -> str:
    if entry.result is not None:
        status = entry.result.get("status")
        if isinstance(status, str) and status:
            return status
    return RunStatus.COMPLETED.value


def _entry_result_text(entry: _SubAgentEntry) -> str:
    if entry.result is None:
        return "(no output)"
    result = entry.result.get("result")
    if isinstance(result, str) and result:
        return result
    note = entry.result.get("note")
    if isinstance(note, str) and note:
        return f"(no output) {note}"
    return "(no output)"


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
) -> None:
    if sub_run is not None:
        if batch_tracker is not None and parent_key is not None:
            batch_tracker.discard_parent(parent_key)
        sub_run.request_cancel()
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
    started_run.request_cancel()


def _chat_run_manager(runtime: Any) -> ChatRunManager:
    manager = getattr(runtime, "chat_run_manager", None)
    if manager is not None:
        return cast(ChatRunManager, manager)
    return cast(ChatRunManager, runtime.chat_runs)


def _parent_key(context: ToolContext) -> ParentKey:
    return (context.agent_id, context.session_id, context.run_id)


def _queue_entry_key(queue_item_id: str) -> str:
    return f"queued:{queue_item_id}"
