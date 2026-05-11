"""Sub-agent spawning tools and in-memory batch completion tracking."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, cast

from core.chat import (
    ChatMessage,
    ChatRunManager,
    ChatSessionError,
    Run,
    RunCancelledError,
    RunNotFoundError,
)
from core.chat.runs import RunStatus
from core.tools.tools import JsonObject, ToolContext, ToolRegistry, tool_failure, tool_success

SUBAGENT_TOOL_NAME = "subagent"
SUBAGENT_RESULT_TOOL_NAME = "subagent_result"
DEFAULT_MAX_SUBAGENT_DEPTH = 4
DEFAULT_MAX_SUBAGENTS_PER_TURN = 8
DEFAULT_SUBAGENT_TIMEOUT_MINUTES = 60
SECONDS_PER_MINUTE = 60
RESULT_PREVIEW_LIMIT = 300

SUBAGENT_TOOL_DESCRIPTION = (
    "Spawn a sub-agent run in a new persisted session. Use non-blocking mode for "
    "parallel work, or blocking mode when the caller must wait for the result."
)
SUBAGENT_RESULT_TOOL_DESCRIPTION = (
    "Fetch the latest result from a spawned sub-agent session and mark it as retrieved."
)

SUBAGENT_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "content": {
            "type": "string",
            "description": "Message to send to the spawned sub-agent.",
        },
        "agent_id": {
            "type": "string",
            "description": "Target agent id. Defaults to the calling agent.",
        },
        "blocking": {
            "type": "boolean",
            "description": "When true, wait for the sub-agent to finish and return its result.",
        },
    },
    "required": ["content"],
    "additionalProperties": False,
}

SUBAGENT_RESULT_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "session_id": {"type": "string", "description": "Sub-agent session id."},
        "agent_id": {
            "type": "string",
            "description": "Sub-agent id. Defaults to the calling agent.",
        },
        "run_id": {
            "type": "string",
            "description": "Optional in-memory sub-agent run id for live result lookup.",
        },
    },
    "required": ["session_id"],
    "additionalProperties": False,
}

ParentKey = tuple[str, str, str]


@dataclass
class _SubAgentEntry:
    agent_id: str
    session_id: str
    run_id: str
    complete: bool = False
    fetched: bool = False
    result: JsonObject | None = None


@dataclass
class _SubAgentBatch:
    entries: dict[str, _SubAgentEntry]
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
            return

        batch.notification_sent = True
        pending_entries = [
            candidate for candidate in batch.entries.values() if not candidate.fetched
        ]
        if not pending_entries:
            return

        message = _batch_completion_message(pending_entries)
        asyncio.create_task(
            self._trigger_service.trigger_run(parent_key[0], message, session_id=parent_key[1])
        )

    def mark_fetched(self, parent_key: ParentKey, sub_session_id: str) -> None:
        """Mark matching sub-agent session results as already fetched by the parent."""
        batch = self._batches.get(parent_key)
        if batch is None:
            return
        for entry in batch.entries.values():
            if entry.session_id == sub_session_id:
                entry.fetched = True

    def spawn_count(self, parent_key: ParentKey) -> int:
        """Return the number of sub-agents spawned by the parent run."""
        batch = self._batches.get(parent_key)
        if batch is None:
            return 0
        return len(batch.entries)

    @staticmethod
    def _all_complete(batch: _SubAgentBatch) -> bool:
        return bool(batch.entries) and all(entry.complete for entry in batch.entries.values())


def register_subagent_tools(
    registry: ToolRegistry,
    runtime: Any,
    trigger_service: Any,
    batch_tracker: SubAgentBatchTracker,
) -> None:
    """Register the public sub-agent tools."""
    registry.register(
        SUBAGENT_TOOL_NAME,
        SUBAGENT_TOOL_DESCRIPTION,
        SUBAGENT_TOOL_PARAMETERS,
        lambda context, arguments: _handle_subagent(
            context,
            arguments,
            runtime=runtime,
            batch_tracker=batch_tracker,
        ),
    )
    registry.register(
        SUBAGENT_RESULT_TOOL_NAME,
        SUBAGENT_RESULT_TOOL_DESCRIPTION,
        SUBAGENT_RESULT_TOOL_PARAMETERS,
        lambda context, arguments: _handle_subagent_result(
            context,
            arguments,
            runtime=runtime,
            batch_tracker=batch_tracker,
        ),
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

    settings = _load_subagent_settings(runtime)
    parent_key = _parent_key(context)
    if context.nesting_depth >= settings["max_subagent_depth"]:
        return tool_failure(
            "subagent_depth_exceeded",
            f"Sub-agent nesting depth limit exceeded: {settings['max_subagent_depth']}",
        )
    if batch_tracker.spawn_count(parent_key) >= settings["max_subagents_per_turn"]:
        return tool_failure(
            "subagent_limit_exceeded",
            f"Sub-agent per-turn limit exceeded: {settings['max_subagents_per_turn']}",
        )

    if context.is_cancelled():
        return tool_failure("run_cancelled", "Parent run was cancelled before sub-agent spawn")

    session = runtime.chat_sessions.create(target_agent_id)
    sub_run = await _start_subagent_run(runtime, target_agent_id, session.id, content, context)
    batch_tracker.register(parent_key, target_agent_id, session.id, sub_run.id)
    _attach_parent_cancellation(runtime, context.run_id, sub_run)

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

    batch_tracker.mark_fetched(parent_key, session.id)
    timeout_seconds = settings["subagent_timeout_minutes"] * SECONDS_PER_MINUTE
    try:
        result = await asyncio.wait_for(_wait_for_subagent_result(sub_run), timeout=timeout_seconds)
    except TimeoutError:
        sub_run.request_cancel()
        return tool_failure(
            "subagent_timeout",
            f"Sub-agent run timed out after {settings['subagent_timeout_minutes']} minutes",
        )

    batch_tracker.on_sub_agent_complete(parent_key, sub_run.id, result)
    return tool_success(result)


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

    result: JsonObject
    if run_id:
        try:
            run = _chat_run_manager(runtime).get(run_id)
        except RunNotFoundError:
            result = _result_from_session(runtime, agent_id, session_id, run_id=run_id)
        else:
            result = await _wait_for_subagent_result(run)
    else:
        result = _result_from_session(runtime, agent_id, session_id, run_id=None)

    batch_tracker.mark_fetched(_parent_key(context), session_id)
    return tool_success(result)


async def _start_subagent_run(
    runtime: Any,
    agent_id: str,
    session_id: str,
    content: str,
    context: ToolContext,
) -> Run:
    from core.chat import ChatLoop

    sub_loop = ChatLoop(runtime, streaming=False)
    sub_loop._nesting_depth = context.nesting_depth + 1  # noqa: SLF001 - planned depth handoff.
    return await _chat_run_manager(runtime).start(
        agent_id=agent_id,
        session_id=session_id,
        executor=lambda run: sub_loop._execute_run(run, content),  # noqa: SLF001
    )


def _track_subagent_completion(
    batch_tracker: SubAgentBatchTracker,
    parent_key: ParentKey,
    run: Run,
) -> None:
    async def complete_when_terminal() -> None:
        result = await _wait_for_subagent_result(run)
        batch_tracker.on_sub_agent_complete(parent_key, run.id, result)

    asyncio.create_task(complete_when_terminal())


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


def _result_dict(run: Run, *, status: str, message: Any) -> JsonObject:
    content: str | None
    usage: JsonObject | None
    if isinstance(message, ChatMessage):
        content = message.content
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


def _last_assistant_with_content(messages: list[ChatMessage]) -> ChatMessage | None:
    for message in reversed(messages):
        if message.role == "assistant" and message.content:
            return message
    return None


def _batch_completion_message(entries: list[_SubAgentEntry]) -> str:
    lines = ["Sub-agent batch completed.", "", "Results:"]
    for entry in entries:
        result_text = "(no output)"
        if entry.result is not None:
            result = entry.result.get("result")
            if isinstance(result, str) and result:
                result_text = result[:RESULT_PREVIEW_LIMIT]
        lines.append(f"- {entry.agent_id}/{entry.session_id}: {result_text}")
    return "\n".join(lines)


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


def _attach_parent_cancellation(runtime: Any, parent_run_id: str, sub_run: Run) -> None:
    try:
        parent_run = _chat_run_manager(runtime).get(parent_run_id)
    except RunNotFoundError:
        return
    parent_run.add_cancel_callback(lambda: sub_run.request_cancel())


def _chat_run_manager(runtime: Any) -> ChatRunManager:
    manager = getattr(runtime, "chat_run_manager", None)
    if manager is not None:
        return cast(ChatRunManager, manager)
    return cast(ChatRunManager, runtime.chat_runs)


def _parent_key(context: ToolContext) -> ParentKey:
    return (context.agent_id, context.session_id, context.run_id)
