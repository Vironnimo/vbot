"""Scripted OpenAI-compatible fake Provider for the load harness.

Runs as its own process (``python -m scripts.perf_load_suite.fake_provider``)
so streaming work never competes with vBot's Event Loop. Every chat request is
answered from the ``[[perf ...]]`` directive in its latest real User message
(see :mod:`scripts.perf_load_suite.directive`):

- Tool-call rounds stream OpenAI ``tool_calls`` deltas whose names and
  arguments are checked against the Tool definitions in the request.
- The final round streams filler text at the scripted rate. Every tenth content
  chunk carries a wall-clock marker ``⟦t=<epoch seconds>⟧`` so the client can
  measure end-to-end delta latency on the same machine.
- Requests without Tools or without a directive (Session titles, background
  reflection, other utility calls) get a short plain answer and are recorded as
  ``aux``.

Each request is recorded with arrival, first-byte and completion times at
``GET /_perf/stats``; ``POST /_perf/reset`` clears the records.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import re
import time
import zlib
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from core.tools.model_names import model_tool_name
from scripts.perf_load_suite.directive import DirectiveError, PerfDirective, find_directive
from scripts.perf_load_suite.fixture import (
    BASH_COMMAND,
    SEARCH_NEEDLE,
    SOURCE_DIRECTORY,
    source_file_paths,
)

SERVICE_NAME = "vbot-perf-fake-provider"
MODEL_ID = "perf-model"
MARKER_EVERY_CHUNKS = 10
UNPACED_CHUNK_TOKENS = 64
AUX_RESPONSE_TEXT = "[title=Perf load session] Acknowledged."
MARKER_PATTERN = re.compile(r"⟦t=(\d+(?:\.\d+)?)⟧")
_SYSTEM_REMINDER_PREFIX = "<system-reminder>"
_FILLER_WORDS = (
    "load",
    "stream",
    "token",
    "vector",
    "kernel",
    "session",
    "delta",
    "queue",
    "buffer",
    "signal",
    "record",
    "index",
)

ResponseKind = Literal["tool_calls", "text", "warmup", "aux"]


class PlanError(ValueError):
    """The request cannot be answered as scripted; reported as HTTP 400."""


def format_marker(timestamp: float) -> str:
    """Return the in-text timing marker for one wall-clock timestamp."""
    return f"⟦t={timestamp:.6f}⟧"


@dataclass(frozen=True)
class ScriptedToolCall:
    """One Tool call the fake Provider streams in a Tool round."""

    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class PlannedResponse:
    """What the fake Provider answers to one chat request."""

    kind: ResponseKind
    tag: str | None = None
    round_index: int = 0
    directive: PerfDirective | None = None
    tool_calls: tuple[ScriptedToolCall, ...] = ()


@dataclass
class RequestRecord:
    """Timing and shape of one chat request, as seen by the fake Provider."""

    index: int
    arrival: float
    kind: str = "aux"
    tag: str | None = None
    round: int = 0
    stream: bool = False
    body_read: float | None = None
    first_byte: float | None = None
    completed: float | None = None
    prompt_bytes: int = 0
    message_count: int = 0
    tool_names: list[str] = field(default_factory=list)
    tool_call_ids: list[str] = field(default_factory=list)
    tokens: int = 0
    max_emit_lag_ms: float = 0.0
    disconnected: bool = False
    error: str | None = None


class RequestLog:
    """In-memory request records served at ``/_perf/stats``."""

    def __init__(self) -> None:
        self._records: list[RequestRecord] = []
        self._counter = itertools.count()
        self._call_counter = itertools.count(1)

    def begin(self, arrival: float) -> RequestRecord:
        record = RequestRecord(index=next(self._counter), arrival=arrival)
        self._records.append(record)
        return record

    def next_call_id(self) -> str:
        return f"call_perf_{next(self._call_counter)}"

    def reset(self) -> None:
        self._records = []

    def snapshot(self) -> list[dict[str, Any]]:
        return [asdict(record) for record in self._records]


def message_text(message: dict[str, Any]) -> str:
    """Return the plain text of an OpenAI chat message's ``content``."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


def offered_tool_schemas(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map offered Tool names to their JSON Schema parameters."""
    schemas: dict[str, dict[str, Any]] = {}
    for tool in body.get("tools") or []:
        function = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            parameters = function.get("parameters")
            schemas[function["name"]] = parameters if isinstance(parameters, dict) else {}
    return schemas


def plan_response(
    body: dict[str, Any],
    *,
    next_call_id: Callable[[], str],
) -> PlannedResponse:
    """Decide the scripted answer to one ``/v1/chat/completions`` request."""
    messages = body.get("messages")
    if not isinstance(messages, list):
        raise PlanError("request has no messages array")
    schemas = offered_tool_schemas(body)
    if not schemas:
        return PlannedResponse(kind="aux")

    located = _locate_directive(messages)
    if located is None:
        return PlannedResponse(kind="aux")
    directive, directive_index = located
    completed_rounds = sum(
        1
        for message in messages[directive_index + 1 :]
        if isinstance(message, dict)
        and message.get("role") == "assistant"
        and message.get("tool_calls")
    )
    if directive.is_warmup:
        return PlannedResponse(kind="warmup", tag=directive.tag, directive=directive)
    if completed_rounds >= directive.tool_rounds:
        return PlannedResponse(
            kind="text",
            tag=directive.tag,
            round_index=completed_rounds,
            directive=directive,
        )
    calls = tuple(
        _scripted_call(directive, completed_rounds, call_index, schemas, next_call_id())
        for call_index in range(directive.calls)
    )
    return PlannedResponse(
        kind="tool_calls",
        tag=directive.tag,
        round_index=completed_rounds,
        directive=directive,
        tool_calls=calls,
    )


def _locate_directive(messages: list[Any]) -> tuple[PerfDirective, int] | None:
    """Find the directive in the latest real User message.

    System Reminders arrive as synthetic User messages and are skipped; any
    other User message without a directive (for example a reflection brief)
    makes the request auxiliary.
    """
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = message_text(message)
        try:
            directive = find_directive(text)
        except DirectiveError as exc:
            raise PlanError(f"malformed perf directive: {exc}") from exc
        if directive is not None:
            return directive, index
        if text.lstrip().startswith(_SYSTEM_REMINDER_PREFIX):
            continue
        return None
    return None


def _scripted_call(
    directive: PerfDirective,
    round_index: int,
    call_index: int,
    schemas: dict[str, dict[str, Any]],
    call_id: str,
) -> ScriptedToolCall:
    slot = round_index * directive.calls + call_index
    registry_name = directive.tools[slot % len(directive.tools)]
    name = model_tool_name(registry_name)
    if name not in schemas:
        offered = ", ".join(sorted(schemas))
        raise PlanError(f"Tool {name!r} is not offered in this request (offered: {offered})")
    arguments = scripted_arguments(registry_name, tag=directive.tag, slot=slot)
    check_arguments(name, arguments, schemas[name])
    return ScriptedToolCall(call_id=call_id, name=name, arguments=arguments)


def scripted_arguments(name: str, *, tag: str, slot: int) -> dict[str, Any]:
    """Return valid arguments for one supported Tool against the fixture Project."""
    if name == "read":
        paths = source_file_paths()
        return {"path": paths[(zlib.crc32(tag.encode("utf-8")) + slot) % len(paths)]}
    if name == "search_files":
        return {"args": ["-F", SEARCH_NEEDLE, SOURCE_DIRECTORY]}
    if name == "bash":
        return {"command": BASH_COMMAND}
    raise PlanError(f"the fake Provider has no scripted arguments for Tool {name!r}")


def check_arguments(name: str, arguments: dict[str, Any], schema: dict[str, Any]) -> None:
    """Reject scripted arguments the offered Tool schema would not accept."""
    properties = schema.get("properties")
    if isinstance(properties, dict):
        unknown = sorted(set(arguments) - set(properties))
        if unknown:
            raise PlanError(f"Tool {name!r} schema has no properties {unknown}")
        for key, value in arguments.items():
            expected = properties[key].get("type") if isinstance(properties[key], dict) else None
            if expected == "string" and not isinstance(value, str):
                raise PlanError(f"Tool {name!r} property {key!r} expects a string")
            if expected == "array" and not isinstance(value, list):
                raise PlanError(f"Tool {name!r} property {key!r} expects an array")
    required = schema.get("required")
    if isinstance(required, list):
        missing = sorted(key for key in required if key not in arguments)
        if missing:
            raise PlanError(f"Tool {name!r} requires {missing}")


# -- OpenAI stream encoding -------------------------------------------------


def encode_frame(payload: dict[str, Any]) -> bytes:
    """Encode one SSE ``data:`` frame."""
    return f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n".encode()


DONE_FRAME = b"data: [DONE]\n\n"


def chunk_payload(
    model: str,
    delta: dict[str, Any],
    *,
    finish_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "id": "chatcmpl-perf",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def usage_payload(model: str, *, prompt_tokens: int, completion_tokens: int) -> dict[str, Any]:
    """The final usage-only chunk OpenAI sends with ``include_usage``."""
    return {
        "id": "chatcmpl-perf",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def tool_call_frames(
    model: str,
    calls: tuple[ScriptedToolCall, ...],
    *,
    prompt_tokens: int,
) -> list[bytes]:
    """Stream Tool calls as OpenAI deltas: header, split arguments, finish, usage."""
    header = [
        {
            "index": index,
            "id": call.call_id,
            "type": "function",
            "function": {"name": call.name, "arguments": ""},
        }
        for index, call in enumerate(calls)
    ]
    frames = [encode_frame(chunk_payload(model, {"role": "assistant", "tool_calls": header}))]
    for index, call in enumerate(calls):
        arguments = json.dumps(call.arguments, ensure_ascii=False)
        middle = len(arguments) // 2
        for fragment in (arguments[:middle], arguments[middle:]):
            frames.append(
                encode_frame(
                    chunk_payload(
                        model,
                        {"tool_calls": [{"index": index, "function": {"arguments": fragment}}]},
                    )
                )
            )
    frames.append(encode_frame(chunk_payload(model, {}, finish_reason="tool_calls")))
    frames.append(
        encode_frame(
            usage_payload(model, prompt_tokens=prompt_tokens, completion_tokens=12 * len(calls))
        )
    )
    frames.append(DONE_FRAME)
    return frames


def filler_chunk(chunk_index: int, token_count: int, *, timestamp: float | None) -> str:
    """Return ``token_count`` filler tokens, prefixed by a marker when timed."""
    words = " ".join(
        _FILLER_WORDS[(chunk_index + offset) % len(_FILLER_WORDS)] for offset in range(token_count)
    )
    prefix = format_marker(timestamp) if timestamp is not None else ""
    return f"{prefix}{words} "


async def stream_text(
    model: str,
    directive: PerfDirective,
    record: RequestRecord,
    *,
    prompt_tokens: int,
) -> AsyncIterator[bytes]:
    """Stream the scripted text response, pacing tokens on an absolute schedule."""
    if directive.think_ms:
        await asyncio.sleep(directive.think_ms / 1000.0)
    total_tokens = directive.text_tokens
    paced = directive.rate > 0
    tokens_per_chunk = 1 if paced else UNPACED_CHUNK_TOKENS
    start = time.monotonic()
    emitted = 0
    chunk_index = 0
    max_lag = 0.0
    first = True
    while emitted < total_tokens:
        count = min(tokens_per_chunk, total_tokens - emitted)
        if paced:
            target = start + emitted / directive.rate
            delay = target - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                max_lag = max(max_lag, -delay)
        now = time.time()
        timed = chunk_index % MARKER_EVERY_CHUNKS == 0
        delta: dict[str, Any] = {
            "content": filler_chunk(chunk_index, count, timestamp=now if timed else None)
        }
        if first:
            delta["role"] = "assistant"
            record.first_byte = now
            first = False
        yield encode_frame(chunk_payload(model, delta))
        emitted += count
        chunk_index += 1
        if not paced and chunk_index % 8 == 0:
            # Unpaced warmup still yields to the loop so parallel streams progress.
            await asyncio.sleep(0)
    record.tokens = emitted
    record.max_emit_lag_ms = round(max_lag * 1000.0, 3)
    yield encode_frame(chunk_payload(model, {}, finish_reason="stop"))
    yield encode_frame(usage_payload(model, prompt_tokens=prompt_tokens, completion_tokens=emitted))
    yield DONE_FRAME


def completion_body(model: str, planned: PlannedResponse, *, prompt_tokens: int) -> dict[str, Any]:
    """Non-streaming answer (utility requests such as Session titles)."""
    message: dict[str, Any] = {"role": "assistant", "content": AUX_RESPONSE_TEXT}
    finish_reason = "stop"
    completion_tokens = 8
    if planned.kind == "tool_calls":
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                }
                for call in planned.tool_calls
            ],
        }
        finish_reason = "tool_calls"
    elif planned.directive is not None:
        completion_tokens = planned.directive.text_tokens
        message["content"] = filler_chunk(0, completion_tokens, timestamp=time.time())
    return {
        "id": "chatcmpl-perf",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


# -- ASGI application ---------------------------------------------------------


def create_app(log: RequestLog | None = None) -> Any:
    """Build the Starlette app; ``log`` is injectable for tests."""
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response, StreamingResponse
    from starlette.routing import Route

    requests = log if log is not None else RequestLog()

    async def health(_request: Request) -> Response:
        return JSONResponse({"ok": True, "service": SERVICE_NAME})

    async def models(_request: Request) -> Response:
        return JSONResponse(
            {
                "object": "list",
                "data": [{"id": MODEL_ID, "object": "model", "owned_by": SERVICE_NAME}],
            }
        )

    async def stats(_request: Request) -> Response:
        return JSONResponse({"service": SERVICE_NAME, "requests": requests.snapshot()})

    async def reset(_request: Request) -> Response:
        requests.reset()
        return JSONResponse({"ok": True})

    async def chat_completions(request: Request) -> Response:
        record = requests.begin(time.time())
        raw = await request.body()
        record.body_read = time.time()
        record.prompt_bytes = len(raw)
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise PlanError("request body must be a JSON object")
            messages = body.get("messages")
            record.message_count = len(messages) if isinstance(messages, list) else 0
            record.stream = body.get("stream") is True
            planned = plan_response(body, next_call_id=requests.next_call_id)
        except (PlanError, json.JSONDecodeError) as exc:
            record.kind = "error"
            record.error = str(exc)
            record.completed = time.time()
            return JSONResponse(
                {"error": {"message": str(exc), "type": "invalid_request_error"}},
                status_code=400,
            )

        record.kind = planned.kind
        record.tag = planned.tag
        record.round = planned.round_index
        record.tool_names = [call.name for call in planned.tool_calls]
        record.tool_call_ids = [call.call_id for call in planned.tool_calls]
        requested_model = body.get("model")
        model = requested_model if isinstance(requested_model, str) else MODEL_ID
        prompt_tokens = max(1, record.prompt_bytes // 4)

        if not record.stream:
            record.first_byte = record.completed = time.time()
            return JSONResponse(completion_body(model, planned, prompt_tokens=prompt_tokens))
        return StreamingResponse(
            _recorded_stream(model, planned, record, prompt_tokens=prompt_tokens),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache"},
        )

    return Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/v1/models", models, methods=["GET"]),
            Route("/v1/chat/completions", chat_completions, methods=["POST"]),
            Route("/_perf/stats", stats, methods=["GET"]),
            Route("/_perf/reset", reset, methods=["POST"]),
        ]
    )


async def _recorded_stream(
    model: str,
    planned: PlannedResponse,
    record: RequestRecord,
    *,
    prompt_tokens: int,
) -> AsyncIterator[bytes]:
    finished = False
    try:
        if planned.kind == "tool_calls":
            frames = tool_call_frames(model, planned.tool_calls, prompt_tokens=prompt_tokens)
            record.first_byte = time.time()
            for frame in frames:
                yield frame
        elif planned.directive is not None:
            async for frame in stream_text(
                model, planned.directive, record, prompt_tokens=prompt_tokens
            ):
                yield frame
        else:
            record.first_byte = time.time()
            yield encode_frame(
                chunk_payload(model, {"role": "assistant", "content": AUX_RESPONSE_TEXT})
            )
            yield encode_frame(chunk_payload(model, {}, finish_reason="stop"))
            yield encode_frame(
                usage_payload(model, prompt_tokens=prompt_tokens, completion_tokens=8)
            )
            yield DONE_FRAME
        finished = True
    finally:
        record.completed = time.time()
        record.disconnected = not finished


def main(argv: list[str] | None = None) -> None:
    """Serve the fake Provider until terminated."""
    import uvicorn

    parser = argparse.ArgumentParser(description="Scripted fake Provider for scripts/perf_load.py")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
