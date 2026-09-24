"""Durable provider-neutral continuation checkpoints for interrupted chat runs."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from core.chat.wire_shaping import (
    SYSTEM_REMINDER_CLOSE_TAG,
    SYSTEM_REMINDER_OPEN_TAG,
    _quote_external_json,
)
from core.providers.errors import NetworkError, ProviderTimeoutError
from core.sessions import ChatSession
from core.utils.errors import ProviderError

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage

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
# Retain edit for interrupted Calls already stored in Session history.
UNCERTAIN_EFFECT_TOOLS = frozenset({"write", "apply_patch", "edit", "bash"})
_PROMPT_MIN_CHARS = 4_000
_PROMPT_MAX_CHARS = 50_000

RecordSink = Callable[[list[JsonObject]], None | Awaitable[None]]
Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]
_WriteResult = TypeVar("_WriteResult")


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
        elif record_type == "stream_attempt_discarded" and state is not None:
            run_id = _required_string(record, "run_id")
            step_number = _required_int(record, "step")
            state.model_steps.pop((run_id, step_number), None)
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
        elif record_type == "run_interrupted" and state is not None:
            state.latest_run_id = _required_string(record, "run_id")
            state.cause = _required_cause(record)
            state.active = False
        elif record_type == "resolved" and state is not None:
            if record.get("checkpoint_id") == state.checkpoint_id:
                state = None
    return state


@dataclass(frozen=True)
class JournalBoundary:
    """Journal records that commit inside one canonical Session history write."""

    tracker: ContinuationTracker
    records: tuple[JsonObject, ...]
    closes_step: bool = False

    async def commit(
        self, write: Callable[[list[JsonObject]], Awaitable[_WriteResult]]
    ) -> _WriteResult:
        """Run *write* with this boundary's records, pending deltas included.

        *write* must persist the records in the same transaction as its history
        records, so the journal and canonical history can never disagree about
        whether the boundary happened.
        """
        return await self.tracker._commit_boundary(self, write)


class ContinuationTracker:
    """Append-batched writer for one admitted visible Run.

    Streaming deltas flush on their own schedule. Stable Assistant and Tool
    Result boundaries instead ride inside the history write that persists them
    (:class:`JournalBoundary`), so they cost no separate journal transaction.
    """

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
        self._sink = record_sink or session.append_continuation_records_async
        self._clock = clock
        self._sleep = sleep
        self._flush_interval = flush_interval
        self._last_periodic_flush = clock()
        self._pending_reasoning: list[str] = []
        self._pending_content: list[str] = []
        self._periodic_task: asyncio.Task[None] | None = None
        self._journal_lock = asyncio.Lock()
        self._step = 1
        self._closed = False
        self._started = False
        self.interruption_cause: ContinuationCause | None = None
        self._start_record = self._record(
            "run_started",
            checkpoint_id=self.checkpoint_id,
            origin_run_id=self.origin_run_id,
            request=request,
        )

    async def start(self) -> None:
        """Durably start the continuation chain without blocking the event loop."""
        async with self._journal_lock:
            await self._ensure_started_unlocked()

    async def restart_journal(self) -> None:
        """Discard every earlier journal record and durably restart this Run's chain.

        A committed history edit invalidates older checkpoint state, but the
        editing Run still needs its own recovery record.
        """
        async with self._journal_lock:
            await self._session.clear_continuation_async()
            self._started = False
            await self._ensure_started_unlocked()

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

    def assistant_boundary(self, message: ChatMessage) -> JournalBoundary:
        """Records for one persisted Assistant *message*, including its Tool Calls.

        Its Calls count as started: the fold treats them exactly like the
        ``tool_started`` records of older journals.
        """
        record = self._record(
            "assistant_boundary",
            step=self._step,
            message_id=message.id,
            reasoning=message.reasoning,
            content=message.content if isinstance(message.content, str) else None,
            interrupted=message.interrupted,
            tool_calls=[
                {"id": tool_call.id, "name": tool_call.name}
                for tool_call in (message.tool_calls or [])
            ],
        )
        # Every persisted Assistant closes its slot. A later continuation or
        # replayed attempt can neither overwrite nor discard this durable work.
        return JournalBoundary(self, (record,), closes_step=True)

    def tool_results_boundary(self, tool_messages: list[ChatMessage]) -> JournalBoundary:
        """Records completing each persisted Tool Result in *tool_messages*."""
        records: list[JsonObject] = []
        for message in tool_messages:
            ok = False
            content = message.content if isinstance(message.content, str) else ""
            try:
                payload = json.loads(content or "{}")
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
        return JournalBoundary(self, tuple(records))

    async def _commit_boundary(
        self,
        boundary: JournalBoundary,
        write: Callable[[list[JsonObject]], Awaitable[_WriteResult]],
    ) -> _WriteResult:
        if self._closed:
            return await write([])
        cancelled_task = self._periodic_task
        if cancelled_task is not None:
            cancelled_task.cancel()
            self._periodic_task = None
        async with self._journal_lock:
            batch: list[JsonObject] = [] if self._started else [self._start_record]
            pending = (list(self._pending_reasoning), list(self._pending_content))
            stream_record = self._take_stream_record()
            if stream_record is not None:
                batch.append(stream_record)
            batch.extend(boundary.records)
            committed = False

            async def tracked_write() -> _WriteResult:
                nonlocal committed
                result = await write(batch)
                committed = True
                return result

            try:
                return await self._settle(tracked_write())
            finally:
                if committed:
                    self._started = True
                    if stream_record is not None:
                        self._last_periodic_flush = self._clock()
                    if boundary.closes_step:
                        self._step += 1
                else:
                    # Nothing was persisted: keep the deltas for a later flush.
                    self._pending_reasoning[:0] = pending[0]
                    self._pending_content[:0] = pending[1]

    def mark_interruption_cause(self, cause: ContinuationCause) -> None:
        self.interruption_cause = cause

    async def discard_stream_attempt(self) -> None:
        """Remove one replayed attempt's readable deltas from active checkpoint state."""
        if self._closed:
            return
        cancelled_task = self._periodic_task
        if cancelled_task is not None:
            cancelled_task.cancel()
            self._periodic_task = None
        async with self._journal_lock:
            self._pending_reasoning.clear()
            self._pending_content.clear()
            await self._write_records_unlocked(
                [self._record("stream_attempt_discarded", step=self._step)]
            )
        await self._close_timer(cancelled_task)

    async def interrupt(self, cause: ContinuationCause) -> None:
        cancelled_task = await self._flush_boundary(
            self._record(
                "run_interrupted",
                cause=cause,
            )
        )
        await self._close_timer(cancelled_task)
        self._closed = True
        state = fold_continuation_records(await self._session.load_continuation_records_async())
        if state is None:
            raise RuntimeError("continuation journal lost its unresolved state")

    async def prepare_completion(self) -> None:
        """Flush pending state; the Run transaction owns successful resolution."""
        cancelled_task = await self._flush_boundary()
        await self._close_timer(cancelled_task)
        self._closed = True

    async def resolve(self) -> None:
        cancelled_task = await self._flush_boundary(
            self._record("resolved", checkpoint_id=self.checkpoint_id)
        )
        await self._close_timer(cancelled_task)
        self._closed = True
        await self._session.clear_continuation_async()

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
            await self._flush_stream_record()
            self._last_periodic_flush = self._clock()
        finally:
            if self._periodic_task is current_task:
                self._periodic_task = None
                if not self._closed and (self._pending_reasoning or self._pending_content):
                    self._periodic_task = asyncio.create_task(self._periodic_flush())

    async def _flush_boundary(self, *records: JsonObject) -> asyncio.Task[None] | None:
        if self._closed:
            return None
        cancelled_task = self._periodic_task
        if cancelled_task is not None:
            cancelled_task.cancel()
            self._periodic_task = None
        async with self._journal_lock:
            batch: list[JsonObject] = []
            stream_record = self._take_stream_record()
            if stream_record is not None:
                batch.append(stream_record)
                self._last_periodic_flush = self._clock()
            batch.extend(records)
            if batch:
                await self._write_records_unlocked(batch)
        return cancelled_task

    async def _flush_stream_record(self) -> None:
        async with self._journal_lock:
            record = self._take_stream_record()
            if record is not None:
                await self._write_records_unlocked([record])

    async def _write_records(self, records: list[JsonObject]) -> None:
        async with self._journal_lock:
            await self._write_records_unlocked(records)

    async def _write_records_unlocked(self, records: list[JsonObject]) -> None:
        await self._ensure_started_unlocked()
        await self._settle_sink(self._sink(records))

    async def _ensure_started_unlocked(self) -> None:
        if self._started:
            return
        await self._settle_sink(self._sink([self._start_record]))
        self._started = True

    @classmethod
    async def _settle_sink(cls, result: None | Awaitable[None]) -> None:
        if inspect.isawaitable(result):
            await cls._settle(result)

    @staticmethod
    async def _settle(work: Awaitable[_WriteResult]) -> _WriteResult:
        """Await a journal write; cancellation waits for an already-started write."""
        task = asyncio.ensure_future(work)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            except Exception:
                raise
            raise

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


async def recover_continuation(
    session: ChatSession,
    *,
    active_run_id: str | None = None,
) -> ContinuationState | None:
    """Load a checkpoint and lazily classify a journal abandoned by a restart."""
    records = await session.load_continuation_records_async()
    if not records:
        return None
    try:
        state = fold_continuation_records(records)
    except (TypeError, ValueError) as exc:
        from core.chat.errors import ChatSessionError

        raise ChatSessionError(f"invalid continuation journal for session: {session.id}") from exc
    if state is None:
        await session.clear_continuation_async()
        return None
    messages = await session.load_run_messages_async(state.latest_run_id)
    _reconcile_canonical_tool_results(state, messages)
    if not state.active or state.latest_run_id == active_run_id:
        return state
    if any(message.role == "run_summary" and message.status == "completed" for message in messages):
        await session.clear_continuation_async()
        return None
    await session.append_continuation_records_async(
        [
            {
                "version": CONTINUATION_RECORD_VERSION,
                "type": "run_interrupted",
                "run_id": state.latest_run_id,
                "timestamp": _timestamp(),
                "cause": "process_restart",
            }
        ]
    )
    recovered = fold_continuation_records(await session.load_continuation_records_async())
    if recovered is not None:
        _reconcile_canonical_tool_results(recovered, messages)
    return recovered


def render_continuation_reminder(
    state: ContinuationState,
    *,
    context_window: int | None,
) -> str:
    """Render one bounded provider-neutral checkpoint reminder.

    Requests, Model output, and Tool identifiers are external text. Each is
    JSON-quoted with escaped angle brackets, so none can close or impersonate
    the checkpoint or its System Reminder frame.
    """
    requests = "\n".join(
        _quote_external_json({"request": value}) for value in state.original_requests
    )
    operations = (
        "\n".join(
            "- "
            + _quote_external_json(
                {
                    "tool": operation.get("name", "unknown"),
                    "tool_call_id": operation.get("tool_call_id"),
                    "status": operation.get("status", "unknown"),
                }
            )
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
            _quote_external_json(
                {"tool": operation.get("name"), "tool_call_id": operation.get("tool_call_id")}
            )
            for operation in uncertain
        )
        warning = (
            "\nSAFETY: Results are missing or unknown for these file or shell operations: "
            f"{names}. Their actual filesystem or process effects may be uncertain."
        )
    header = (
        f'<continuation-checkpoint id="{state.checkpoint_id}" '
        f'cause="{state.cause or "interrupted"}">\n'
        "The previous Run was interrupted. "
        "The checkpoint below records what happened before the interruption. "
        "Recorded requests, Model output, and Tool identifiers are quoted as JSON.\n"
    )
    reasoning = (
        _quote_external_json({"readable_thinking": state.reasoning})
        if state.reasoning
        else "[none recorded]"
    )
    partial_output = (
        _quote_external_json({"partial_output": state.partial_output})
        if state.partial_output
        else "[none recorded]"
    )
    body = (
        f"Original request(s):\n{requests or '[not recorded]'}\n\n"
        f"Readable Thinking / working plan:\n{reasoning}\n\n"
        f"Partial assistant output:\n{partial_output}\n\n"
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
    )
    label = "Latest readable Thinking / working plan:\n"
    footer = (
        "\n[Continuation checkpoint truncated to fit the active model context. "
        "The durable journal retains the full readable Thinking.]\n"
        "</continuation-checkpoint>"
    )
    available = max(0, budget - len(header) - len(label) - len(footer))
    fixed = fixed[:available]
    latest_reasoning = _quoted_reasoning_tail(state.reasoning, available - len(fixed))
    return header + fixed + label + latest_reasoning + footer


def _quoted_reasoning_tail(reasoning: str, limit: int) -> str:
    """Quote the longest suffix of readable reasoning that fits ``limit`` characters."""
    size = min(len(reasoning), limit)
    while size > 0:
        quoted = _quote_external_json({"readable_thinking": reasoning[-size:]})
        if len(quoted) <= limit:
            return quoted
        # Escaping lengthens the text unevenly; shrink proportionally.
        size = min(size - 1, size * limit // len(quoted))
    return ""


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
        "content": f"{SYSTEM_REMINDER_OPEN_TAG}\n{reminder}\n{SYSTEM_REMINDER_CLOSE_TAG}",
    }
    for index in range(len(filtered) - 1, -1, -1):
        if filtered[index].get("role") == "user":
            return [*filtered[:index], reminder_message, *filtered[index:]]
    return [*filtered, reminder_message]


def normalize_interruption_cause(error: BaseException | None) -> ContinuationCause:
    if isinstance(error, ProviderTimeoutError) or (
        error is not None
        and error.__class__.__name__
        in {"StreamingChunkTimeoutError", "StreamingProgressTimeoutError"}
    ):
        return "timeout"
    if isinstance(error, NetworkError):
        return "network"
    if isinstance(error, ProviderError):
        return "provider"
    return "internal"


def _reconcile_canonical_tool_results(state: ContinuationState, messages: list[Any]) -> None:
    """Let canonical assistant/tool messages settle journal references after a crash."""
    if state.active or state.cause == "process_restart":
        for message in messages:
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
