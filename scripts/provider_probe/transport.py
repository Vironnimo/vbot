"""Provider Tool probe: transport."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from typing import Any

from core.providers.tool_schema import ToolSchemaProfile
from core.tools.contracts import ToolContract
from scripts.provider_probe.common import PROBE_TOOL, PROBE_TOOL_NAME, ProbeScenario
from scripts.provider_probe.measurements import (
    _argument_measurements,
    _expected_argument_measurements,
    _invalid_measurement,
    _optional_boolean_measurements,
    _validation_measurements,
)
from scripts.provider_probe.trace import _provider_tools_from_wire


def _expected_profile(args: argparse.Namespace) -> ToolSchemaProfile:
    if args.profile == "explicit_non_strict":
        return "explicit_non_strict"
    if args.profile == "omit_strict":
        return "omit_strict"
    if args.provider == "openai":
        return "explicit_non_strict"
    return "omit_strict"


def _tool_choice(value: str, tool_name: str) -> str | dict[str, Any] | None:
    if value == "auto":
        return None
    if value == "required":
        return "required"
    return {"type": "function", "function": {"name": tool_name}}


def _request_kwargs(
    args: argparse.Namespace,
    traced_request: dict[str, Any] | None = None,
    *,
    tools: list[dict[str, Any]] | None = None,
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
            "tools": tools or [PROBE_TOOL],
        }
    if args.max_tokens is not None:
        kwargs["max_tokens"] = args.max_tokens
    selected_tools = kwargs["tools"]
    selected_name = (
        str(selected_tools[0].get("name", PROBE_TOOL_NAME))
        if isinstance(selected_tools, list) and selected_tools
        else PROBE_TOOL_NAME
    )
    if (choice := _tool_choice(args.tool_choice, selected_name)) is not None:
        kwargs["tool_choice"] = choice
    return kwargs


def _probe_tool_call_stream_key(delta: dict[str, Any]) -> str:
    """Return the stable Tool Call key used by the normalized stream contract."""
    slot = delta.get("slot")
    if isinstance(slot, int) and not isinstance(slot, bool):
        return f"index:{slot}"
    if isinstance(slot, str) and slot:
        return f"slot:{slot}"

    tool_call_id = delta.get("id")
    if isinstance(tool_call_id, str) and tool_call_id:
        return f"id:{tool_call_id}"
    raise ValueError("tool_call_delta must contain a slot or non-empty id")


async def _probe_stream(
    adapter: Any,
    messages: list[dict[str, Any]],
    args: argparse.Namespace,
    traced_request: dict[str, Any] | None,
    tools: list[dict[str, Any]],
    contracts: dict[str, ToolContract],
    scenario: ProbeScenario,
) -> dict[str, Any]:
    started = time.monotonic()
    first_delta_seconds: float | None = None
    last_delta_seconds: float | None = None
    counts: dict[str, int] = {}
    content_chars = 0
    reasoning_chars = 0
    tool_argument_chars = 0
    tool_name_chars = 0
    tool_names_by_stream_key: dict[str, str] = {}
    tool_arguments_by_stream_key: dict[str, str] = {}
    finish_reason: str | None = None
    status = "stream_ended"
    error_type: str | None = None

    stream = adapter.stream(
        messages,
        model_id=args.model,
        **_request_kwargs(args, traced_request, tools=tools),
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
                    stream_key = _probe_tool_call_stream_key(delta)
                    name_delta = str(delta.get("name_delta", ""))
                    tool_names_by_stream_key[stream_key] = (
                        tool_names_by_stream_key.get(stream_key, "") + name_delta
                    )
                    arguments_delta = str(delta.get("arguments_delta", ""))
                    tool_arguments_by_stream_key[stream_key] = (
                        tool_arguments_by_stream_key.get(stream_key, "") + arguments_delta
                    )
                    tool_name_chars += len(name_delta)
                    tool_argument_chars += len(arguments_delta)
                elif delta_type == "finish":
                    finish_reason = str(delta.get("reason", ""))
    except TimeoutError:
        status = "total_timeout"
    except Exception as exc:  # noqa: BLE001 - diagnostics must classify all Provider failures
        status = "error"
        error_type = type(exc).__name__
    finally:
        await stream.aclose()

    parsed_calls: list[dict[str, Any]] = []
    validation = _invalid_measurement("invalid_json")
    try:
        for stream_key, name in tool_names_by_stream_key.items():
            arguments = json.loads(tool_arguments_by_stream_key.get(stream_key, ""))
            parsed_calls.append({"name": name, "arguments": arguments})
    except json.JSONDecodeError:
        pass
    else:
        validation = _validation_measurements(parsed_calls, contracts)

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
        "tool_calls": len(parsed_calls),
        "tool_argument_chars": tool_argument_chars,
        "finish_reason": finish_reason,
        "error_type": error_type,
        **_optional_boolean_measurements(parsed_calls, tools),
        **_expected_argument_measurements(parsed_calls, scenario),
        **validation,
    }


async def _probe_nonstream(
    adapter: Any,
    messages: list[dict[str, Any]],
    args: argparse.Namespace,
    traced_request: dict[str, Any] | None,
    tools: list[dict[str, Any]],
    contracts: dict[str, ToolContract],
    scenario: ProbeScenario,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        async with asyncio.timeout(args.total_timeout):
            raw = await adapter.send(
                messages,
                model_id=args.model,
                **_request_kwargs(args, traced_request, tools=tools),
            )
        normalized = adapter.normalize_response(raw, model_id=args.model)
        normalized_tool_calls = normalized.get("tool_calls")
        tool_calls, argument_chars, tool_content_chars = _argument_measurements(
            normalized_tool_calls
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
            **_optional_boolean_measurements(normalized_tool_calls, tools),
            **_expected_argument_measurements(normalized_tool_calls, scenario),
            **_validation_measurements(normalized_tool_calls, contracts),
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
        **_invalid_measurement(error_type or status),
    }
