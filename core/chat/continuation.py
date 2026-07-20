"""Durable provider-neutral continuation checkpoints for interrupted chat runs."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, cast

from core.providers.errors import NetworkError, ProviderTimeoutError
from core.sessions import ChatSession
from core.utils.errors import ProviderError

JsonObject = dict[str, Any]
ContinuationCause = Literal[
    "user",
    "provider",
    "network",
    "timeout",
    "process_restart",
    "internal",
]

CONTINUATION_RECORD_VERSION = 1
CONTINUATION_FLUSH_INTERVAL_SECONDS = 2.0
CONTINUATION_REMINDER_MARKER = "<continuation-checkpoint"
UNCERTAIN_EFFECT_TOOLS = frozenset({"write", "edit", "bash"})
_PROMPT_MIN_CHARS = 4_000
_PROMPT_MAX_CHARS = 50_000

RecordSink = Callable[[list[JsonObject]], None]
Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class _ModelStepState:
    reasoning: str = ""
    content: str = ""
    assistant_message_id: str | None = None
    interrupted: bool = False


@dataclass
class ContinuationState:
    """Folded private state of one unresolved continuation chain."""

    checkpoint_id: str
    origin_run_id: str
    latest_run_id: str
    cause: ContinuationCause | None = None
    active: bool = True
    original_requests: list[Any] = field(default_factory=list)
    model_steps: dict[tuple[str, int], _ModelStepState] = field(default_factory=dict)
    operations: dict[str, JsonObject] = field(default_factory=dict)
    compaction_count: int = 0

    @property
    def reasoning(self) -> str:
        return "\n\n".join(step.reasoning for step in self.model_steps.values() if step.reasoning)

    @property
    def partial_output(self) -> str:
        return "\n\n".join(step.content for step in self.model_steps.values() if step.content)

    @property
    def unresolved_operations(self) -> list[JsonObject]:
        return [
            dict(value) for value in self.operations.values() if value.get("status") != "completed"
        ]


def fold_continuation_records(records: list[JsonObject]) -> ContinuationState | None:
    """Fold append-only journal records into the current unresolved state."""
    state: ContinuationState | None = None
    for record in records:
        if record.get("version") != CONTINUATION_RECORD_VERSION:
            raise ValueError("unsupported continuation record version")
        record_type = record.get("type")
        if record_type == "run_started":
            checkpoint_id = _required_string(record, "checkpoint_id")
            run_id = _required_string(record, "run_id")
            origin_run_id = _required_string(record, "origin_run_id")
            if state is None or state.checkpoint_id != checkpoint_id:
                state = ContinuationState(
                    checkpoint_id=checkpoint_id,
                    origin_run_id=origin_run_id,
                    latest_run_id=run_id,
                )
            state.latest_run_id = run_id
            state.active = True
            state.cause = None
            if "request" in record and record["request"] is not None:
                state.original_requests.append(record["request"])
        elif record_type == "stream_delta" and state is not None:
            run_id = _required_string(record, "run_id")
            step_number = _required_int(record, "step")
            step = state.model_steps.setdefault((run_id, step_number), _ModelStepState())
            reasoning = record.get("reasoning_delta")
            content = record.get("content_delta")
            if isinstance(reasoning, str):
                step.reasoning += reasoning
            if isinstance(content, str):
                step.content += content
        elif record_type == "assistant_boundary" and state is not None:
            run_id = _required_string(record, "run_id")
            step_number = _required_int(record, "step")
            step = state.model_steps.setdefault((run_id, step_number), _ModelStepState())
            reasoning = record.get("reasoning")
            content = record.get("content")
            if isinstance(reasoning, str):
                step.reasoning = reasoning
            if isinstance(content, str):
                step.content = content
            message_id = record.get("message_id")
            step.assistant_message_id = message_id if isinstance(message_id, str) else None
            step.interrupted = record.get("interrupted") is True
            tool_calls = record.get("tool_calls")
            if isinstance(tool_calls, list):
                for tool_call in tool_calls:
                    if not isinstance(tool_call, dict):
                        continue
                    tool_call_id = tool_call.get("id")
                    name = tool_call.get("name")
                    if isinstance(tool_call_id, str) and isinstance(name, str):
                        state.operations.setdefault(
                            tool_call_id,
                            {
                                "tool_call_id": tool_call_id,
                                "name": name,
                                "run_id": run_id,
                                "status": "unknown",
                            },
                        )
        elif record_type == "tool_started" and state is not None:
            tool_call_id = _required_string(record, "tool_call_id")
            state.operations[tool_call_id] = {
                "tool_call_id": tool_call_id,
                "name": _required_string(record, "name"),
                "run_id": _required_string(record, "run_id"),
                "status": "unknown",
            }
        elif record_type == "tool_result" and state is not None:
            tool_call_id = _required_string(record, "tool_call_id")
            operation = state.operations.setdefault(
                tool_call_id,
                {
                    "tool_call_id": tool_call_id,
                    "name": _required_string(record, "name"),
                    "run_id": _required_string(record, "run_id"),
                },
            )
            operation["status"] = "completed"
            operation["ok"] = record.get("ok") is True
        elif record_type == "compaction_boundary" and state is not None:
            state.compaction_count += 1
        elif record_type == "run_interrupted" and state is not None:
            state.latest_run_id = _required_string(record, "run_id")
            state.cause = _required_cause(record)
            state.active = False
        elif record_type == "resolved" and state is not None:
            if record.get("checkpoint_id") == state.checkpoint_id:
                state = None
    return state


class ContinuationTracker:
    """Append-batched writer for one admitted visible Run."""

    def __init__(
        self,
        session: ChatSession,
        *,
        run_id: str,
        request: Any,
        prior_state: ContinuationState | None = None,
        record_sink: RecordSink | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleeper = asyncio.sleep,
        flush_interval: float = CONTINUATION_FLUSH_INTERVAL_SECONDS,
    ) -> None:
        self._session = session
        self.run_id = run_id
        self.checkpoint_id = (
            prior_state.checkpoint_id if prior_state is not None else uuid.uuid4().hex
        )
        self.origin_run_id = prior_state.origin_run_id if prior_state is not None else run_id
        self._sink = record_sink or session.append_continuation_records
        self._clock = clock
        self._sleep = sleep
        self._flush_interval = flush_interval
        self._last_periodic_flush = clock()
        self._pending_reasoning: list[str] = []
        self._pending_content: list[str] = []
        self._periodic_task: asyncio.Task[None] | None = None
        self._step = 1
        self._closed = False
        self.interruption_cause: ContinuationCause | None = None
        self._sink(
            [
                self._record(
                    "run_started",
                    checkpoint_id=self.checkpoint_id,
                    origin_run_id=self.origin_run_id,
                    request=request,
                )
            ]
        )

    @property
    def step(self) -> int:
        return self._step

    @property
    def closed(self) -> bool:
        return self._closed

    def record_stream_delta(self, *, reasoning: str = "", content: str = "") -> None:
        if self._closed or (not reasoning and not content):
            return
        if reasoning:
            self._pending_reasoning.append(reasoning)
        if content:
            self._pending_content.append(content)
        if self._periodic_task is None:
            self._periodic_task = asyncio.create_task(self._periodic_flush())

    def record_assistant_boundary(
        self,
        *,
        message_id: str,
        reasoning: str | None,
        content: str | None,
        interrupted: bool,
        tool_calls: list[Any] | None = None,
    ) -> None:
        self._flush_boundary(
            self._record(
                "assistant_boundary",
                step=self._step,
                message_id=message_id,
                reasoning=reasoning,
                content=content,
                interrupted=interrupted,
                tool_calls=[
                    {"id": tool_call.id, "name": tool_call.name} for tool_call in (tool_calls or [])
                ],
            )
        )

    def record_tool_starts(self, tool_calls: list[Any]) -> None:
        self._flush_boundary(
            *[
                self._record(
                    "tool_started",
                    tool_call_id=tool_call.id,
                    name=tool_call.name,
                )
                for tool_call in tool_calls
            ]
        )

    def record_tool_results(self, tool_messages: list[Any]) -> None:
        records: list[JsonObject] = []
        for message in tool_messages:
            ok = False
            try:
                payload = json.loads(message.content or "{}")
                ok = isinstance(payload, dict) and payload.get("ok") is True
            except json.JSONDecodeError:
                pass
            records.append(
                self._record(
                    "tool_result",
                    tool_call_id=message.tool_call_id,
                    name=message.name,
                    ok=ok,
                )
            )
        self._flush_boundary(*records)
        self._step += 1

    def record_compaction_boundary(self) -> None:
        self._flush_boundary(self._record("compaction_boundary"))

    def mark_interruption_cause(self, cause: ContinuationCause) -> None:
        self.interruption_cause = cause

    async def interrupt(self, cause: ContinuationCause) -> None:
        cancelled_task = self._flush_boundary(
            self._record(
                "run_interrupted",
                cause=cause,
            )
        )
        await self._close_timer(cancelled_task)
        self._closed = True
        state = fold_continuation_records(self._session.load_continuation_records())
        if state is None:
            raise RuntimeError("continuation journal lost its unresolved state")

    async def resolve(self) -> None:
        cancelled_task = self._flush_boundary(
            self._record("resolved", checkpoint_id=self.checkpoint_id)
        )
        await self._close_timer(cancelled_task)
        self._closed = True
        self._session.clear_continuation()

    async def _periodic_flush(self) -> None:
        current_task = asyncio.current_task()
        try:
            delay = max(
                0.0,
                self._flush_interval - (self._clock() - self._last_periodic_flush),
            )
            await self._sleep(delay)
            if self._closed:
                return
            self._flush_stream_record()
            self._last_periodic_flush = self._clock()
        finally:
            if self._periodic_task is current_task:
                self._periodic_task = None
                if not self._closed and (self._pending_reasoning or self._pending_content):
                    self._periodic_task = asyncio.create_task(self._periodic_flush())

    def _flush_boundary(self, *records: JsonObject) -> asyncio.Task[None] | None:
        if self._closed:
            return None
        cancelled_task = self._periodic_task
        if cancelled_task is not None:
            cancelled_task.cancel()
            self._periodic_task = None
        batch: list[JsonObject] = []
        stream_record = self._take_stream_record()
        if stream_record is not None:
            batch.append(stream_record)
            self._last_periodic_flush = self._clock()
        batch.extend(records)
        if batch:
            self._sink(batch)
        return cancelled_task

    def _flush_stream_record(self) -> None:
        record = self._take_stream_record()
        if record is not None:
            self._sink([record])

    def _take_stream_record(self) -> JsonObject | None:
        if not self._pending_reasoning and not self._pending_content:
            return None
        record = self._record(
            "stream_delta",
            step=self._step,
            reasoning_delta="".join(self._pending_reasoning),
            content_delta="".join(self._pending_content),
        )
        self._pending_reasoning.clear()
        self._pending_content.clear()
        return record

    def _record(self, record_type: str, **fields: Any) -> JsonObject:
        return {
            "version": CONTINUATION_RECORD_VERSION,
            "type": record_type,
            "run_id": self.run_id,
            "timestamp": _timestamp(),
            **fields,
        }

    async def _close_timer(self, task: asyncio.Task[None] | None = None) -> None:
        if task is None:
            task = self._periodic_task
            self._periodic_task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def recover_continuation(
    session: ChatSession,
    *,
    active_run_id: str | None = None,
) -> ContinuationState | None:
    """Load a checkpoint and lazily classify a journal abandoned by a restart."""
    records = session.load_continuation_records()
    if not records:
        return None
    try:
        state = fold_continuation_records(records)
    except (TypeError, ValueError) as exc:
        from core.chat.errors import ChatSessionError

        raise ChatSessionError(f"invalid continuation journal for session: {session.id}") from exc
    if state is None:
        session.clear_continuation()
        return None
    _reconcile_canonical_tool_results(state, session)
    if not state.active or state.latest_run_id == active_run_id:
        return state
    if _transcript_proves_normal_completion(session, state.latest_run_id):
        session.clear_continuation()
        return None
    session.append_continuation_record(
        {
            "version": CONTINUATION_RECORD_VERSION,
            "type": "run_interrupted",
            "run_id": state.latest_run_id,
            "timestamp": _timestamp(),
            "cause": "process_restart",
        }
    )
    recovered = fold_continuation_records(session.load_continuation_records())
    if recovered is not None:
        _reconcile_canonical_tool_results(recovered, session)
    return recovered


def render_continuation_reminder(
    state: ContinuationState,
    *,
    context_window: int | None,
) -> str:
    """Render one bounded provider-neutral checkpoint reminder."""
    requests = "\n\n".join(_render_request(value) for value in state.original_requests)
    operations = (
        "\n".join(
            f"- {operation.get('name', 'unknown')} ({operation.get('tool_call_id')}): "
            f"{operation.get('status', 'unknown')}"
            for operation in state.operations.values()
        )
        or "- none recorded"
    )
    uncertain = [
        operation
        for operation in state.unresolved_operations
        if operation.get("name") in UNCERTAIN_EFFECT_TOOLS
    ]
    warning = ""
    if uncertain:
        names = ", ".join(
            f"{operation.get('name')} ({operation.get('tool_call_id')})" for operation in uncertain
        )
        warning = (
            "\nSAFETY: Results are missing or unknown for these write/edit/bash operations: "
            f"{names}. Inspect the actual filesystem/process state before repeating any of them."
        )
    header = (
        f'<continuation-checkpoint id="{state.checkpoint_id}" '
        f'cause="{state.cause or "interrupted"}">\n'
        "Resume the interrupted work from this provider-neutral checkpoint. "
        "Treat canonical Tool Calls and Tool Results in the conversation as authoritative.\n"
    )
    body = (
        f"Original request(s):\n{requests or '[not recorded]'}\n\n"
        f"Readable Thinking / working plan:\n{state.reasoning or '[none recorded]'}\n\n"
        f"Partial assistant output:\n{state.partial_output or '[none recorded]'}\n\n"
        f"Operations:\n{operations}{warning}\n"
        "</continuation-checkpoint>"
    )
    budget = continuation_prompt_budget(context_window)
    full = header + body
    if len(full) <= budget:
        return full
    fixed = (
        f"Original request(s):\n{requests or '[not recorded]'}\n\n"
        f"Operations:\n{operations}{warning}\n\n"
        "[Continuation checkpoint truncated to fit the active model context. "
        "The durable journal retains the full readable Thinking.]\n"
    )
    remaining = max(0, budget - len(header) - len(fixed) - len("</continuation-checkpoint>"))
    latest_reasoning = state.reasoning[-remaining:] if remaining else ""
    return (
        header
        + fixed
        + "Latest readable Thinking / working plan:\n"
        + latest_reasoning
        + "\n</continuation-checkpoint>"
    )[:budget]


def continuation_prompt_budget(context_window: int | None) -> int:
    if context_window is None:
        return 16_000
    return max(_PROMPT_MIN_CHARS, min(_PROMPT_MAX_CHARS, context_window))


def inject_continuation_reminder(
    messages: list[JsonObject],
    reminder: str,
) -> list[JsonObject]:
    """Inject exactly one reminder immediately before the new user turn."""
    filtered = [
        message
        for message in messages
        if not (
            message.get("role") == "user"
            and isinstance(message.get("content"), str)
            and CONTINUATION_REMINDER_MARKER in message["content"]
        )
    ]
    reminder_message = {
        "role": "user",
        "content": f"<system-reminder>\n{reminder}\n</system-reminder>",
    }
    for index in range(len(filtered) - 1, -1, -1):
        if filtered[index].get("role") == "user":
            return [*filtered[:index], reminder_message, *filtered[index:]]
    return [*filtered, reminder_message]


def normalize_interruption_cause(error: BaseException | None) -> ContinuationCause:
    if isinstance(error, ProviderTimeoutError) or (
        error is not None and error.__class__.__name__ == "StreamingChunkTimeoutError"
    ):
        return "timeout"
    if isinstance(error, NetworkError):
        return "network"
    if isinstance(error, ProviderError):
        return "provider"
    return "internal"


def _transcript_proves_normal_completion(session: ChatSession, run_id: str) -> bool:
    messages = session.load()
    for index, message in enumerate(messages):
        if message.role != "run_summary" or message.run_id != run_id:
            continue
        if message.status != "completed":
            return False
        for prior in reversed(messages[:index]):
            if prior.role == "run_summary":
                break
            if prior.role == "assistant":
                return not prior.interrupted and not prior.tool_calls
        return False
    return False


def _reconcile_canonical_tool_results(state: ContinuationState, session: ChatSession) -> None:
    """Let canonical assistant/tool messages settle journal references after a crash."""
    messages = session.load()
    if state.active or state.cause == "process_restart":
        tail_start = 0
        for index, message in enumerate(messages):
            if message.role == "run_summary":
                tail_start = index + 1
        for message in messages[tail_start:]:
            if message.role != "assistant":
                continue
            for tool_call in message.tool_calls or []:
                state.operations.setdefault(
                    tool_call.id,
                    {
                        "tool_call_id": tool_call.id,
                        "name": tool_call.name,
                        "run_id": state.latest_run_id,
                        "status": "unknown",
                    },
                )
    for message in messages:
        if message.role == "tool" and message.tool_call_id in state.operations:
            operation = state.operations[message.tool_call_id]
            operation["status"] = "completed"
            try:
                payload = json.loads(message.content if isinstance(message.content, str) else "{}")
            except json.JSONDecodeError:
                payload = {}
            operation["ok"] = isinstance(payload, dict) and payload.get("ok") is True


def _render_request(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _required_string(record: JsonObject, key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"continuation record {key} must be a non-empty string")
    return value


def _required_int(record: JsonObject, key: str) -> int:
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"continuation record {key} must be a positive integer")
    return value


def _required_cause(record: JsonObject) -> ContinuationCause:
    value = record.get("cause")
    if value not in {"user", "provider", "network", "timeout", "process_restart", "internal"}:
        raise ValueError("continuation record cause is invalid")
    return cast(ContinuationCause, value)
