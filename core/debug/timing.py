"""Content-free timing of a Model request and the Tool Calls it produces.

The request scope contains measurements only. It is copied into the stream's
reader Tasks; Tool batches deliberately use their own context and correlate by
Run, Iteration and Call instead. No measurement participates in scheduling.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from uuid import uuid4

from core.utils.logging import get_logger
from core.utils.timestamps import utc_now_timestamp

_LOGGER = get_logger("debug.timing")


@dataclass
class _Request:
    fields: dict[str, str | int]
    wire: dict[str, str | int] = field(default_factory=dict)


_REQUEST: ContextVar[_Request | None] = ContextVar("provider_timing", default=None)


def enabled() -> bool:
    return _LOGGER.isEnabledFor(logging.DEBUG)


def framed_event() -> None:
    """Identify the SSE event currently being normalized, without its payload."""
    request = _REQUEST.get()
    if request is not None:
        request.wire["wire_event"] = int(request.wire.get("wire_event", 0)) + 1


def mark(phase: str, **fields: str | int | float | bool | None) -> None:
    """Write identifiers, counts and clocks only; callers must never pass content."""
    if not enabled():
        return
    request = _REQUEST.get()
    values: dict[str, str | int | float | bool | None] = {
        "phase": phase,
        "at": utc_now_timestamp(),
        "mono_ns": time.monotonic_ns(),
        "pid": os.getpid(),
    }
    if request is not None:
        values.update(request.fields)
        values.update(request.wire)
    values.update(fields)
    _LOGGER.debug(
        "Tool timing (%s)",
        " ".join(f"{key}={json.dumps(value, ensure_ascii=True)}" for key, value in values.items()),
        extra={"tool_timing": values},
    )


@contextmanager
def request_scope(*, run: str, iteration: int, model: str) -> Iterator[None]:
    """Give each attempt its own id, including retries in one Iteration."""
    if not enabled():
        yield
        return
    request = _Request({"request": uuid4().hex, "run": run, "iteration": iteration, "model": model})
    token = _REQUEST.set(request)
    outcome = "completed"
    try:
        mark("request_started")
        yield
    except BaseException as exc:
        outcome = type(exc).__name__
        raise
    finally:
        mark("request_finished", outcome=outcome)
        _REQUEST.reset(token)


class WireTiming:
    """Time transport reads before decoding, including consumer backpressure.

    A received chunk is not a network packet: it is what the HTTP transport
    returned to vBot. ``read_started_ns`` distinguishes a pending read from a
    consumer that did not ask for bytes yet. Neither clock proves when a remote
    backend generated the data.
    """

    def __init__(self, *, trace: str | None = None) -> None:
        self._request = _REQUEST.get()
        self._wire = trace or uuid4().hex
        self._sequence = 0
        self._read_started_ns: int | None = None
        self._last_received_ns: int | None = None
        if self._request is not None:
            self._request.wire = {}
        mark("wire_started", wire=self._wire)

    def reading(self) -> None:
        self._read_started_ns = time.monotonic_ns()

    def received(self, size: int) -> None:
        received_ns = time.monotonic_ns()
        self._sequence += 1
        fields: dict[str, str | int] = {
            "wire": self._wire,
            "wire_chunk": self._sequence,
            "wire_received_ns": received_ns,
        }
        if self._request is not None:
            self._request.wire.update(fields)
        mark(
            "wire_received",
            **fields,
            bytes=size,
            read_started_ns=self._read_started_ns,
            consumer_gap_ns=(
                self._read_started_ns - self._last_received_ns
                if self._last_received_ns is not None and self._read_started_ns is not None
                else None
            ),
        )
        self._last_received_ns = received_ns
        self._read_started_ns = None
