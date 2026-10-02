"""Task-backed Tool calls: one driver over both MCP task protocols.

A server may answer a Tool call with a task, its handle for work it finishes in
the background, instead of the Tool result. "Task" here always means that
server-side handle, never an asyncio Task.

Protocol 2025-11-25 has tasks in its core: the client asks for one with the
call's ``task`` parameter (the runner does so when the Tool requires it), polls
``tasks/get`` and reads the outcome with ``tasks/result``, which on
``input_required`` waits until the task ends while the server sends its own
requests. From 2026-07-28 tasks are the extension ``io.modelcontextprotocol/tasks``
(SEP-2663): a client declares it, the server decides per call, ``tasks/get``
carries the status, the server's input requests and the final result or error,
and ``tasks/update`` returns the client's answers.

``drive`` runs either to its end the same way: it polls at the server's
suggested interval within ``MIN_POLL_SECONDS``..``MAX_POLL_SECONDS``, gives up
once the task outlived its time-to-live, answers input requests through the
connection's own callbacks (elicitation and sampling become the usual pending
inputs), and returns the final ``CallToolResult``. A failed, cancelled or expired
task raises ``TaskEndedError``. A caller that stops waiting (a cancelled Run, a
connection that closes) has ``abandon`` cancel the task at the server. A lost
connection does not end the server's task: the caller marks its ``TaskRun`` as
kept and drives the same task again (``resumed``) on the next connection. To an
Agent a task-backed call is an ordinary Tool call.

The extension wire uses only public SDK interfaces: the ``ResultClaim`` of
``TasksExtension`` and ``ClientSession.send_request``. The 2025-11-25 call that
starts a task needs the runner's raw request seam (``ConnectionRunner._task_send``),
because the SDK validates its answer as a ``CallToolResult``.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, override

import mcp.types as types
from mcp.client.extension import ClaimContext, ClientExtension, ResultClaim
from mcp.client.session import ClientRequestContext, ClientSession
from mcp.shared.exceptions import MCPError
from pydantic import ConfigDict, TypeAdapter

TASKS_EXTENSION = "io.modelcontextprotocol/tasks"
# The poll interval when a task suggests none, and the range a suggested one is held to.
DEFAULT_POLL_SECONDS = 1.0
MIN_POLL_SECONDS = 0.1
MAX_POLL_SECONDS = 60.0
# How long cancelling a task at the server may take.
CANCEL_SECONDS = 5.0
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
# Tests patch these seams instead of the process-wide clock and ``asyncio.sleep``.
_sleep = asyncio.sleep
_clock = time.monotonic
_INPUT_REQUEST: TypeAdapter[types.InputRequest] = TypeAdapter(types.InputRequest)


class TaskEndedError(ValueError):
    """The task behind a call ended without a Tool result: failed, cancelled or expired."""


class TaskUnknownError(ValueError):
    """After a reconnect, the server no longer knows the task a call waited for."""


@dataclass(frozen=True)
class TaskState:
    """A task as either protocol reports it; times in seconds."""

    task_id: str
    status: str
    message: str | None = None
    ttl: float | None = None
    interval: float | None = None
    input_requests: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


@dataclass
class TaskRun:
    """One call's wait for a server task: what a new connection needs to resume it."""

    state: TaskState
    # Whether the task runs under protocol 2025-11-25 (``LegacyWire``), else the Tasks extension.
    legacy: bool
    # When the task was received (``_clock``); its time-to-live counts from here.
    seen: float = field(default_factory=lambda: _clock())
    # The answers given to its input requests, by the server's key.
    answers: dict[str, types.InputResponse] = field(default_factory=dict)
    # Set by the caller when the connection was lost: the call resumes the task on
    # the next connection, so stopping this wait does not cancel it.
    kept: bool = False


class TaskHandle(types.Result):
    """The Tasks extension's answer to a Tool call: a task instead of the result."""

    result_type: Literal["task"]
    task_id: str
    status: str
    status_message: str | None = None
    ttl_ms: int | None = None
    poll_interval_ms: int | None = None

    def state(self) -> TaskState:
        return TaskState(
            self.task_id,
            self.status,
            self.status_message,
            _seconds(self.ttl_ms),
            _seconds(self.poll_interval_ms),
        )


class _DetailedTask(types.Result):
    """A ``tasks/get`` answer of the Tasks extension."""

    task_id: str
    status: str
    status_message: str | None = None
    ttl_ms: int | None = None
    poll_interval_ms: int | None = None
    input_requests: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    def state(self) -> TaskState:
        return TaskState(
            self.task_id,
            self.status,
            self.status_message,
            _seconds(self.ttl_ms),
            _seconds(self.poll_interval_ms),
            self.input_requests,
            self.result,
            self.error,
        )


class _Answer(types.Result):
    """An answer kept with every field the server sent."""

    model_config = ConfigDict(extra="allow")


class _TaskParams(types.RequestParams):
    task_id: str


class _TaskResponses(_TaskParams):
    input_responses: types.InputResponses


# Over Streamable HTTP the extension's requests name their task in the Mcp-Name header.
class _GetTask(types.Request[_TaskParams, Literal["tasks/get"]]):
    method: Literal["tasks/get"] = "tasks/get"
    name_param = "taskId"


class _UpdateTask(types.Request[_TaskResponses, Literal["tasks/update"]]):
    method: Literal["tasks/update"] = "tasks/update"
    name_param = "taskId"


class _CancelTask(types.Request[_TaskParams, Literal["tasks/cancel"]]):
    method: Literal["tasks/cancel"] = "tasks/cancel"
    name_param = "taskId"


class TasksExtension(ClientExtension):
    """Declares the Tasks extension and hands each task a server returns to *resolve*.

    The SDK advertises it only on connections that negotiated 2026-07-28 or
    later, where the claim is active; a legacy connection never sees it.
    """

    identifier = TASKS_EXTENSION

    def __init__(
        self, resolve: Callable[[TaskHandle, ClaimContext], Awaitable[types.CallToolResult]]
    ) -> None:
        self._resolve = resolve

    @override
    def claims(self) -> Sequence[ResultClaim[Any]]:
        return (ResultClaim(result_type="task", model=TaskHandle, resolve=self._resolve),)


class TaskWire(Protocol):
    """How one protocol reads, answers, finishes and cancels a task."""

    session: ClientSession
    # Whether the client answers input requests itself (else ``finish`` serves them).
    answers_inputs: bool

    async def status(self, task_id: str) -> TaskState: ...

    async def answer(self, task_id: str, responses: types.InputResponses) -> None: ...

    async def finish(self, state: TaskState, timeout: float | None) -> types.CallToolResult: ...

    async def cancel(self, task_id: str) -> None: ...


class ExtensionWire:
    """The Tasks extension (2026-07-28): status, input requests and outcome in ``tasks/get``."""

    answers_inputs = True

    def __init__(self, session: ClientSession) -> None:
        self.session = session

    async def status(self, task_id: str) -> TaskState:
        answer = await self.session.send_request(
            _GetTask(params=_TaskParams(task_id=task_id)), _DetailedTask
        )
        return answer.state()

    async def answer(self, task_id: str, responses: types.InputResponses) -> None:
        request = _UpdateTask(params=_TaskResponses(task_id=task_id, input_responses=responses))
        await self.session.send_request(request, types.Result)

    async def finish(self, state: TaskState, timeout: float | None) -> types.CallToolResult:
        if state.status == "failed":
            error = state.error or {}
            message = error.get("message") if isinstance(error.get("message"), str) else None
            raise TaskEndedError(
                "The MCP server reports that this call failed" + (f": {message}" if message else "")
            )
        if state.result is None:
            raise ValueError("The MCP server reported a completed task without its result")
        return types.CallToolResult.model_validate(state.result, by_name=False)

    async def cancel(self, task_id: str) -> None:
        await self.session.send_request(
            _CancelTask(params=_TaskParams(task_id=task_id)), types.Result
        )

    async def request(self, method: str, task_id: str) -> dict[str, Any]:
        """The complete answer to an explicit ``tasks/get`` or ``tasks/cancel``."""
        request = (_GetTask if method == "tasks/get" else _CancelTask)(
            params=_TaskParams(task_id=task_id)
        )
        answer = await self.session.send_request(request, _Answer)
        return dict(answer.model_dump(mode="json", by_alias=True, exclude_none=True))


class LegacyWire:
    """Core tasks of protocol 2025-11-25: ``tasks/result`` serves input requests and the outcome."""

    answers_inputs = False

    def __init__(self, session: ClientSession) -> None:
        self.session = session

    async def status(self, task_id: str) -> TaskState:
        request = types.GetTaskRequest(params=types.GetTaskRequestParams(task_id=task_id))
        return legacy_state(await self.session.send_request(request, types.GetTaskResult))

    async def answer(self, task_id: str, responses: types.InputResponses) -> None:
        raise AssertionError("Protocol 2025-11-25 delivers input requests through tasks/result")

    async def finish(self, state: TaskState, timeout: float | None) -> types.CallToolResult:
        # The underlying Tool result, an ``isError`` one for a failed task, or its JSON-RPC error.
        request = types.GetTaskPayloadRequest(
            params=types.GetTaskPayloadRequestParams(task_id=state.task_id)
        )
        return await self.session.send_request(
            request, types.CallToolResult, request_read_timeout_seconds=timeout
        )

    async def cancel(self, task_id: str) -> None:
        request = types.CancelTaskRequest(params=types.CancelTaskRequestParams(task_id=task_id))
        await self.session.send_request(request, types.CancelTaskResult)


def legacy_state(task: types.Task) -> TaskState:
    """A 2025-11-25 task (from its creation or ``tasks/get``) as a ``TaskState``."""
    return TaskState(
        task.task_id,
        task.status,
        task.status_message,
        _seconds(task.ttl),
        _seconds(task.poll_interval),
    )


async def drive(
    wire: TaskWire,
    run: TaskRun,
    *,
    observe: Callable[[TaskState], None],
    abandon: Callable[[str], None],
    resumed: bool = False,
) -> types.CallToolResult:
    """Run the task of *run* to its end and return its Tool result.

    *observe* sees every status read. A task this call stops waiting for before
    it ended is cancelled at the server, like a request the SDK abandons: on a
    failure here directly, on cancellation through *abandon*, which receives
    its id because a cancelled caller cannot wait for the answer. A kept run is
    not cancelled.

    *resumed*: *run* was kept when its connection was lost, and *wire* is a new
    connection. The status is read again first; an error answer means the server
    no longer knows the task (``TaskUnknownError``). Answers the server still
    lists may not have reached it, so they are sent once more. The task keeps
    its time-to-live from when it was first received.
    """
    resend = resumed
    run.kept = False
    try:
        if resumed:
            run.state = await _status_again(wire, run.state.task_id)
        observe(run.state)
        while True:
            state = run.state
            if state.status == "cancelled":
                raise TaskEndedError(
                    "The MCP server cancelled this call"
                    + (f": {state.message}" if state.message else "")
                )
            deadline = None if state.ttl is None else run.seen + state.ttl
            if state.status in {"completed", "failed"}:
                return await wire.finish(state, None)
            if state.status == "input_required" and not wire.answers_inputs:
                # The wait spans the user's answers and the rest of the work.
                timeout = math.inf if deadline is None else deadline - _clock()
                if timeout > 0:
                    return await wire.finish(state, timeout)
            elif state.status == "input_required":
                await _answer(wire, run, resend=resend)
            elif state.status != "working":
                raise ValueError(f"The MCP server reported an unknown task status: {state.status}")
            if deadline is not None and _clock() >= deadline:
                raise TaskEndedError(
                    f"This call did not finish within the {state.ttl:g} seconds the MCP "
                    "server keeps its task, so vBot cancelled it"
                )
            wait = min(
                max(state.interval or DEFAULT_POLL_SECONDS, MIN_POLL_SECONDS), MAX_POLL_SECONDS
            )
            if deadline is not None:
                wait = max(0.0, min(wait, deadline - _clock()))
            resend = False
            await _sleep(wait)
            run.state = await wire.status(state.task_id)
            observe(run.state)
    except asyncio.CancelledError:
        if run.state.status not in TERMINAL_STATUSES and not run.kept:
            abandon(run.state.task_id)
        raise
    except TaskUnknownError:
        raise
    except Exception:
        if run.state.status not in TERMINAL_STATUSES and not run.kept:
            await cancel_quietly(wire, run.state.task_id)
        raise


async def _status_again(wire: TaskWire, task_id: str) -> TaskState:
    """The status of a kept task, read over a new connection."""
    try:
        return await wire.status(task_id)
    except MCPError as error:
        if error.code in {types.CONNECTION_CLOSED, types.REQUEST_TIMEOUT}:
            # No answer came back: the new connection failed, not the task.
            raise
        raise TaskUnknownError(error.message) from None


async def _answer(wire: TaskWire, run: TaskRun, *, resend: bool) -> None:
    """Answer the input requests of the task's state that were not answered yet.

    A server lists a request until it processed the answer, so each key is
    answered once; with *resend*, the given answers it still lists are sent
    again. The requests run concurrently through the connection's callbacks; a
    refused one (``ErrorData``) fails the call, as in a direct call.
    """
    state = run.state
    listed = state.input_requests or {}
    if resend:
        again = {key: run.answers[key] for key in listed if key in run.answers}
        if again:
            await wire.answer(state.task_id, again)
    pending: dict[str, types.InputRequest] = {}
    for key, request in listed.items():
        if key in run.answers:
            continue
        try:
            pending[key] = _INPUT_REQUEST.validate_python(request, by_name=False)
        except ValueError:
            method = request.get("method") if isinstance(request, dict) else None
            raise ValueError(
                f"The MCP server sent an input request vBot cannot answer: {method}"
            ) from None
    if not pending:
        return
    session = wire.session
    responses: types.InputResponses = {}

    async def respond(key: str, request: types.InputRequest) -> None:
        context = ClientRequestContext(
            session=session, request_id=key, meta=request.params.meta if request.params else None
        )
        response = await session.dispatch_input_request(context, request)
        if isinstance(response, types.ErrorData):
            raise MCPError.from_error_data(response)
        responses[key] = response

    try:
        async with asyncio.TaskGroup() as group:
            for key, request in pending.items():
                group.create_task(respond(key, request))
    except* MCPError as refused:
        raise refused.exceptions[0] from None
    run.answers.update(responses)
    await wire.answer(state.task_id, responses)


async def cancel_quietly(wire: TaskWire, task_id: str) -> None:
    """Ask the server to cancel *task_id*, waiting at most ``CANCEL_SECONDS``.

    Best effort: the call has already failed or stopped, and a server that
    cannot be reached or refuses changes nothing about that outcome.
    """
    with contextlib.suppress(Exception):
        async with asyncio.timeout(CANCEL_SECONDS):
            await wire.cancel(task_id)


def _seconds(milliseconds: float | None) -> float | None:
    return None if milliseconds is None else max(milliseconds, 0) / 1000
