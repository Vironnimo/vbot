"""Admitted MCP invocation records and outcome distinctions."""

from __future__ import annotations

import asyncio
import contextvars
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.tools.tools import ToolContext

from ._tasks import TaskRun

if TYPE_CHECKING:
    from .client import ConnectionRunner


class InvocationNotSentError(ValueError):
    """An invocation that never reached the server, so it changed nothing there.

    *denied*: vBot refused it (Tool access). *refused*: the HTTP status with which
    the server refused it unprocessed (3xx or 4xx); *message* then describes that
    answer.
    """

    def __init__(self, message: str, *, denied: bool = False, refused: int | None = None) -> None:
        super().__init__(message)
        self.denied = denied
        self.refused = refused


class UnsupportedOperationError(InvocationNotSentError):
    """An operation the server does not offer under the negotiated protocol; never sent."""


class SubscriptionEndedError(ValueError):
    """An unsubscribe ended a subscription before its caller received the outcome."""


class InvalidToolResultError(ValueError):
    """A Tool ran, but its result does not satisfy the output schema the Tool declares.

    *problem* is redacted and bounded; *payload* is the result as received.
    """

    def __init__(self, problem: str, payload: dict[str, Any]) -> None:
        super().__init__(problem)
        self.problem = problem
        self.payload = payload


@dataclass(eq=False)
class _Call:
    """One admitted invocation, from its queue entry until its result is settled."""

    runner: ConnectionRunner
    operation: str
    arguments: dict[str, Any]
    context: ToolContext | None
    result: asyncio.Future[dict[str, Any]]
    # Whether a request of this call may have reached the server.
    delivered: bool
    # The connection task that admitted the call.
    owner: asyncio.Task[None] | None
    task: asyncio.Task[None] | None = None
    # A read that may run again: it failed transiently or lost its connection.
    retry: bool = False
    # The HTTP status of the last answer of 300 or more to a request of this call.
    http_status: int | None = None
    # The server task a task-backed Tool call waits for, kept across a lost connection.
    server_task: TaskRun | None = None
    # Whether this call resumes *server_task* of an earlier call whose connection was lost.
    resuming: bool = False


_CURRENT_CALL: contextvars.ContextVar[_Call | None] = contextvars.ContextVar(
    "mcp_current_call", default=None
)
