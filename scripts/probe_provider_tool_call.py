#!/usr/bin/env python
"""Probe one configured Provider for streamed and non-streamed Tool Calls.

The probe deliberately prints only structural measurements. It never prints
credentials, prompts, generated content, Tool arguments, or raw Provider
responses.

Examples:
    python scripts/probe_provider_tool_call.py --model glm-5.2 --mode stream
    python scripts/probe_provider_tool_call.py --model glm-5.2 --mode nonstream --lines 500
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any

from core.runtime.runtime import Runtime
from core.utils.config import Config

DEFAULT_PROVIDER = "opencode-go"
DEFAULT_CONNECTION = "opencode-go:api-key"
DEFAULT_MODEL = "glm-5.2"
DEFAULT_LINES = 8
DEFAULT_IDLE_TIMEOUT_SECONDS = 180.0
DEFAULT_TOTAL_TIMEOUT_SECONDS = 900.0
PROBE_TOOL_NAME = "write_probe"

PROBE_TOOL = {
    "name": PROBE_TOOL_NAME,
    "description": "Write the supplied UTF-8 content to the probe target.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    },
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default=DEFAULT_PROVIDER)
    parser.add_argument("--connection", default=DEFAULT_CONNECTION)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--mode", choices=("stream", "nonstream"), default="stream")
    parser.add_argument("--wire", choices=("auto", "openai", "anthropic"), default="auto")
    parser.add_argument("--lines", type=int, default=DEFAULT_LINES)
    parser.add_argument(
        "--tool-choice",
        choices=("auto", "required", "explicit"),
        default="auto",
    )
    parser.add_argument("--thinking-effort", default="high")
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--idle-timeout", type=float, default=DEFAULT_IDLE_TIMEOUT_SECONDS)
    parser.add_argument("--total-timeout", type=float, default=DEFAULT_TOTAL_TIMEOUT_SECONDS)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument(
        "--trace-request",
        type=Path,
        help=(
            "Replay the request body from a vBot Provider debug trace. The trace "
            "content is read locally but never printed."
        ),
    )
    parser.add_argument(
        "--continue-trace-response",
        action="store_true",
        help=(
            "Append the trace's partial assistant response plus an internal recovery "
            "instruction before replaying it."
        ),
    )
    return parser


def _probe_content(line_count: int) -> str:
    if line_count <= 0:
        raise ValueError("--lines must be positive")
    return "\n".join(
        f"{index:04d}: deterministic provider Tool Call probe line"
        for index in range(1, line_count + 1)
    )


def _messages(content: str) -> list[dict[str, Any]]:
    return [
        {
            "role": "system",
            "content": (
                "You are a deterministic Tool Call probe. Call write_probe exactly once. "
                "Do not answer with ordinary text. Copy the payload between the markers "
                "verbatim into the content argument."
            ),
        },
        {
            "role": "user",
            "content": (
                "Use path probe-output.md and copy this exact payload:\n"
                "<PAYLOAD>\n"
                f"{content}\n"
                "</PAYLOAD>"
            ),
        },
    ]


def _tool_choice(value: str) -> str | dict[str, Any] | None:
    if value == "auto":
        return None
    if value == "required":
        return "required"
    return {"type": "function", "function": {"name": PROBE_TOOL_NAME}}


def _request_kwargs(
    args: argparse.Namespace,
    traced_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if traced_request is not None:
        kwargs = {
            key: traced_request[key]
            for key in ("max_tokens", "temperature", "reasoning_effort", "thinking_effort")
            if traced_request.get(key) is not None
        }
        kwargs["tools"] = _provider_tools_from_wire(traced_request.get("tools"))
    else:
        kwargs = {
            "thinking_effort": args.thinking_effort,
            "tools": [PROBE_TOOL],
        }
    if args.max_tokens is not None:
        kwargs["max_tokens"] = args.max_tokens
    if (choice := _tool_choice(args.tool_choice)) is not None:
        kwargs["tool_choice"] = choice
    return kwargs


def _provider_tools_from_wire(raw_tools: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_tools, list):
        return []
    tools: list[dict[str, Any]] = []
    for raw_tool in raw_tools:
        if not isinstance(raw_tool, dict):
            continue
        function = raw_tool.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        description = function.get("description")
        parameters = function.get("parameters")
        if isinstance(name, str) and isinstance(description, str) and isinstance(parameters, dict):
            tools.append(
                {
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                }
            )
    return tools


def _load_trace(path: Path) -> dict[str, Any]:
    trace = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(trace, dict):
        raise ValueError("trace root must be a JSON object")
    return trace


def _trace_request(trace: dict[str, Any]) -> dict[str, Any]:
    request = trace.get("request")
    if not isinstance(request, dict):
        raise ValueError("trace has no request object")
    body = request.get("body")
    if not isinstance(body, str):
        raise ValueError("trace request body is not text")
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise ValueError("trace request body must be a JSON object")
    return parsed


def _partial_assistant_from_trace(trace: dict[str, Any]) -> dict[str, Any]:
    response = trace.get("response")
    if not isinstance(response, dict):
        raise ValueError("trace has no response object")
    body = response.get("body")
    if not isinstance(body, str):
        raise ValueError("trace response body is not text")
    reasoning_parts: list[str] = []
    content_parts: list[str] = []
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        data = line.removeprefix("data:").strip()
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(chunk, dict):
            continue
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            continue
        delta = choices[0].get("delta")
        if not isinstance(delta, dict):
            continue
        reasoning = delta.get("reasoning_content")
        if not isinstance(reasoning, str):
            reasoning = delta.get("reasoning")
        content = delta.get("content")
        if isinstance(reasoning, str):
            reasoning_parts.append(reasoning)
        if isinstance(content, str):
            content_parts.append(content)
    reasoning = "".join(reasoning_parts)
    content = "".join(content_parts)
    if not reasoning and not content:
        raise ValueError("trace response contains no partial assistant output")
    return {
        "role": "assistant",
        "content": content or None,
        "reasoning": reasoning or None,
    }


def _append_interrupted_continuation(
    messages: list[dict[str, Any]],
    trace: dict[str, Any],
) -> None:
    messages.append(_partial_assistant_from_trace(trace))
    messages.append(
        {
            "role": "user",
            "content": (
                "[Internal recovery notice: The preceding assistant response was "
                "interrupted by the Provider before a finish signal. Continue the same "
                "work from that exact point without repeating preceding text. If you "
                "announced a Tool action, perform the Tool Call now.]"
            ),
        }
    )


def _messages_from_wire(raw_messages: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_messages, list):
        raise ValueError("trace request has no messages list")
    messages: list[dict[str, Any]] = []
    for raw_message in raw_messages:
        if not isinstance(raw_message, dict):
            raise ValueError("trace request contains a non-object message")
        role = raw_message.get("role")
        message = {
            key: value
            for key, value in raw_message.items()
            if key not in {"reasoning_content", "tool_calls"}
        }
        if role == "assistant":
            reasoning = raw_message.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                message["reasoning"] = reasoning
            raw_tool_calls = raw_message.get("tool_calls")
            if isinstance(raw_tool_calls, list):
                tool_calls: list[dict[str, Any]] = []
                for raw_call in raw_tool_calls:
                    if not isinstance(raw_call, dict):
                        continue
                    function = raw_call.get("function")
                    if not isinstance(function, dict):
                        continue
                    arguments = function.get("arguments")
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except json.JSONDecodeError:
                            arguments = {}
                    if not isinstance(arguments, dict):
                        arguments = {}
                    tool_calls.append(
                        {
                            "id": str(raw_call.get("id", "")),
                            "name": str(function.get("name", "")),
                            "arguments": arguments,
                        }
                    )
                if tool_calls:
                    message["tool_calls"] = tool_calls
        messages.append(message)
    return messages


def _argument_measurements(tool_calls: Any) -> tuple[int, int, int]:
    if not isinstance(tool_calls, list):
        return 0, 0, 0
    call_count = 0
    argument_chars = 0
    content_chars = 0
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        call_count += 1
        arguments = call.get("arguments")
        if not isinstance(arguments, dict):
            continue
        argument_chars += len(json.dumps(arguments, ensure_ascii=False))
        content = arguments.get("content")
        if isinstance(content, str):
            content_chars += len(content)
    return call_count, argument_chars, content_chars


async def _probe_stream(
    adapter: Any,
    messages: list[dict[str, Any]],
    args: argparse.Namespace,
    traced_request: dict[str, Any] | None,
) -> dict[str, Any]:
    started = time.monotonic()
    first_delta_seconds: float | None = None
    last_delta_seconds: float | None = None
    counts: dict[str, int] = {}
    content_chars = 0
    reasoning_chars = 0
    tool_argument_chars = 0
    tool_name_chars = 0
    tool_names_by_id: dict[str, str] = {}
    finish_reason: str | None = None
    status = "stream_ended"
    error_type: str | None = None

    stream = adapter.stream(
        messages,
        model_id=args.model,
        **_request_kwargs(args, traced_request),
    )
    iterator = stream.__aiter__()
    try:
        async with asyncio.timeout(args.total_timeout):
            while True:
                try:
                    delta = await asyncio.wait_for(
                        iterator.__anext__(),
                        timeout=args.idle_timeout,
                    )
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    status = "idle_timeout"
                    break
                elapsed = time.monotonic() - started
                if first_delta_seconds is None:
                    first_delta_seconds = elapsed
                last_delta_seconds = elapsed
                delta_type = str(delta.get("type", "unknown"))
                counts[delta_type] = counts.get(delta_type, 0) + 1
                text = delta.get("text")
                if delta_type == "content_delta" and isinstance(text, str):
                    content_chars += len(text)
                elif delta_type == "reasoning_delta" and isinstance(text, str):
                    reasoning_chars += len(text)
                elif delta_type == "tool_call_delta":
                    tool_call_id = str(delta.get("id", ""))
                    name_delta = str(delta.get("name_delta", ""))
                    tool_names_by_id[tool_call_id] = (
                        tool_names_by_id.get(tool_call_id, "") + name_delta
                    )
                    tool_name_chars += len(name_delta)
                    tool_argument_chars += len(str(delta.get("arguments_delta", "")))
                elif delta_type == "finish":
                    finish_reason = str(delta.get("reason", ""))
    except TimeoutError:
        status = "total_timeout"
    except Exception as exc:  # noqa: BLE001 - diagnostics must classify all Provider failures
        status = "error"
        error_type = type(exc).__name__
    finally:
        await stream.aclose()

    return {
        "mode": "stream",
        "status": status,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "first_delta_seconds": (
            round(first_delta_seconds, 3) if first_delta_seconds is not None else None
        ),
        "last_delta_seconds": (
            round(last_delta_seconds, 3) if last_delta_seconds is not None else None
        ),
        "delta_counts": counts,
        "reasoning_chars": reasoning_chars,
        "content_chars": content_chars,
        "tool_name_chars": tool_name_chars,
        "tool_names": sorted(name for name in tool_names_by_id.values() if name),
        "tool_argument_chars": tool_argument_chars,
        "finish_reason": finish_reason,
        "error_type": error_type,
    }


async def _probe_nonstream(
    adapter: Any,
    messages: list[dict[str, Any]],
    args: argparse.Namespace,
    traced_request: dict[str, Any] | None,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        async with asyncio.timeout(args.total_timeout):
            raw = await adapter.send(
                messages,
                model_id=args.model,
                **_request_kwargs(args, traced_request),
            )
        normalized = adapter.normalize_response(raw, model_id=args.model)
        tool_calls, argument_chars, tool_content_chars = _argument_measurements(
            normalized.get("tool_calls")
        )
        content = normalized.get("content")
        reasoning = normalized.get("reasoning")
        return {
            "mode": "nonstream",
            "status": "complete",
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "reasoning_chars": len(reasoning) if isinstance(reasoning, str) else 0,
            "content_chars": len(content) if isinstance(content, str) else 0,
            "tool_calls": tool_calls,
            "tool_argument_chars": argument_chars,
            "tool_content_chars": tool_content_chars,
            "error_type": None,
        }
    except TimeoutError:
        status = "total_timeout"
        error_type = None
    except Exception as exc:  # noqa: BLE001 - diagnostics must classify all Provider failures
        status = "error"
        error_type = type(exc).__name__
    return {
        "mode": "nonstream",
        "status": status,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "error_type": error_type,
    }


async def _run(args: argparse.Namespace) -> int:
    trace = _load_trace(args.trace_request) if args.trace_request else None
    traced_request = _trace_request(trace) if trace is not None else None
    if traced_request is None:
        expected_content = _probe_content(args.lines)
        messages = _messages(expected_content)
    else:
        messages = _messages_from_wire(traced_request.get("messages"))
        if args.continue_trace_response:
            if trace is None:
                raise ValueError("--continue-trace-response requires --trace-request")
            _append_interrupted_continuation(messages, trace)
        expected_content = None
        traced_model = traced_request.get("model")
        if isinstance(traced_model, str) and traced_model:
            args.model = traced_model
    runtime = Runtime(Config(data_dir=args.data_dir))
    runtime.start()
    adapter = runtime.get_adapter(args.provider, args.connection)
    request_adapter: Any = adapter
    try:
        if args.wire == "anthropic":
            request_adapter = getattr(adapter, "_messages", None)
            if request_adapter is None:
                raise ValueError("selected Provider adapter has no Anthropic Messages route")
        if args.mode == "stream":
            result = await _probe_stream(request_adapter, messages, args, traced_request)
        else:
            result = await _probe_nonstream(request_adapter, messages, args, traced_request)
    finally:
        await adapter.aclose()
        await runtime.aclose()

    result.update(
        {
            "provider": args.provider,
            "connection": args.connection,
            "model": args.model,
            "tool_choice": args.tool_choice,
            "requested_lines": args.lines if expected_content is not None else None,
            "expected_content_chars": (
                len(expected_content) if expected_content is not None else None
            ),
            "request_messages": len(messages),
            "request_tools": len(_request_kwargs(args, traced_request).get("tools", [])),
            "trace_replay": traced_request is not None,
            "trace_continuation": args.continue_trace_response,
            "wire": args.wire,
        }
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    successful = (
        result["status"] in {"complete", "stream_ended"}
        and result.get("finish_reason", "tool_calls") == "tool_calls"
        and (result.get("tool_argument_chars", 0) > 0 or result.get("tool_calls", 0) > 0)
    )
    return 0 if successful else 1


def main() -> int:
    return asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
